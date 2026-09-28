"""Wait until a page has really finished loading before it is judged or photographed.

A page is settled when no fetch/XHR request is still in flight AND its text length, element count, URL and
number of finished requests stay the same across a few consecutive reads. The in-flight count comes from a
small tracker installed before the page's own scripts run (`track_network`). Screen-only stability is not
enough: a skeleton screen does not change while it waits for an API, and would look "done".
Pages that never go quiet (long polling, streaming) are simply judged when `max_s` runs out.
"""

import time

TRACKER = """(() => {
  if (window.__swTracked) return;
  window.__swTracked = true;
  window.__swInflight = 0;
  const done = () => { window.__swInflight = Math.max(0, window.__swInflight - 1); };
  const fetch = window.fetch;
  if (fetch) window.fetch = function (...args) {
    window.__swInflight++;
    return fetch.apply(this, args).finally(done);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    window.__swInflight++;
    this.addEventListener("loadend", done, { once: true });
    return send.apply(this, args);
  };
})()"""

PAGE_STATE = ("[document.readyState, document.body ? document.body.innerText.length : 0, "
              "document.getElementsByTagName('*').length, location.href, "
              "performance.getEntriesByType('resource').length, window.__swInflight || 0]")


def track_network(browser):
    """Engine `prepare` hook: install the request tracker on every document this tab loads."""
    browser.call("Page.enable")
    browser.call("Page.addScriptToEvaluateOnNewDocument", source=TRACKER)


def chain(*hooks):
    """Run several `prepare` hooks in order (e.g. restore a session, then track the network)."""
    hooks = [h for h in hooks if h]

    def prepare(browser):
        for hook in hooks:
            hook(browser)

    return prepare


def read_state(browser):
    """-> (state, idle): what the page looks like now, and whether no request is in flight. None mid-navigation."""
    try:
        state = browser.evaluate(PAGE_STATE)
    except Exception:
        return None
    return state, not isinstance(state, list) or len(state) < 6 or state[5] == 0


class Steadiness:
    """Counts consecutive reads where the page stayed the same with no request in flight."""

    def __init__(self, stable_reads=3):
        self._needed, self._last, self._same = stable_reads - 1, None, 0

    def read(self, reading):
        """`reading`: from read_state. -> True once the page is settled."""
        if reading is None:
            self._last, self._same = None, 0  # navigating; start over
            return False
        state, idle = reading
        self._same = self._same + 1 if (state == self._last and idle) else 0
        self._last = state
        return self._same >= self._needed


def wait_until_stable(browser, *, max_s, poll_s=0.4, stable_reads=3, clock=time.monotonic, sleep=time.sleep):
    """-> True once settled, False if `max_s` passed first."""
    deadline = clock() + max_s
    steady = Steadiness(stable_reads)
    while clock() < deadline:
        sleep(poll_s)
        if steady.read(read_state(browser)):
            return True
    return False
