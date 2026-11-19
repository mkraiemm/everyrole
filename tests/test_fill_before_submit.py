"""A sign-in never sends its form with the account or password field still empty (P27).

A "Check sign-in" (2026-11-18): the email and password were right, and a person signed in with
them by hand, yet the check failed with "the page still asked for a password". Replayed with a made-up login, the
model chose "Log in" on the empty form at every decision (0.65 to 0.75), never a field: the browser refused the
incomplete form each time and the check stopped on /login/email. Now a sign-in that chooses to send its form while a
field the form needs is empty fills that field first, with the value the test account gives it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jev_ultrafast import model

# the shape of that form: an email and a password field named only by their placeholders, both required, a
# password of 8 characters at least; its values held by the page's own state and written back on every render, as
# React does (a value typed without the input events a person's typing fires is wiped)
FORM = """<!doctype html><html><body><nav><a href="/">Home</a></nav>
<form id="f"><h1>Welcome back</h1>
  <input id="email" type="email" placeholder="you@example.com" required>
  <a href="#forgot">Forgot password?</a>
  <input id="pw" type="password" placeholder="&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;" required
    minlength="8">
  <button type="submit">Log in</button></form>
<script>
const state = {email: '', pw: ''};
const render = () => { for (const k in state) { const e = document.getElementById(k);
  if (e && e.value !== state[k]) e.value = state[k]; } };
for (const k in state) document.getElementById(k).addEventListener('input', ev => { state[k] = ev.target.value; });
setInterval(render, 50);
document.getElementById('f').addEventListener('submit', ev => { ev.preventDefault();
  if (state.email === 'qa-probe@example.com' && state.pw === 'right-pass-123')
    document.body.innerHTML = '<h1>Your clips</h1><p>Signed in</p>'; });
</script></body></html>"""

# the engine, driven the way the real model drove it on that form: "Log in" at every decision until signed in
SCRIPT = r"""
import json, sys, urllib.parse
from jev_ultrafast import agent as engine

def choose(page, goal, history):
    if "Signed in" in page["text"]:
        choice = "DONE"
    else:
        choice = next(a["id"] for a in page["actions"] if a["kind"] == "click" and a["label"] == "Log in")
    return {"choice": choice, "operation": "CLICK", "target": None, "confidence": 0.7,
            "probabilities": {choice: 0.7}, "latency_ms": 0, "usage": {}}

engine.choose = choose
VALUES = {"you@example.com": "qa-probe@example.com"}

def typed(context, action, page):  # the test account's values, as EveryRole's value chooser gives them
    return VALUES.get(action["label"], "right-pass-123"), {"model": "values", "latency_ms": 0}

agent = engine.Agent("data:text/html," + urllib.parse.quote(sys.argv[1]), "Sign in", max_steps=6,
                     text_provider=typed, **({"fill_before_submit": True} if sys.argv[2] == "on" else {}))
try:
    for _ in agent.run():
        pass
    out = {"status": agent.state["status"], "text": agent.state["page"]["text"][:200],
           "history": [{k: h.get(k) for k in ("kind", "action", "instead_of")} for h in agent.state["history"]]}
finally:
    agent.close()
