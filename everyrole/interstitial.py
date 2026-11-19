"""Full-page interstitials: a window from another site laid over the whole page (an ad shown between two pages, a
"vignette"), closed the way a person closes it before EveryRole reads or acts on the page.

automationexercise.com, 2026-11-17: after a click, an ad network laid its ad over the whole page (a frame from its own
site, the address gaining "#google_vignette"). Every control of the page was under it, the engine can't see inside
another site's frame, so the journey stopped at step 1 or 2 and the step's checks were reported as never seen.

What makes one, without anything written for one site or ad network: a frame whose page comes from another site (the
app's own scripts can't read it) is what the middle of the window shows, and it covers most of the window.
How it is closed, each way checked (the cover gone) before the next is tried:
1. its own close control: inside that frame, the visible control whose name says close, dismiss or skip (in the
   languages below), clicked where a person would click it;
2. else, when the cover put itself in the browser's history (the entry before it is the same page without its "#..."
   part, so Back closes it), going back that one entry, as a person pressing Back would.
Nothing else is ever pressed in that frame, and a frame the app's own page can read is never one (its controls are
listed for the agent like the page's).

close(browser) -> {"found": bool, "closed": bool, "by": the frame's site, "how": "close control" | "back" | ""}.
"""

import json
import time
from urllib.parse import urldefrag

# "most of the window": the share of it the frame must cover to be a cover, not a banner or an embed
COVER_SHARE = 0.6
# a close control's name (its label, title or words), in the languages the consent vocabulary covers
CLOSE = (r"^\s*[×✕✖╳xX]\s*$|^\s*(?:close|dismiss|skip|fermer|passer|schlie(?:ß|ss)en|überspringen|cerrar|omitir|"
         r"saltar|chiudi|salta|fechar|pular|sluiten|overslaan)\b|\b(?:close|dismiss|skip)"
         r"(?: (?:ad|this ad|advertisement))?\s*$")
CLOSED_WAIT_S = 3  # how long a closed cover may take to go away (its closing animation)

# the cover in the app's page: the frame at the middle of the window, from another site, over most of the window
FIND_COVER = r"""((share) => {
  if (!innerWidth || !innerHeight) return null;
  const hit = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  if (!hit || !['IFRAME', 'FRAME'].includes(hit.tagName)) return null;
  let readable = null;
  try { readable = hit.contentDocument; } catch (err) { readable = null; }
  if (readable) return null;  // the app's own frame: its controls are listed like the page's
  const r = hit.getBoundingClientRect();
  const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
  const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
  return w * h >= innerWidth * innerHeight * share ? hit : null;
})"""

# inside the frame: its visible close control nearest to a button, the smallest one that says so, with its middle
FIND_CLOSE = r"""((close) => {
  const CLOSE = new RegExp(close, 'i');
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; };
  const named = e => [e.getAttribute('aria-label'), e.getAttribute('title'), e.getAttribute('alt'),
    (e.innerText || e.value || '').length <= 30 ? (e.innerText || e.value || '') : '']
    .map(t => (t || '').replace(/\s+/g, ' ').trim()).find(t => t && CLOSE.test(t)) || '';
  const control = e => e.matches('button, a, [role="button"], input[type="button"], input[type="submit"], [onclick]') ||
    getComputedStyle(e).cursor === 'pointer';
  const found = [];
  const walk = root => { for (const e of root.querySelectorAll('*')) {
    if (e.shadowRoot) walk(e.shadowRoot);
    if (['HTML', 'BODY'].includes(e.tagName) || !shown(e)) continue;
    const said = named(e);
    if (said && (control(e) || e.querySelector('*') === null || e.getAttribute('aria-label'))) found.push([e, said]);
  } };
  walk(document);
  const area = e => { const r = e.getBoundingClientRect(); return r.width * r.height; };
  found.sort((a, b) => (control(b[0]) - control(a[0])) || (area(a[0]) - area(b[0])));
  for (const [e, said] of found) {
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    const top = document.elementFromPoint(x, y);
    if (top && (e === top || e.contains(top) || top.contains(e))) return {x, y, label: said.slice(0, 40)};
  }
  return null;
})"""

# where the frame's own page starts on the app's page (its border inside), and the site it comes from
FRAME_AT = """function () { const r = this.getBoundingClientRect(); let from = '';
  try { from = new URL(this.src, location.href).hostname; } catch (err) { from = ''; }
  return {x: r.x + this.clientLeft, y: r.y + this.clientTop, from}; }"""

