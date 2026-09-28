"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import socket
import struct
import sys
import time
from pathlib import Path

import os

from browser_harness import _ipc
from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp


def _closing_without_time_wait(connect):
    """Each browser call is one local connection to the harness daemon. On Windows (TCP on 127.0.0.1) every closed
    one waits up to 4 minutes (TIME_WAIT) holding a port; polling a page every few milliseconds used up Windows'
    16,384 outgoing ports and froze the whole machine's network. The reply is fully read before the socket closes,
    so these local connections are closed at once (SO_LINGER 0) instead."""
    def connect_now(name, timeout=1.0):
        sock, token = connect(name, timeout=timeout)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        return sock, token

    return connect_now


if sys.platform == "win32":
    _ipc.connect = _closing_without_time_wait(_ipc.connect)

# Browser Harness waits 5 s for each call by default; large pages under parallel load need longer.
IPC_TIMEOUT_S = float(os.environ.get("JEV_IPC_TIMEOUT_S", "20"))

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

# Browser dialogs (alert/confirm/prompt) freeze the page and every input sent to it. They never block here:
# confirm answers Cancel and prompt answers nothing (the safe choice: a "Delete?" is never confirmed), and each
# message is kept in window.__jevDialogs so a caller can report what the page asked.
NO_BLOCKING_DIALOGS = """(() => {
  const seen = window.__jevDialogs = window.__jevDialogs || [];
  const note = (kind, message) => { seen.push({kind, message: String(message ?? '').slice(0, 200)}); };
  window.alert = message => { note('alert', message); };
  window.confirm = message => { note('confirm', message); return false; };
  window.prompt = message => { note('prompt', message); return null; };
})()"""


TABS_FILE_ENV = "JEV_TABS_FILE"  # a file every tab this process opens is written to, one target id a line


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url, *, isolated=False, context=None, prepare=None, settle=None):
        ensure_daemon()
        # isolated: a fresh browser context (own cookies/storage), like an incognito window per run.
        # context: open this tab in another tab's context instead (shared cookies, storage and connections);
        # that tab owns it and disposes of it.
        self._owns_context = isolated and not context
        self.context = context or (cdp("Target.createBrowserContext")["browserContextId"] if isolated else None)
        target = {"url": "about:blank", "background": True}
        if self.context:
            target["browserContextId"] = self.context
        self.target = cdp("Target.createTarget", **target)["targetId"]
        if os.environ.get(TABS_FILE_ENV):  # whoever started this process closes it if this process can't
            with open(os.environ[TABS_FILE_ENV], "a", encoding="utf-8") as tabs:
                tabs.write(self.target + "\n")
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.enable")
        self.call("Page.addScriptToEvaluateOnNewDocument", source=NO_BLOCKING_DIALOGS)
        if prepare:
            prepare(self)  # e.g. restore a saved session before the first navigation
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)
        if settle:
            settle(self)  # e.g. wait for the app's data to load before the first observation

    def call(self, method, **params):
        return cdp(method, session_id=self.session, _response_timeout=IPC_TIMEOUT_S, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        # Target-scoped for actions: the element and its surrounding form/card must be unchanged. Live content
        # elsewhere on the page (tickers, rotating headlines) must not make every decision stale.
        if action is not None and action["kind"] in {"click", "select", "fill", "submit"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None
        if getattr(self, "context", None) and getattr(self, "_owns_context", True):
            cdp("Target.disposeBrowserContext", browserContextId=self.context)
        self.context = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, _response_timeout=IPC_TIMEOUT_S, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=action.get("x", 550), y=action.get("y", 650), deltaX=0,
                 deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              // inside a same-origin iframe: its position on the page is the frames' offsets plus its own
              const offset=()=>{
                let x=0, y=0, w=e.ownerDocument.defaultView;
                while (w.frameElement) {
                  const f=w.frameElement, r=f.getBoundingClientRect();
                  x+=r.x+f.clientLeft; y+=r.y+f.clientTop; w=w.parent;
                }
                return {x,y};
              };
              const at=()=>{
                const r=e.getBoundingClientRect(), o=offset(), lx=r.x+r.width/2, ly=r.y+r.height/2;
                const x=lx+o.x, y=ly+o.y;
                if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
                const frame=e.ownerDocument.defaultView.frameElement;
                if (frame && document.elementFromPoint(x,y)!==frame && !frame.contains(document.elementFromPoint(x,y)))
                  return null;  // the frame itself is covered on the page
                return e.contains(e.ownerDocument.elementFromPoint(lx,ly)) ? {x,y} : null;
              };
              // Covered by something fixed on screen (a banner, a chat button): bring the target to the middle
              // of the view, where fixed bars at the edges no longer cover it, and check again.
              let point=at();
              if (!point) { e.scrollIntoView({block:'center',inline:'center'}); point=at(); }
              if (!point) return null;
              const {x,y}=point;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "submit":  # P12: the click focused the input; now press Enter in it
                    for event in ("keyDown", "keyUp"):
                        call("Input.dispatchKeyEvent", type=event, key="Enter", code="Enter",
                             windowsVirtualKeyCode=13, nativeVirtualKeyCode=13,
                             **({"text": "\r"} if event == "keyDown" else {}))
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