print(json.dumps(out), flush=True)
"""


@pytest.fixture(scope="module")
def drive(tmp_path_factory):
    """Runs SCRIPT in a child process against a throwaway headless Chrome. -> run(flag) -> its outcome."""
    from everyrole import daemons
    from everyrole.chrome import chrome_session

    home = tmp_path_factory.mktemp("daemons")
    with chrome_session() as chrome:
        import everyrole

        code = os.pathsep.join(str(p) for p in (Path(everyrole.__file__).parents[1], Path(model.__file__).parents[1]))
        env = {**os.environ, "BU_CDP_URL": chrome.url, "BU_NAME": f"fill-before-submit-{os.getpid()}",
               daemons.ENV: str(home), "PYTHONPATH": code}  # this checkout's engine, in the child too

        def run(flag):
            done = subprocess.run([sys.executable, "-c", SCRIPT, FORM, flag], env=env, capture_output=True,
                                  text=True, timeout=120)
            assert done.returncode == 0, done.stderr[-800:]
            return json.loads(done.stdout.strip().splitlines()[-1])

        try:
            yield run
        finally:
            daemons.stop(home)


def test_without_it_the_sign_in_presses_log_in_on_an_empty_form_and_never_gets_in(drive):
    before = drive("off")
    assert before["status"] == "blocked"  # three presses that changed nothing
    assert [h["kind"] for h in before["history"]] == ["click", "click", "click"]
    assert "Signed in" not in before["text"]


def test_a_sign_in_fills_the_empty_email_and_password_before_sending_its_form(drive):
    after = drive("on")
    assert after["status"] == "done" and "Signed in" in after["text"]
    # the values stuck (the page's own state took them) and the form went once both were in
    assert after["history"] == [
        {"kind": "fill", "action": "you@example.com", "instead_of": "Log in"},
        {"kind": "fill", "action": "•" * 8, "instead_of": "Log in"},
        {"kind": "click", "action": "Log in", "instead_of": None},
    ]


REACT_FORM = ('<form><input placeholder="Email"><input type="password" placeholder="Password">'
              '<input placeholder="Referral"><button>Go</button></form>')


@pytest.mark.parametrize("page,kind,label,expected", [
    # a form with no required marks (a React form checks in its own code): its account and password fields count,
    # never an optional field (a sign-up's referral code)
    (REACT_FORM, "click", "Go", ["Email", "Password"]),
    # Enter in one of its fields sends it too
    (REACT_FORM, "submit", "Press Enter in Email", ["Email", "Password"]),
    # a click into a field only puts the cursor there
    (REACT_FORM, "click", "Open Email", []),
    # filled fields don't count
    ('<form><input placeholder="Email" value="a@b.c"><input type="password" placeholder="Password">'
     '<button>Go</button></form>', "click", "Go", ["Password"]),
    # a button outside any form sends nothing
    ('<input placeholder="Email"><input type="password" placeholder="Password"><button>Go</button>', "click", "Go",
     []),
    # a plain button in a form (type=button: a "show password" eye) sends nothing
    ('<form><input type="password" placeholder="Password"><button type="button">Show</button>'
     '<button>Go</button></form>', "click", "Show", []),
    # a form with no password: only what it marks required
    ('<form><input placeholder="Name" required><input placeholder="Nickname"><button>Go</button></form>', "click",
     "Go", ["Name"]),
])
def test_the_fields_a_submit_would_send_empty(page, kind, label, expected, tmp_path):
    from everyrole import daemons
    from everyrole.chrome import chrome_session

    script = (
        "import json, sys, urllib.parse\n"
        "from jev_ultrafast.browser import Browser\n"
        "b = Browser('data:text/html,' + urllib.parse.quote(sys.argv[1]), isolated=True)\n"
        "page = b.observe(screenshot=False)\n"
        "act = next(a for a in page['actions'] if a['kind'] == sys.argv[2] and a['label'] == sys.argv[3])\n"
        "nodes = b.empty_before_submit(act)\n"
        "print(json.dumps([next(a['label'] for a in page['actions'] if a['node'] == n and a['kind'] == 'fill')\n"
        "                  for n in nodes]))\n"
        "b.close()\n")
    home = tmp_path / "daemons"
    home.mkdir()
    with chrome_session() as chrome:
        import everyrole

        code = os.pathsep.join(str(p) for p in (Path(everyrole.__file__).parents[1], Path(model.__file__).parents[1]))
        env = {**os.environ, "BU_CDP_URL": chrome.url, "BU_NAME": f"empty-fields-{os.getpid()}",
               daemons.ENV: str(home), "PYTHONPATH": code}
        try:
            done = subprocess.run([sys.executable, "-c", script, page, kind, label], env=env, capture_output=True,
                                  text=True, timeout=90)
        finally:
            daemons.stop(home)
    assert done.returncode == 0, done.stderr[-800:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == expected


def test_the_runner_turns_it_on_for_a_sign_in_only():
    from dataclasses import replace

    from tests.test_runner import flow, make_runner

    runner, _, agents = make_runner([{"label": "Sign in"}])
    runner.run(replace(flow(expect=[{"password_field_absent": True}]), is_login=True))
    assert agents[-1].fill_before_submit is True
    runner, _, agents = make_runner([{"label": "Save"}])
    runner.run(flow())
    assert agents[-1].fill_before_submit is False

