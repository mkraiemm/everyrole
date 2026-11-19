"""Journeys (flow `steps`): several goals done one after the other in ONE browser, each step judged by its own
checks on a settled page, a reload before a step's checks when it asks, a visitor who signs in partway, and the
"count" check that proves something happened once and only once."""

import base64

import pytest

from everyrole import assertions
from everyrole.errors import SpecError
from everyrole.runner import FlowRunner
from everyrole.spec import Config, Persona, parse_flow, parse_flows

ENV = {"PAID_EMAIL": "paid@example.com", "PAID_PASSWORD": "hunter2-secret"}
BASE = "https://films.example.com"


class Browser:
    """A page whose text the steps change; a reload shows `after_reload` when set (what really survived)."""

    def __init__(self):
        self.url, self.text, self.after_reload, self.reloads, self.pictures = f"{BASE}/start", "Start", None, 0, 0

    def page(self):
        return {"url": self.url, "title": "t", "text": self.text, "actions": [], "fingerprint": self.text}

    def evaluate(self, expression):
        if 'type="password"' in expression:
            return 0
        return self.url if "location" in expression else self.text

    def observe(self, screenshot=False):
        return self.page()

    def call(self, method, **_):
        if method == "Page.reload":
            self.reloads += 1
            if self.after_reload is not None:
                self.text = self.after_reload
            return {}
        assert method == "Page.captureScreenshot"
        self.pictures += 1
        return {"data": base64.b64encode(b"jpeg").decode()}


class JourneyAgent:
    """Plays the script of whichever goal it was last given, through the same state the real engine keeps."""

    def __init__(self, scripts, url, goal, *, max_steps, text_provider, before_execute, browser_options=None,
                 after_action=None, stop_needs_fresh_page=True, fill_before_submit=False):
        self.scripts, self.max_steps, self.text_provider = scripts, max_steps, text_provider
        self.before_execute = before_execute
        self.browser, self.screenshots = Browser(), False
        self.goals = []
        self.state = {"goal": goal, "plan": [goal], "plan_index": 0, "status": "ready", "decision": None,
                      "history": [], "decisions": [], "text_calls": [], "page": self.browser.page(), "elapsed_ms": 0}
        self.closed = False

    def snapshot(self):
        return {**self.state, "page": self.browser.page()}

    def run(self):
        goal = self.state["goal"]
        self.goals.append(goal)
        # a step's goal can carry EveryRole's own hint after it ("... (log in from the page that asks...)")
        script = self.scripts.get(goal) or next((s for g, s in self.scripts.items() if goal.startswith(g)), [])
        for step in script:
            action = {"label": step["label"], "kind": step.get("kind", "click")}
            text = None
            if action["kind"] == "fill":
                text, _ = self.text_provider({"field": {"label": step["label"]}}, action, self.browser.page())
            self.before_execute(action, self.browser.page(), text)
            self.browser.url = step.get("url", self.browser.url)
            self.browser.text = step.get("text", self.browser.text)
            if "after_reload" in step:
                self.browser.after_reload = step["after_reload"]
            self.state["decisions"].append({"usage": {"input_tokens": 10}})
            self.state["history"].append({"step": len(self.state["history"]) + 1, "action": action["label"],
                                          "kind": action["kind"], "text": text, "url": self.browser.url})
            yield self.snapshot()
        self.state["status"] = "done"

    def close(self):
        self.closed = True


class Values:
    def __init__(self, values, secret_literals):
        self.values = values

    def __call__(self, context, action, page):
        name = next(n for n in self.values if n in action["label"].lower())
        return self.values[name], {"model": "values", "value_choice": name}


def runner_for(scripts):
    agents = []
    ticks = iter(i * 0.25 for i in range(100000))
    runner = FlowRunner(agent_factory=lambda url, goal, **hooks: agents.append(
                            JourneyAgent(scripts, url, goal, **hooks)) or agents[-1],
                        outbound_filters=lambda: [], value_chooser=Values, clock=lambda: next(ticks),
                        sleep=lambda s: None, wait_stable=lambda *a, **k: None)
    return runner, agents


CONFIG = Config(personas={"paid": Persona(name="paid", values={"email": "{{secrets.PAID_EMAIL}}",
                                                               "password": "{{secrets.PAID_PASSWORD}}"})})

MAKE_IT = {  # a sign-up wall at "Make it": chat logged out, sign in, come back, the film starts once
    "name": "make-it-after-login",
    "start": "{{BASE_URL}}/start",
    "steps": [
        {"goal": "Answer the start chat about this site", "expect": [{"text_visible": "Recap"}]},
        {"goal": "Press Make it and sign in", "sign_in_as": "paid",
         "expect": [{"text_visible": "Your answers"}, {"url_contains": "/start"}]},
        {"reload": True, "expect": [{"count": {"text": "Film started", "equals": 1}}]},
    ],
}