GROUP = "everyrole-cover"


def cover(browser):
    """-> {"frame": its frame id, "x", "y" (where its page starts), "by": its site}, or None when nothing covers."""
    try:
        found = browser.call("Runtime.evaluate", expression=f"{FIND_COVER}({COVER_SHARE})", objectGroup=GROUP)
        element = found.get("result", {})
        if found.get("exceptionDetails") or element.get("subtype") == "null" or not element.get("objectId"):
            return None
        node = browser.call("DOM.describeNode", objectId=element["objectId"]).get("node", {})
        at = browser.call("Runtime.callFunctionOn", objectId=element["objectId"], functionDeclaration=FRAME_AT,
                          returnByValue=True).get("result", {}).get("value") or {}
    except Exception:
        return None
    finally:
        try:
            browser.call("Runtime.releaseObjectGroup", objectGroup=GROUP)
        except Exception:
            pass
    if not node.get("frameId"):
        return None
    return {"frame": node["frameId"], "x": at.get("x", 0), "y": at.get("y", 0), "by": at.get("from", "")}


def close(browser, sleep=time.sleep, clock=time.monotonic, announced_only=False):
    """Close a full-page cover from another site, the way a person would. -> {"found", "closed", "by", "how"}.
    `announced_only`: only a cover that put itself in the browser's history (an ad between two pages does: "#..."),
    as after any ordinary action; a window the app opened on purpose (another site's checkout, a sign-in window) does
    not, and is left to the test unless it is stuck under it."""
    seen = cover(browser)
    if not seen or (announced_only and not _announced(browser)):
        return {"found": False, "closed": False, "by": "", "how": ""}
    answer = {"found": True, "closed": False, "by": seen["by"], "how": ""}
    try:
        spot = browser.evaluate_in_frame(seen["frame"], f"{FIND_CLOSE}({json.dumps(CLOSE)})")
    except Exception:
        spot = None
    if isinstance(spot, dict) and "x" in spot:
        x, y = seen["x"] + spot["x"], seen["y"] + spot["y"]
        try:
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                browser.call("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                             button="left" if kind != "mouseMoved" else "none", clickCount=1)
        except Exception:
            pass
        if _gone(browser, seen, sleep, clock):
            return {**answer, "closed": True, "how": "close control"}
    if _back_closes(browser) and _gone(browser, seen, sleep, clock):
        return {**answer, "closed": True, "how": "back"}
    return answer


def _gone(browser, seen, sleep, clock):
    """The cover went away (or another took its place) within CLOSED_WAIT_S."""
    deadline = clock() + CLOSED_WAIT_S
    while True:
        now = cover(browser)
        if not now or now["frame"] != seen["frame"]:
            return True
        if clock() >= deadline:
            return False
        sleep(0.2)


def _announced(browser):
    """The entry before this one in the history is this same page without its "#..." part."""
    try:
        history = browser.call("Page.getNavigationHistory")
        entries, index = history.get("entries") or [], history.get("currentIndex", 0)
        if index < 1 or index >= len(entries):
            return False
        here, before = entries[index]["url"], entries[index - 1]["url"]
        page, fragment = urldefrag(here)
        return bool(fragment) and urldefrag(before)[0] == page and before != here
    except Exception:
        return False


def _back_closes(browser):
    """The cover put itself in the history (the entry before is this same page without its "#..." part): go back
    that one entry. -> True when it went back."""
    try:
        history = browser.call("Page.getNavigationHistory")
        entries, index = history.get("entries") or [], history.get("currentIndex", 0)
        if index < 1 or index >= len(entries):
            return False
        here, before = entries[index]["url"], entries[index - 1]["url"]
        page, fragment = urldefrag(here)
        if not fragment or urldefrag(before)[0] != page or before == here:
            return False
        browser.call("Page.navigateToHistoryEntry", entryId=entries[index - 1]["id"])
        return True
    except Exception:
        return False


def said(answer):
    """The answer in plain words, or "" when nothing covered the page."""
    if not answer or not answer.get("found"):
        return ""
    by = f" from {answer['by']}" if answer.get("by") else ""
    if answer.get("closed"):
        return f"Closed a full-page window{by} (an ad) that covered the page"
    return f"A full-page window{by} (an ad) covered the page and had no way to close it"
