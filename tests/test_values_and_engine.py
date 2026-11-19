import json

import pytest
from jev_ultrafast import model

from everyrole.redact import Redactor
from everyrole.values import WRITE, ValueChooser

CONTEXT = {
    "goal": "Log in with the test account",
    "field": {"label": "Password", "role": "textbox", "value": ""},
    "page": {"title": "Sign in", "text": "Sign in to the shop"},
    "recent_actions": [],
}


@pytest.fixture(autouse=True)
def provider_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setenv("TYPESAFE_URL", "https://gateway.example/typesafe/v1/systemone")
    monkeypatch.setenv("TYPESAFE_MODEL", "typesafe-ai/jev")


def answer(choice, names):
    probabilities = {n: (1.0 if n == choice else 0.0) for n in names}
    return {"answers": {"value": {"type": "choice", "choice": choice, "probabilities": probabilities,
                                  "confidence": 1}}, "usage": {"input_tokens": 42}}


def test_chooser_sends_only_value_names_and_types_literal_locally():
    sent = []
    values = {"email": "qa@example.com", "password": "hunter2-secret", "site": "https://example.com"}

    def post(url, key, body):
        sent.append((url, body))
        return answer("password", [*values, WRITE])

    chooser = ValueChooser(values, ("qa@example.com", "hunter2-secret"), post=post, write=None)
    text, helper = chooser(CONTEXT, {"label": "Password"}, {})
    assert text == "hunter2-secret"
    assert helper["value_choice"] == "password" and helper["usage"] == {"input_tokens": 42}
    url, body = sent[0]
    assert url == "https://gateway.example/typesafe/v1/systemone" and body["model"] == "typesafe-ai/jev"
    criteria = body["questions"]["value"]["criteria"]
    assert set(criteria) == {"email", "password", "site", WRITE}
    assert "hunter2-secret" not in json.dumps(body) and "qa@example.com" not in json.dumps(body)
    assert "https://example.com" in criteria["site"]  # non-secret values are shown so Jev can match their shape
    assert "hidden" in criteria["password"]


def test_chooser_write_falls_back_to_text_llm():
    written = []

    def write(context):
        written.append(context)
        return "free text", {"model": "text-llm", "latency_ms": 5}

    chooser = ValueChooser({"email": "qa@example.com"}, post=lambda *a: answer(WRITE, ["email", WRITE]), write=write)
    text, helper = chooser(CONTEXT, {"label": "Search"}, {})
    assert text == "free text" and helper["value_choice"] == WRITE and written == [CONTEXT]


def test_chooser_without_values_uses_text_llm_directly():
    chooser = ValueChooser({}, post=None, write=lambda c: ("x", {"model": "m"}))
    assert chooser(CONTEXT, {"label": "Search"}, {}) == ("x", {"model": "m"})


def test_chooser_rejects_invalid_model_answer():
    bad = {"answers": {"value": {"choice": "nope", "probabilities": {}, "confidence": 1}}}
    with pytest.raises(ValueError):
        ValueChooser({"email": "e"}, post=lambda *a: bad, write=None)(CONTEXT, {"label": "Email"}, {})


def test_engine_outbound_filter_redacts_every_request(monkeypatch):
    captured = []

    class Response:
        status_code, is_error = 200, False

        @staticmethod
        def json():
            return {"ok": True}

    monkeypatch.setattr(model.CLIENT, "post", lambda url, json, headers: captured.append(json) or Response())
    redactor = Redactor({"QA_PASSWORD": 'hun"ter2'})
    model.OUTBOUND_FILTERS.append(redactor.apply)
    try:
        model.post_json("https://x", "k", {"recent_actions": [{"text": 'hun"ter2'}], "n": 1})
    finally:
        model.OUTBOUND_FILTERS.remove(redactor.apply)
    assert captured == [{"recent_actions": [{"text": "[secret:QA_PASSWORD]"}], "n": 1}]


def test_engine_endpoint_resolution(monkeypatch):
    monkeypatch.delenv("TYPESAFE_URL")
    monkeypatch.delenv("TYPESAFE_MODEL")
    assert model.typesafe_url() == "https://api.typesafe.ai/v1/systemone"
    assert model.typesafe_model() == "jev-latest"


def test_engine_snapshot_masks_password_values():
    from pathlib import Path

    source = (Path(model.__file__).with_name("snapshot.js")).read_text(encoding="utf-8")
    assert "!['file','hidden'].includes(e.type)" in source  # password inputs are offered as targets
    assert "String(shown(e))" in source  # ...but their value is reported masked
    assert "secret(e)?e.value.length:e.value" in source  # fingerprints use length only
    assert "'tel','password'].includes(e.type)) return 'textbox'" in source  # and they get a textbox role
    assert "kind:'submit'" in source and "'Press Enter in '" in source  # P12: inputs can be submitted with Enter