SCRIPTS = {
    "Answer the start chat about this site": [{"label": "Send", "text": "Recap: a film for films.example.com"}],
    "Press Make it and sign in": [
        {"label": "Make it", "url": f"{BASE}/login", "text": "Sign in"},
        {"label": "Email", "kind": "fill"}, {"label": "Password", "kind": "fill"},
        {"label": "Sign in", "url": f"{BASE}/start", "text": "Your answers · Film started",
         "after_reload": "Your answers · Film started"},
    ],
}


def journey(raw=None, **overrides):
    return parse_flows({**(raw or MAKE_IT), **overrides}, config=CONFIG, base_url=BASE, env=ENV)[0]


def test_a_journey_runs_each_step_in_one_browser_and_proves_each_one(tmp_path):
    runner, agents = runner_for(SCRIPTS)
    result = runner.run(journey(), output_dir=tmp_path)
    assert result.passed and result.status == "passed" and result.failed_step == 0
    assert len(agents) == 1  # one browser for the whole journey: the session and the draft carry over
    assert agents[0].goals[0] == "Answer the start chat about this site"
    assert agents[0].goals[1].startswith("Press Make it and sign in (log in from the page that asks for it")
    assert result.steps_judged[1]["goal"] == "Press Make it and sign in"  # people read the goal, not the hint
    assert [s["passed"] for s in result.steps_judged] == [True, True, True]
    assert result.steps_judged[2]["goal"] == "Reload the page" and agents[0].browser.reloads == 1
    assert result.steps_judged[2]["expectations"][0]["detail"] == "found 1 time"
    for step in result.steps_judged:  # a picture of the page each step was judged on: its proof
        assert step["picture"].endswith(f".step{step['step']}.jpg") and (tmp_path / step["picture"]).exists()
    assert "hunter2-secret" not in str(result.as_dict())  # the sign-in values stay secret


def test_the_first_failing_step_stops_the_journey_and_says_which(tmp_path):
    scripts = {**SCRIPTS, "Press Make it and sign in": [
        {"label": "Make it", "url": f"{BASE}/login", "text": "Sign in"},
        {"label": "Email", "kind": "fill"}, {"label": "Password", "kind": "fill"},
        {"label": "Sign in", "url": f"{BASE}/start", "text": "Start a new film"},  # the draft was lost
    ]}
    runner, agents = runner_for(scripts)
    result = runner.run(journey(), output_dir=tmp_path)
    assert not result.passed and result.failed_step == 2
    assert [s["step"] for s in result.steps_judged] == [1, 2]  # step 3 never ran
    assert result.steps_judged[1]["expectations"][0] == {
        "kind": "text_visible", "expected": "Your answers", "passed": False, "detail": "not found in page text"}
    assert agents[0].browser.reloads == 0


def test_a_failed_middle_step_is_never_turned_into_a_pass(tmp_path):
    """A one-goal test whose checks hold at the end passes even if the agent gave up; a journey stopped halfway
    must not: its later steps were never proven."""
    raw = {**MAKE_IT, "steps": [
        {"goal": "Answer the start chat about this site", "expect": [{"text_visible": "Something else"}]},
        {"goal": "Press Make it and sign in", "expect": [{"text_visible": "Recap"}]},  # holds on step 1's page
    ]}
    runner, _ = runner_for(SCRIPTS)
    result = runner.run(journey(raw), output_dir=tmp_path)
    assert not result.passed and result.failed_step == 1 and result.status == "failed_expect"


def test_a_reload_shows_what_really_happened_once(tmp_path):
    scripts = {**SCRIPTS, "Press Make it and sign in": [
        *SCRIPTS["Press Make it and sign in"][:3],
        {"label": "Sign in", "url": f"{BASE}/start", "text": "Your answers · Film started",
         "after_reload": "Your answers · Film started · Film started"},  # two films after a reload: started twice
    ]}
    runner, _ = runner_for(scripts)
    result = runner.run(journey(), output_dir=tmp_path)
    assert not result.passed and result.failed_step == 3
    assert result.steps_judged[2]["expectations"][0]["detail"] == "found 2 times"
    assert result.steps_judged[2]["expectations"][0]["expected"] == "“Film started” exactly 1 time"


def test_signing_in_partway_gives_the_agent_that_user_types_values():
    flow = journey()
    assert flow.persona == "" and flow.values["email"] == "paid@example.com"  # starts as a visitor
    assert flow.goal == "Answer the start chat about this site" and flow.expect == (
        {"count": {"text": "Film started", "equals": 1}},)  # the last step's checks are the journey's final ones
    assert [s["sign_in_as"] for s in flow.steps] == ["", "paid", ""]


def test_a_journey_is_steps_or_one_goal_never_both():
    with pytest.raises(SpecError):
        journey(goal="Do everything")
    with pytest.raises(SpecError):  # a step does something or reloads
        journey(steps=[{"expect": [{"text_visible": "Recap"}]}])
    with pytest.raises(SpecError):  # signing in as a user type the app doesn't have
        journey(steps=[{"goal": "Sign in", "sign_in_as": "admin", "expect": [{"text_visible": "Hi"}]}])
    assert parse_flow({"name": "one-goal", "start": "{{BASE_URL}}/", "goal": "Open the home page",
                       "expect": [{"text_visible": "Films"}]}, base_url=BASE).steps == ()


