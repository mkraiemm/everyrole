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
  window.__swPending = new Map();  // what is in flight, to say what a page that never settles is waiting for
  let next = 0;
  const where = (url) => {  // host and path, never the query: it may hold tokens
    try { const u = new URL(url, location.href); return u.host + u.pathname; }
    catch (e) { return String(url).split("?")[0]; }
  };
  const start = (kind, url) => {
    const id = ++next;
    window.__swPending.set(id, [kind, where(url), Date.now()]);
    return id;
  };
  const done = (id) => () => {
    window.__swInflight = Math.max(0, window.__swInflight - 1);
    window.__swPending.delete(id);
  };
  const fetch = window.fetch;
  if (fetch) window.fetch = function (...args) {
    window.__swInflight++;
    const id = start("fetch", args[0] && args[0].url || args[0]);
    return fetch.apply(this, args).finally(done(id));
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__swUrl = url;
    return open.call(this, method, url, ...rest);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    window.__swInflight++;
    this.addEventListener("loadend", done(start("xhr", this.__swUrl)), { once: true });
    return send.apply(this, args);
  };
})()"""

PENDING = ("[...(window.__swPending || new Map()).values()]"
           ".map(([kind, where, since]) => [kind, where, (Date.now() - since) / 1000])")

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


def read_pending(browser):
    """-> [(kind, host and path, seconds in flight)] of the requests a page is still waiting for ([] if unknown)."""
    try:
        found = browser.evaluate(PENDING)
    except Exception:
        return []
    return [(k, w, float(s)) for k, w, s in found] if isinstance(found, list) else []


def describe_pending(pending, limit=3):
    """"waiting for xhr api.app.io/poll (4.9 s)" — the oldest first, for a result a person reads."""
    if not pending:
        return ""
    oldest = sorted(pending, key=lambda item: -item[2])
    shown = ", ".join(f"{kind} {where} ({age:.1f} s)" for kind, where, age in oldest[:limit])
    more = f" and {len(oldest) - limit} more" if len(oldest) > limit else ""
    return f"waiting for {shown}{more}"


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
