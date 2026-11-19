"""Fault injection for benchmarking: plant a bug in the browser, then see whether the suite catches it.

A bug is a JSON list of rules applied to every page before the app's own scripts run (and kept applied as the
app re-renders). It changes what users get without touching the app's code or server:

  {"hide": "text"}             remove every control whose label contains the text
  {"disable": "text"}          disable matching buttons/fields
  {"swallow_click": "text"}    clicks on matching buttons/links do nothing
  {"replace_text": ["a", "b"]} replace text a with b everywhere on the page
  {"fail_request": "/api/x", "status": 500}   requests to that address fail with that status

Enabled with `everyrole run --inject bug.json` (sets EVERYROLE_INJECT for every worker). Never used otherwise.
"""

import json
import os
from pathlib import Path

ENV = "EVERYROLE_INJECT"

INJECTOR = r"""(() => {
  if (window.__sqInjected) return;
  window.__sqInjected = true;
  const rules = %s;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = e => norm(e.getAttribute('aria-label') || e.innerText || e.value ||
    e.getAttribute('placeholder') || e.getAttribute('title') || '');
  const matches = (e, t) => textOf(e).includes(t);
  const fails = rules.filter(r => r.fail_request);
  if (fails.length && window.fetch) {
    const fetch = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || String(input);
      const hit = fails.find(r => url.includes(r.fail_request));
      if (hit) return Promise.resolve(new Response(JSON.stringify({ error: 'injected failure' }),
        { status: hit.status || 500, headers: { 'content-type': 'application/json' } }));
      return fetch.apply(this, arguments);
    };
  }
  document.addEventListener('click', (event) => {
    for (const r of rules) {
      if (!r.swallow_click) continue;
      const el = event.target.closest && event.target.closest('button,a,[role="button"],input[type="submit"]');
      if (el && matches(el, r.swallow_click)) { event.stopImmediatePropagation(); event.preventDefault(); }
    }
  }, true);
  const controls = 'button,a,[role="button"],[role="link"],input,textarea,select';
  const apply = () => {
    for (const r of rules) {
      if (r.hide) document.querySelectorAll(controls).forEach(e => {
        if (matches(e, r.hide)) e.style.setProperty('display', 'none', 'important');
      });
      if (r.disable) document.querySelectorAll(controls).forEach(e => {
        if (matches(e, r.disable)) e.disabled = true;
      });
      if (r.replace_text && document.body) {
        const [from, to] = r.replace_text;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
          if (node.nodeValue.includes(from)) node.nodeValue = node.nodeValue.split(from).join(to);
        }
      }
    }
  };
  const start = () => {
    apply();
    new MutationObserver(apply).observe(document.documentElement,
      { subtree: true, childList: true, characterData: true });
  };
  if (document.documentElement) start(); else document.addEventListener('readystatechange', start, { once: true });
})()"""


def rules_from_env(environ=None):
    environ = os.environ if environ is None else environ
    path = environ.get(ENV)
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))["rules"]


def injector(rules):
    """Engine `prepare` hook that plants the bug on every document this tab loads."""
    source = INJECTOR % json.dumps(rules)

    def prepare(browser):
        browser.call("Page.enable")
        browser.call("Page.addScriptToEvaluateOnNewDocument", source=source)

    return prepare


def from_env():
    rules = rules_from_env()
    return injector(rules) if rules else None