@pytest.mark.parametrize("bounds, page, passed", [
    ({"equals": 1}, "Film started", True),
    ({"equals": 1}, "Film started\nfilm  STARTED", False),  # spaces and case don't matter
    ({"at_least": 2}, "Film started film started", True),
    ({"at_most": 0}, "Nothing here", True),
    ({"at_least": 1, "at_most": 2}, "Film started", True),
])
def test_count_checks(bounds, page, passed):
    [check] = assertions.build([{"count": {"text": "Film started", **bounds}}])
    outcome = check.check(assertions.PageFacts(url="", text=page, labels=()))
    assert outcome.passed is passed and outcome.kind == "count"


def test_count_checks_read_numbers_as_hash():
    [check] = assertions.build([{"count": {"text": "Film #", "equals": 2}}])
    assert check.check(assertions.PageFacts(url="", text="Film 1 · Film 22", labels=())).passed


def test_the_replay_names_each_step_where_it_was_judged():
    from everyrole.video import timeline

    marks = timeline([{"action": "Make it", "kind": "click", "executed_ms": 900}], 3000,
                     judged=[{"step": 1, "goal": "Answer the chat", "passed": True, "at_ms": 500},
                             {"step": 2, "goal": "Press Make it", "passed": False, "at_ms": 2000}])
    assert [m["label"] for m in marks] == ["Start", "Step 1 passed: Answer the chat", "Clicked “Make it”",
                                           "Step 2 failed: Press Make it", "Checks judged on this page"]


def test_a_long_test_name_still_gets_a_browser_worker():
    import re

    from everyrole.suite import worker_name

    long_a = worker_name("20261002-120000-abcdef", "change-visitor-reloads-signup-page-keeps-return-address@paid")
    long_b = worker_name("20261002-120000-abcdef", "change-visitor-reloads-signup-page-keeps-return-address@free")
    for name in (long_a, long_b):
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)  # what the browser daemon accepts
    assert long_a != long_b and worker_name("r1", "home@paid") == "sw-r1-home-paid"


def test_a_step_whose_checks_held_before_it_acted_proves_nothing(tmp_path):
    """A walk weakened "open the checkout" to the app's name, on every page; it passed on an
    app with no checkout. A step whose checks all held before it did anything fails as the test's mistake."""
    from everyrole import verdict

    # "/start": the address before and after (like an app's name on every page): it held, and still holds
    weak = {"goal": "Press Make it and sign in", "sign_in_as": "paid", "expect": [{"url_contains": "/start"}]}
    runner, _ = runner_for(SCRIPTS)
    result = runner.run(journey({**MAKE_IT, "steps": [MAKE_IT["steps"][0], weak]}), output_dir=tmp_path)
    assert not result.passed and result.failed_step == 2 and result.cause == verdict.SETUP
    assert result.steps_judged[1]["held_before"] is True and "already showed before the step" in result.error


def test_a_last_step_out_of_moves_whose_checks_held_before_proves_nothing(tmp_path):
    """"Open the checkout" checked by "$12, one time", printed on the
    pricing page it started on. The agent wandered home, ran out of moves, and a fresh read passed it."""
    from everyrole import verdict

    class OutOfMoves(JourneyAgent):
        def run(self):
            yield from super().run()
            if self.state["goal"].startswith("Open the checkout"):  # it never found one
                self.state["history"] += [{"step": n, "action": "Scroll"} for n in range(self.max_steps)]
                self.state["status"] = "blocked"

    pricing = {"goal": "Answer the start chat about this site", "expect": [{"text_visible": "Recap"}]}
    checkout = {"goal": "Open the checkout", "expect": [{"text_visible": "Recap"}]}
    agents = []
    ticks = iter(i * 0.25 for i in range(100000))
    runner = FlowRunner(agent_factory=lambda url, goal, **hooks: agents.append(
                            OutOfMoves(SCRIPTS, url, goal, **hooks)) or agents[-1],
                        outbound_filters=lambda: [], value_chooser=Values, clock=lambda: next(ticks),
                        sleep=lambda s: None, wait_stable=lambda *a, **k: None)
    result = runner.run(journey({**MAKE_IT, "steps": [pricing, checkout]}), output_dir=tmp_path)
    assert not result.passed and result.failed_step == 2 and result.cause == verdict.SETUP
    assert result.steps_judged[1]["held_before"] is True


def test_a_middle_step_whose_checks_held_before_is_proven_by_the_next(tmp_path):
    """"Type the website address" checked by its own field held before it typed; typing changes
    nothing else, so the next step proves it. Only the journey's last step must prove itself."""
    typing = {"goal": "Answer the start chat about this site", "expect": [{"url_contains": "/start"}]}
    runner, _ = runner_for(SCRIPTS)
    result = runner.run(journey({**MAKE_IT, "steps": [typing, *MAKE_IT["steps"][1:]]}), output_dir=tmp_path)
    assert result.passed and result.steps_judged[0]["proven_by_next"] is True