CARDS = """<!doctype html><html><body><style>
.card{display:inline-flex;padding:12px;margin:4px;cursor:pointer}.card input{position:absolute;opacity:0;width:1px}
.tile{display:inline-block;padding:12px;margin:4px;cursor:pointer}</style>
<h2>What describes you best?</h2>
<label class="card"><input type="radio" name="role"><span>Founder / Indie Hacker</span></label>
<label class="card"><input type="radio" name="role" checked><span>Marketer</span></label>
<h2>How big is your team?</h2><div class="tile" onclick="0">Just me</div>
<div style="cursor:pointer" onclick="0"><a href="#x">A link inside a pointer box</a></div>
<h2>What should this film achieve?</h2><button aria-pressed="true">Explain what we do</button>
<button aria-pressed="false">Get more visitors</button>
<button disabled>Continue</button></body></html>"""


def test_answers_drawn_as_cards_are_offered_to_the_agent():
    """An onboarding whose answers are cards over hidden radios, and the
    agent was offered nothing but a greyed-out Continue: it never got past step 1 of 4. A hidden radio is offered
    through its visible label (with its checked state); a pointer-cursor tile with no control in it is a button; a
    toggle button says whether it is pressed."""
    import asyncio
    import urllib.parse
    import urllib.request
    from pathlib import Path

    import websockets

    from everyrole.chrome import chrome_session

    snapshot = (Path(model.__file__).with_name("snapshot.js")).read_text(encoding="utf-8")

    async def offered(url):
        targets = json.loads(urllib.request.urlopen(url.rstrip("/") + "/json").read())
        page = next(t for t in targets if t["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
            async def call(number, method, **params):
                await ws.send(json.dumps({"id": number, "method": method, "params": params}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == number:
                        return message
            await call(1, "Page.navigate", url="data:text/html," + urllib.parse.quote(CARDS))
            for _ in range(50):
                if (await call(2, "Runtime.evaluate", expression="document.readyState", returnByValue=True)
                        )["result"]["result"].get("value") == "complete":
                    break
                await asyncio.sleep(0.1)
            state = (await call(3, "Runtime.evaluate", expression=snapshot, returnByValue=True))["result"]["result"]
            clicks = [a for a in state["value"]["actions"] if a["kind"] == "click"]
            return [(a["role"], a["label"], a.get("checked") or a.get("pressed")) for a in clicks]

    with chrome_session() as chrome:
        actions = asyncio.run(offered(chrome.url))
    assert ("radio", "Founder / Indie Hacker", "false") in actions and ("radio", "Marketer", "true") in actions
    assert ("button", "Just me", None) in actions
    assert not any(label == "A link inside a pointer box" and role == "button" for role, label, _ in actions)
    # a chat: answers are toggles (aria-pressed); the agent must see which it picked,
    # or it presses a picked one again, which un-picks it (P17)
    assert ("button", "Explain what we do", "true") in actions and ("button", "Get more visitors", "false") in actions


LONG_PAGE = """<!doctype html><html><body style="margin:0"><h1>Top</h1><div style="height:6000px"></div>
<footer><a href="#about">About</a></footer></body></html>"""


def test_a_long_page_offers_a_jump_to_its_footer_and_back():
    """Reaching a footer's "About" took 14 scrolls, one model call and one settle
    each. A page more than a screen and a half long offers a jump to its bottom, and once down there, back to its top
    (P19); a short page offers neither."""
    import asyncio
    import urllib.parse
    import urllib.request
    from pathlib import Path

    import websockets

    from everyrole.chrome import chrome_session

    snapshot = (Path(model.__file__).with_name("snapshot.js")).read_text(encoding="utf-8")

    async def scroll_ids(url, html, at_bottom=False):
        targets = json.loads(urllib.request.urlopen(url.rstrip("/") + "/json").read())
        page = next(t for t in targets if t["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
            async def call(number, method, **params):
                await ws.send(json.dumps({"id": number, "method": method, "params": params}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == number:
                        return message
            await call(1, "Page.navigate", url="data:text/html," + urllib.parse.quote(html))
            for _ in range(50):
                if (await call(2, "Runtime.evaluate", expression="document.readyState", returnByValue=True)
                        )["result"]["result"].get("value") == "complete":
                    break
                await asyncio.sleep(0.1)
            if at_bottom:
                await call(3, "Runtime.evaluate", expression="window.scrollTo(0, document.body.scrollHeight)")
            state = (await call(4, "Runtime.evaluate", expression=snapshot, returnByValue=True))["result"]["result"]
            return {a["id"]: a for a in state["value"]["actions"] if a["kind"] == "scroll"}

    with chrome_session() as chrome:
        top = asyncio.run(scroll_ids(chrome.url, LONG_PAGE))
        bottom = asyncio.run(scroll_ids(chrome.url, LONG_PAGE, at_bottom=True))
        short = asyncio.run(scroll_ids(chrome.url, "<h1>Short</h1>"))
    assert {"scroll_down", "scroll_end"} <= set(top) and "scroll_start" not in top
    assert top["scroll_end"]["delta"] > top["scroll_down"]["delta"] * 3  # one move reaches the footer
    assert {"scroll_up", "scroll_start"} <= set(bottom) and "scroll_end" not in bottom
    assert not short


def _engine_agent(spots):
    """An Agent with no browser whose page lands, after each action, on the given (url, scroll y) spots."""
    from jev_ultrafast.agent import Agent

    agent = Agent.__new__(Agent)
    agent.state = {"page": {}}
    landed = []
    for url, y in spots:
        agent.state["page"] = {"url": url, "scroll": {"y": y}, "h": 800}
        landed.append(agent._in_circles({"kind": "scroll"}))
    return landed


def test_scrolling_up_and_down_in_circles_stops_the_test():
    """automationexercise.com (staging, 2026-11-17): failing tests scrolled down, up, down, up... until their 40 or
    80 step budget ran out, one model call a step. Coming back to spots already reached twice, twice, stops (P20)."""
    oscillating = [("/p", 0), ("/p", 800), ("/p", 0), ("/p", 800), ("/p", 0), ("/p", 800)]
    assert _engine_agent(oscillating) == [False, False, False, False, False, True]
    # going all the way down then all the way back up visits each spot twice at most: never stopped
    down_and_back = [("/p", y) for y in (640, 1280, 1920, 2560, 3200, 2560, 1920, 1280, 640, 0)]
    assert not any(_engine_agent(down_and_back))
    # back a few pixels off where it was (a carousel, lazy images) is the same part of the page
    assert _engine_agent([("/p", y) for y in (0, 640, 12, 655, 7, 630)]) == [False] * 5 + [True]
    # the same scroll position on another page is another spot
    assert not any(_engine_agent([("/a", 0), ("/b", 0), ("/c", 0), ("/a", 800), ("/b", 800)]))


AD_SHOP = """<!doctype html><html><body style="margin:0"><h1>Shop</h1><a id="next" href="/next">Next page</a>
<script>
const q = new URLSearchParams(location.search), ad = q.get('ad');
let to = null, layer = null;
const shut = () => { if (layer) { layer.remove(); layer = null; } };
document.getElementById('next').addEventListener('click', ev => {
  if (!ad) return;
  ev.preventDefault(); to = ev.currentTarget.href;
  if (q.get('push')) history.pushState({}, '', '#cover');
  layer = document.createElement('div');
  layer.style.cssText = 'position:fixed;inset:0;z-index:2147483647';
  const host = ad === 'own' ? location.host : 'localhost:' + location.port;
  layer.innerHTML = '<iframe src="http://' + host + '/ad?close=' + (q.get('close') || '') +
    '" style="width:100%;height:100%;border:0"></iframe>';
  document.body.appendChild(layer);
});
addEventListener('message', e => { if (e.data === 'close') { shut(); location.href = to; } });
addEventListener('popstate', shut);
</script></body></html>"""

AD_FRAME = """<!doctype html><html><body style="margin:0;background:#fff"><p>Buy our things</p>
<a href="/bought" target="_top">Learn more</a>{close}</body></html>"""
AD_CLOSE = ('<div role="button" aria-label="Close ad" onclick="parent.postMessage(`close`, `*`)" '
            'style="position:absolute;top:20px;right:20px;width:40px;height:30px;cursor:pointer">Close</div>')

AD_WORKER = r"""
import json, sys, time
from jev_ultrafast.browser import Browser
from everyrole import interstitial

def until(check, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not check():
        time.sleep(0.1)

out = {}
for case in sys.argv[2:]:
    b = Browser(sys.argv[1] + "/?" + case, isolated=True)
    try:
        page = b.observe(screenshot=False)
        b.act(next(a for a in page["actions"] if a["label"] == "Next page"), page)
        until(lambda: b.evaluate("document.querySelector('iframe')?.contentWindow?.length === 0"), 5)
        time.sleep(0.5)  # the frame's page has loaded
        by = next(a.get("covered_by") for a in b.observe(screenshot=False)["actions"] if a["label"] == "Next page")
        answer = interstitial.close(b)
        until(lambda: "/next" in (b.evaluate("location.href") or ""), 3 if answer["how"] == "close control" else 0)
        out[case] = {"answer": answer, "url": b.evaluate("location.href"), "by": by}
    finally:
        b.close()
print(json.dumps(out), flush=True)
"""


def test_a_full_page_window_from_another_site_is_closed_like_a_person_would(tmp_path):
    """automationexercise.com (2026-11-17): after a click, an ad network laid a frame from its own site over the whole
    page (the address gaining "#google_vignette"); the agent can't see inside it, every control was under it, and every
    journey stopped at step 1 or 2. Such a cover is closed by its own close control (the click then goes on to the
    next page), else by going Back when it put itself in the history; one with neither is reported, never pressed
    elsewhere; and a frame of the app's own site is never one (P21). Real Chrome, two sites on this machine."""
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from everyrole import daemons
    from everyrole.chrome import chrome_session

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            body = (AD_FRAME.format(close=AD_CLOSE if "close=1" in query else "") if path == "/ad" else
                    "<!doctype html><h1>The next page</h1>" if path == "/next" else
                    "<!doctype html><h1>Bought</h1>" if path == "/bought" else AD_SHOP).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / "daemons"
    home.mkdir()
    cases = ["ad=other&close=1", "ad=other&push=1", "ad=other", "ad=own&close=1"]
    try:
        with chrome_session() as chrome:
            env = {**os.environ, "BU_CDP_URL": chrome.url, "BU_NAME": f"ad-cover-{os.getpid()}",
                   daemons.ENV: str(home)}
            try:
                done = subprocess.run([sys.executable, "-c", AD_WORKER, f"http://127.0.0.1:{server.server_port}",
                                       *cases], env=env, capture_output=True, text=True, timeout=120)
            finally:
                daemons.stop(home)
    finally:
        server.shutdown()
        server.server_close()
    assert done.returncode == 0, done.stderr[-2000:]
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    closed = seen["ad=other&close=1"]
    # its own close control, clicked where a person would: the click it held back goes on to the next page
    assert closed["answer"] == {"found": True, "closed": True, "by": "localhost", "how": "close control"}
    assert closed["url"].endswith("/next")
    assert closed["by"] == "a window from localhost"  # what the agent is told covers the link
    back = seen["ad=other&push=1"]  # no close control, but it put itself in the history: Back closes it
    assert back["answer"]["closed"] is True and back["answer"]["how"] == "back"
    assert back["url"].endswith("/?ad=other&push=1")
    stuck = seen["ad=other"]  # neither: reported as not closed, and nothing in it pressed ("Learn more" stays)
    assert stuck["answer"] == {"found": True, "closed": False, "by": "localhost", "how": ""}
    assert "/bought" not in stuck["url"]
    assert seen["ad=own&close=1"]["answer"]["found"] is False  # the app's own frame: never closed for it


WRAPPED = """<!doctype html><html><body style="margin:0"><div style="width:120px;font:20px sans-serif">
<h4 style="margin:0;line-height:40px">
<a href="#picked" id="p" onclick="document.title='picked'">Samsung galaxy s6</a></h4></div></body></html>"""


def test_a_link_whose_words_wrap_onto_two_lines_is_clicked(tmp_path):
    """demoblaze (2026-11-18): "Samsung galaxy s6" wraps onto two lines; the middle of its outer box is between them,
    on the heading, so every click was refused as covered (40 decisions dropped, the main path never passed step 1).
    The click lands on one of the link's own lines (P22)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from everyrole import daemons
    from everyrole.chrome import chrome_session

    script = (
        "import json, sys, urllib.parse\n"
        "from jev_ultrafast.browser import Browser\n"
        "b = Browser('data:text/html,' + urllib.parse.quote(sys.argv[1]), isolated=True)\n"
        "page = b.observe(screenshot=False)\n"
        "link = next(a for a in page['actions'] if a['label'] == 'Samsung galaxy s6')\n"
        "b.act(link, page)\n"
        "import time; time.sleep(0.5)\n"
        "print(json.dumps(b.evaluate('document.title')))\n"
        "b.close()\n")
    home = tmp_path / "daemons"
    home.mkdir()
    with chrome_session() as chrome:
        import everyrole

        code = os.pathsep.join(str(p) for p in (Path(everyrole.__file__).parents[1], Path(model.__file__).parents[1]))
        env = {**os.environ, "BU_CDP_URL": chrome.url, "BU_NAME": f"sw-test-wrap-{os.getpid()}",
               daemons.ENV: str(home), "PYTHONPATH": code}  # this checkout's engine, in the child too
        try:
            done = subprocess.run([sys.executable, "-c", script, WRAPPED], env=env, capture_output=True, text=True,
                                  timeout=90)
        finally:
            daemons.stop(home)
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip().splitlines()[-1] == '"picked"', done.stdout


SHOP = """<!doctype html><html><body><style>.btn{cursor:pointer;padding:6px;display:inline-block}</style>
<div class="card"><p>Blue Top</p><a class="btn" data-id="1">Add to cart</a></div>
<a>Just words, not clickable</a></body></html>"""


def test_a_link_with_no_address_drawn_as_a_button_is_offered():
    """automationexercise.com (2026-11-18): "Add to cart" is <a class="btn"> with no href and a script behind it; the
    agent was never offered it and scrolled until its budget ran out. A link with no address and a pointer cursor is
    offered as a button (P23); one with no pointer cursor is not."""
    import asyncio
    import urllib.parse
    import urllib.request
    from pathlib import Path

    import websockets

    from everyrole.chrome import chrome_session

    snapshot = (Path(model.__file__).with_name("snapshot.js")).read_text(encoding="utf-8")

    async def offered(url):
        targets = json.loads(urllib.request.urlopen(url.rstrip("/") + "/json").read())
        page = next(t for t in targets if t["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
            async def call(number, method, **params):
                await ws.send(json.dumps({"id": number, "method": method, "params": params}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == number:
                        return message
            await call(1, "Page.navigate", url="data:text/html," + urllib.parse.quote(SHOP))
            for _ in range(50):
                if (await call(2, "Runtime.evaluate", expression="document.readyState", returnByValue=True)
                        )["result"]["result"].get("value") == "complete":
                    break
                await asyncio.sleep(0.1)
            state = (await call(3, "Runtime.evaluate", expression=snapshot, returnByValue=True))["result"]["result"]
            return [(a["role"], a["label"]) for a in state["value"]["actions"] if a["kind"] == "click"]

    with chrome_session() as chrome:
        actions = asyncio.run(offered(chrome.url))
    assert ("button", "Add to cart") in actions
    assert not any(label == "Just words, not clickable" for _, label in actions)

def test_a_decision_too_big_for_the_model_is_asked_again_smaller(monkeypatch):
    """P24: a page over the decision model's window is refused with HTTP 400 (automationexercise.com, 2026-11-18:
    2 tests stopped as "machine problem"). It is asked again with less page text and no duplicated dropdown options;
    every choice stays."""
    sent = []

    def post(url, key, body, timeout=None):
        sent.append(body)
        if len(sent) == 1:
            raise model.ModelRefused(400)
        target = body["questions"]["select_target"]["criteria"]
        pick = next(iter(target))
        return {"answers": {"operation": {"choice": "SELECT", "confidence": 0.9,
                                          "probabilities": {k: (1.0 if k == "SELECT" else 0.0)
                                                            for k in body["questions"]["operation"]["criteria"]}},
                            "select_target": {"choice": pick, "confidence": 0.9,
                                              "probabilities": {k: (1.0 if k == pick else 0.0) for k in target}}},
                "model": "m"}

    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    years = [{"id": f"y{n}", "kind": "select", "node": "years", "label": str(n), "value": str(n)}
             for n in range(1900, 2022)]
    state = {"url": "https://shop.example/signup", "title": "Sign up", "text": "words " * 5000, "actions": years}
    decision = model.choose(state, "Fill the form", [{"action": "a", "kind": "click", "text": "", "page_changed": 1}])
    first, second = sent
    assert decision["operation"] == "SELECT" and len(sent) == 2
    assert len(second["state"]["page"]["text"]) == len(first["state"]["page"]["text"]) // 2
    assert "options" in first["state"]["elements"][0] and "options" not in second["state"]["elements"][0]
    assert second["questions"] == first["questions"]  # what the model may choose never shrinks
    assert len(json.dumps(second)) < len(json.dumps(first))


def test_a_refusal_other_than_too_big_is_not_asked_again(monkeypatch):
    calls = []

    def post(url, key, body, timeout=None):
        calls.append(body)
        raise model.ModelRefused(403)

    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    state = {"url": "u", "title": "t", "text": "x",
             "actions": [{"id": "b", "kind": "click", "node": "n", "label": "Go"}]}
    with pytest.raises(model.ModelRefused):
        model.choose(state, "go", [])
    assert len(calls) == 1

