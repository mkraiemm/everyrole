"""Signed-in sessions: capture once per persona, restore into every later flow before its first page load.

A session file holds live credentials (auth cookies/tokens). It is written only to a private temp
directory owned by the suite, never to the report directory, and deleted when the suite ends.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Fields Network.setCookies accepts (CookieParam); Network.getAllCookies returns extra read-only ones.
COOKIE_FIELDS = (
    "name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires",
    "priority", "sourceScheme", "sourcePort", "partitionKey",
)

RESTORE_STORAGE = """(() => {
  const saved = %s;
  const items = saved[location.origin];
  if (!items) return;
  for (const [key, value] of items) {
    if (localStorage.getItem(key) === null) localStorage.setItem(key, value);
  }
})()"""


def capture(browser, origins=()):
    """Cookies of the flow's browser context + localStorage of the current origin and of every other origin
    in `origins` (the app's other parts: signing in on app.example.com may also store something for
    example.com).

    Cookies are read through the flow's own tab session, which is scoped to its isolated context.
    """
    cookies = browser.call("Network.getAllCookies").get("cookies", [])
    origin = browser.evaluate("location.origin")
    local = json.loads(browser.evaluate("JSON.stringify(Object.entries(localStorage))") or "[]")
    storage = {origin: local}
    for other in origins:
        if other != origin:
            items = stored(browser, other)
            if items:
                storage[other] = items
    return {"cookies": [{k: c[k] for k in COOKIE_FIELDS if k in c} for c in cookies], "storage": storage}


def stored(call_target, origin):
    """localStorage entries another origin holds in this browser, read without opening it; [] when it holds
    none (a part the sign-in never touched)."""
    call = call_target.call
    try:
        call("DOMStorage.enable")
        entries = call("DOMStorage.getDOMStorageItems", storageId={"securityOrigin": origin, "isLocalStorage": True})
    except Exception:
        return []  # the browser keeps nothing for that origin
    return [list(e) for e in entries.get("entries", [])]


def restorer(state):
    """Engine `prepare` hook: runs after the tab exists, before its first navigation."""

    def prepare(browser):
        if state.get("cookies"):
            browser.call("Network.setCookies", cookies=state["cookies"])  # scoped to this tab's context
        if state.get("storage"):
            browser.call("Page.enable")
            browser.call(
                "Page.addScriptToEvaluateOnNewDocument", source=RESTORE_STORAGE % json.dumps(state["storage"])
            )

    return prepare


def save(state, path):
    path = Path(path)
    path.write_text(json.dumps(state), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@contextmanager
def private_dir():
    """A temp directory for session files, removed with everything in it when the suite ends."""
    with tempfile.TemporaryDirectory(prefix="everyrole-sessions-") as path:
        yield Path(path)
