"""Write-guard: let a page read anything, but block everything that would change data.

Installed before the page's own scripts. GET/HEAD/OPTIONS requests pass; any other fetch/XHR method, form
POST submission and sendBeacon is blocked and recorded in `window.__swBlocked` as [method, url].
Used (1) while exploring, so clicking buttons to see what they open can never change the app, and (2) by
"form only" flows, which fill and submit a form and then check that it TRIED to save, without saving.

Limits (page-level, not a network proxy): service-worker traffic and native form navigations to other
origins are not intercepted; websockets are not blocked.
"""

import json

GUARD = """(() => {
  if (window.__swGuarded) return;
  window.__swGuarded = true;
  window.__swBlocked = [];
  const safe = m => ["GET", "HEAD", "OPTIONS"].includes((m || "GET").toUpperCase());
  const record = (method, url) => { window.__swBlocked.push([String(method).toUpperCase(), String(url)]); };
  const fetch = window.fetch;
  if (fetch) window.fetch = function (input, init) {
    const method = (init && init.method) || (input && typeof input === "object" && input.method) || "GET";
    const url = typeof input === "string" ? input : (input && input.url) || String(input);
    if (!safe(method)) {
      record(method, url);
      return Promise.reject(new TypeError("EveryRole blocked a write request"));
    }
    return fetch.apply(this, arguments);
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__swMethod = method; this.__swUrl = url;
    return open.apply(this, arguments);
  };
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    if (!safe(this.__swMethod)) {
      record(this.__swMethod, this.__swUrl);
      setTimeout(() => this.dispatchEvent(new ProgressEvent("error")), 0);
      return;
    }
    return send.apply(this, arguments);
  };
  if (navigator.sendBeacon) navigator.sendBeacon = (url) => { record("BEACON", url); return true; };
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form && (form.method || "get").toLowerCase() !== "get") {
      event.preventDefault();
      record("FORM", form.action || location.href);
    }
  }, true);
})()"""

BLOCKED = "JSON.stringify(window.__swBlocked || [])"


def block_writes(browser):
    """Engine `prepare` hook: install the guard on every document this tab loads."""
    browser.call("Page.enable")
    browser.call("Page.addScriptToEvaluateOnNewDocument", source=GUARD)


def blocked(browser):
    """[[method, url], ...] write attempts blocked so far on the current document."""
    try:
        return [tuple(x) for x in json.loads(browser.evaluate(BLOCKED) or "[]")]
    except Exception:
        return []


def app_writes(entries):
    """Blocked attempts that look like the app saving something (analytics beacons excluded)."""
    return [e for e in entries if e[0] != "BEACON"]
