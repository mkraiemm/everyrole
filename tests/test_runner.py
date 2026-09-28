import base64

from everyrole.runner import FlowRunner
from everyrole.spec import parse_flow

ENV = {"QA_EMAIL": "qa@example.com", "QA_PASSWORD": "hunter2-secret"}


class FakeBrowser:
    def __init__(self):
        self.url, self.text, self.actions = "https://app.example.com/login", "Sign in", []
        self.passwords = 0

    def page(self):
        return {"url": self.url, "title": "t", "text": self.text, "actions": self.actions}

    def evaluate(self, expression):
        if 'type="password"' in expression:
            return self.passwords
        return self.url if "location" in expression else self.text

    def observe(self, screenshot=False):
        return self.page()

    def call(self, method, **_):
        assert method == "Page.captureScreenshot"
        return {"data": base64.b64encode(b"jpeg").decode()}


class FakeAgent:
    """Replays a script of steps through the same hooks the real engine calls."""

    def __init__(self, script, final_status, url, goal, *, max_steps, text_provider, before_execute,
                 browser_options=None, after_action=None, stop_needs_fresh_page=True):
        self.script, self.final_status = script, final_status
        self.hooks = {"text_provider": text_provider, "before_execute": before_execute}
        self.max_steps, self.browser_options = max_steps, browser_options or {}
        self.browser = FakeBrowser()
        self.history, self.decisions, self.status, self.closed = [], [], "ready", False

    def snapshot(self):
        return {
            "status": self.status,
            "history": list(self.history),
            "decisions": list(self.decisions),
            "text_calls": [],
            "page": self.browser.page(),
        }

    def run(self):
        for step in self.script:
            if isinstance(step, Exception):
                raise step
            if "raise" in step:  # the page changes, then the provider fails (e.g. HTTP 503 on the next decision)
                self.browser.url = step.get("url", self.browser.url)
                self.browser.text = step.get("text", self.browser.text)
                raise step["raise"]
            action = {"label": step["label"], "kind": step.get("kind", "click")}
            page = self.browser.page()
            self.decisions.append({"usage": {"input_tokens": 100}})
            text = None
            if action["kind"] == "fill":
                context = {"goal": "g", "field": {"label": action["label"]}, "page": page, "recent_actions": []}
                text, _ = self.hooks["text_provider"](context, action, page)
            self.hooks["before_execute"](action, page, text)
            self.browser.url = step.get("url", self.browser.url)
            self.browser.text = step.get("text", self.browser.text)
            self.history.append({"step": len(self.history) + 1, "action": action["label"], "kind": action["kind"],
                                 "text": text, "url": self.browser.url})
            yield self.snapshot()
        self.status = self.final_status

    def close(self):
        self.closed = True


class StubValues:
    """Picks the value whose name appears in the field label; never calls a model."""

    def __init__(self, values, secret_literals):
        self.values, self.secret_literals = values, secret_literals

    def __call__(self, context, action, page):
        name = next(n for n in self.values if n in action["label"].lower())
        return self.values[name], {"model": "values", "value_choice": name}


def make_runner(script, final_status="done", clock=None):
    filters, agents = [], []

    def factory(url, goal, **hooks):
        agents.append(FakeAgent(script, final_status, url, goal, **hooks))
        return agents[-1]

    ticks = iter(i * 0.25 for i in range(100000))
    runner = FlowRunner(
        agent_factory=factory,
        outbound_filters=lambda: filters,
        value_chooser=StubValues,
        clock=clock or (lambda: next(ticks)),  # fake time: settle/timeout never really wait
        sleep=lambda s: None,
    )
    return runner, filters, agents


def flow(**overrides):
    raw = {
        "name": "login",
        "start": "{{BASE_URL}}/login",
        "goal": "Log in",
        "values": {"email": "{{secrets.QA_EMAIL}}", "password": "{{secrets.QA_PASSWORD}}"},
        "expect": [{"url_contains": "/dashboard"}, {"text_visible": "Projects"}],
        "forbid": ["Delete"],
    }
    raw.update(overrides)
    return parse_flow(raw, base_url="https://app.example.com", env=ENV)


LOGIN = [
    {"label": "Email", "kind": "fill"},
    {"label": "Password", "kind": "fill"},
    {"label": "Sign in", "url": "https://app.example.com/dashboard", "text": "Projects"},
    {"label": "Should never run"},
]


def test_passes_and_stops_early_when_expectations_hold(tmp_path):
    runner, filters, agents = make_runner(LOGIN)
    result = runner.run(flow(), output_dir=tmp_path)
    assert result.status == "passed" and result.passed
    assert [s["action"] for s in result.steps] == ["Email", "Password", "Sign in"]  # early stop
    assert all(e["passed"] for e in result.expectations)
    assert agents[0].closed and filters == []  # redaction filter registered only during the run
    assert not result.screenshot


def test_secrets_never_appear_in_results():
    runner, _, _ = make_runner(LOGIN)
    dumped = str(runner.run(flow()).as_dict())
    assert "hunter2-secret" not in dumped and "qa@example.com" not in dumped
    assert "[secret:QA_PASSWORD]" in dumped


def test_redaction_filter_is_active_while_agent_runs():
    seen = []
    script = [{"label": "Email", "kind": "fill"}]
    runner, filters, _ = make_runner(script)
    original = FakeAgent.run

    def spying_run(self):
        seen.append([f("hunter2-secret") for f in filters])
        yield from original(self)

    FakeAgent.run = spying_run
    try:
        runner.run(flow())
    finally:
        FakeAgent.run = original
    assert seen == [["[secret:QA_PASSWORD]"]]


def test_forbidden_action_is_refused_before_execution(tmp_path):
    script = [{"label": "Delete project", "url": "https://app.example.com/dashboard", "text": "Projects"}]
    runner, _, agents = make_runner(script)
    result = runner.run(flow(), output_dir=tmp_path)
    assert result.status == "forbidden_action" and not result.passed
    assert agents[0].browser.url.endswith("/login")  # nothing executed
    assert result.screenshot.endswith("login.jpg")


def test_leaving_allowed_hosts_fails():
    runner, _, _ = make_runner([{"label": "Help", "url": "https://evil.example.net/"}])
    assert runner.run(flow()).status == "left_allowed_hosts"


def test_done_without_expectations_is_a_failure():
    runner, _, _ = make_runner([{"label": "Something", "url": "https://app.example.com/home"}], final_status="done")
    runner._sleep = lambda s: None
    result = runner.run(flow(settle_s=0))
    assert result.status == "failed_expect"
    assert [e["passed"] for e in result.expectations] == [False, False]


def test_settle_waits_for_redirect_after_agent_stops():
    class Redirecting(FakeBrowser):
        reads = 0

        def observe(self, screenshot=False):
            Redirecting.reads += 1
            if Redirecting.reads >= 3:  # the redirect lands on the third re-check
                self.url, self.text = "https://app.example.com/dashboard", "Projects"
            return self.page()

    ticks = iter(i * 0.25 for i in range(1000))
    runner, _, agents = make_runner([{"label": "Sign in"}], final_status="done", clock=lambda: next(ticks))
    runner._sleep = lambda s: None
    original = FakeAgent.__init__

    def init(self, *a, **k):
        original(self, *a, **k)
        self.browser = Redirecting()

    FakeAgent.__init__ = init
    try:
        result = runner.run(flow(settle_s=5))
    finally:
        FakeAgent.__init__ = original
    assert result.status == "passed"


def test_settle_zero_judges_immediately():
    runner, _, _ = make_runner([{"label": "Something"}], final_status="done")
    runner._sleep = lambda s: (_ for _ in ()).throw(AssertionError("must not sleep"))
    assert runner.run(flow(settle_s=0)).status == "failed_expect"


def test_blocked_agent_reported_as_blocked():
    runner, _, _ = make_runner([{"label": "Something"}], final_status="blocked")
    assert runner.run(flow(settle_s=0)).status == "blocked"


def test_provider_error_after_success_is_passed_with_warning():
    script = [{"label": "Sign in", "url": "https://app.example.com/dashboard", "text": "Loading"},
              {"raise": RuntimeError("Model provider returned HTTP 503; no action executed."), "text": "Projects"}]
    runner, _, _ = make_runner(script)
    result = runner.run(flow())
    assert result.status == "passed" and result.passed
    assert "503" in result.warnings[0]


def test_provider_error_before_success_is_error():
    runner, _, _ = make_runner([RuntimeError("Model provider returned HTTP 503")])
    result = runner.run(flow())
    assert result.status == "error" and "503" in result.error


def test_timeout_budget():
    ticks = iter(range(0, 1000, 50))
    runner, _, _ = make_runner([{"label": "a"}, {"label": "b"}, {"label": "c"}], clock=lambda: next(ticks))
    result = runner.run(flow(timeout_s=60))
    assert result.status == "budget_exceeded"


def test_usage_is_summed():
    runner, _, _ = make_runner(LOGIN)
    assert runner.run(flow()).input_tokens == 300


def test_expect_denied_does_not_stop_early_and_blocked_is_a_pass():
    # The Invite button is absent from the start, but the agent must still really try before we judge.
    script = [{"label": "Team settings"}, {"label": "Members"}]
    runner, _, _ = make_runner(script, final_status="blocked")
    result = runner.run(flow(expect=[{"element_absent": "Invite"}], expect_denied=True))
    assert result.status == "passed" and result.passed and result.expect_denied
    assert [s["action"] for s in result.steps] == ["Team settings", "Members"]  # no early stop


def test_expect_denied_fails_when_the_action_succeeds():
    script = [{"label": "Invite", "text": "Invitation sent"}]
    runner, _, _ = make_runner(script, final_status="done")
    result = runner.run(flow(expect=[{"text_absent": "Invitation sent"}], expect_denied=True, settle_s=0))
    assert result.status == "failed_expect" and not result.passed


def test_budget_exhaustion_counts_as_denial_only_for_denied_flows():
    script = [{"label": f"try {i}"} for i in range(3)]
    runner, _, _ = make_runner(script, final_status="blocked")
    denied = runner.run(flow(expect=[{"element_absent": "Invite"}], expect_denied=True, max_steps=3))
    assert denied.passed
    runner, _, _ = make_runner(script, final_status="blocked")
    normal = runner.run(flow(expect=[{"url_contains": "/dashboard"}], max_steps=3, settle_s=0))
    assert normal.status == "budget_exceeded"


def test_session_is_restored_and_saved(monkeypatch, tmp_path):
    import everyrole.runner as runner_module

    saved = []
    monkeypatch.setattr(runner_module, "capture", lambda browser, origins=(): {"cookies": [{"name": "sid"}]})
    monkeypatch.setattr(runner_module, "save", lambda state, path: saved.append((state, path)))
    runner, _, agents = make_runner(LOGIN)
    result = runner.run(flow(), session_state={"cookies": [{"name": "old"}]}, save_session_to=tmp_path / "s.json")
    assert result.passed and result.session_saved
    assert agents[0].browser_options["isolated"] and callable(agents[0].browser_options["prepare"])
    assert saved == [({"cookies": [{"name": "sid"}]}, tmp_path / "s.json")]


def test_failed_flow_never_saves_a_session(monkeypatch, tmp_path):
    import everyrole.runner as runner_module

    monkeypatch.setattr(runner_module, "save", lambda *a: (_ for _ in ()).throw(AssertionError("saved")))
    runner, _, _ = make_runner([{"label": "Help"}], final_status="done")
    result = runner.run(flow(settle_s=0), save_session_to=tmp_path / "s.json")
    assert not result.passed and not result.session_saved


def test_text_that_only_exists_while_loading_does_not_pass():
    # The page shows "Opening Workspace" while it loads, then the real workspace replaces it.
    script = [{"label": "Workspace", "url": "https://app.example.com/dashboard", "text": "Opening Workspace"}]
    runner, _, agents = make_runner(script, final_status="done")

    def finish_loading(browser, **_):
        browser.text = "Workspace · Projects"  # loading finished
        return True

    runner._wait_stable = finish_loading
    result = runner.run(flow(expect=[{"text_visible": "Opening Workspace"}], settle_s=0))
    assert result.status == "failed_expect" and not result.passed


def test_checks_that_hold_on_the_loaded_page_pass_and_stop_early():
    script = [{"label": "Reports", "url": "https://app.example.com/dashboard", "text": "Your reports LOADING"},
              {"label": "Should never run"}]
    runner, _, agents = make_runner(script)
    runner.agents = agents
    waited = []

    def finish_loading(browser, **_):
        waited.append(True)
        browser.text = "Your reports Launch plan Projects"
        return True

    runner._wait_stable = finish_loading
    result = runner.run(flow(expect=[{"url_contains": "/dashboard"}, {"text_visible": "Your reports"}]))
    assert result.passed and waited and [s["action"] for s in result.steps] == ["Reports"]
    assert agents_browser_text(runner) == "Your reports Launch plan Projects"  # judged on the loaded page


def agents_browser_text(runner):
    return runner.agents[0].browser.text


def test_agent_that_gives_up_above_the_fold_is_scrolled_and_resumed():
    class Scrollable(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.scrolls = 0

        def evaluate(self, expression):
            if expression.startswith("[scrollY"):
                return [self.scrolls * 600, 780, 3000]
            return super().evaluate(expression)

        def call(self, method, **params):
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseWheel":
                self.scrolls += 1
                self.text = "Projects"  # the form below the fold is now in view
                return {}
            return super().call(method, **params)

    class GivesUpOnce:
        """Like the engine: status lives in state; run() stops when it is done/blocked."""

        def __init__(self, *_a, **_k):
            self.browser, self.screenshots, self.runs = Scrollable(), False, 0
            self.browser.url = "https://app.example.com/dashboard"
            self.state = {"status": "ready"}

        def snapshot(self):
            return {"status": self.state["status"], "history": [], "decisions": [], "text_calls": [],
                    "page": self.browser.page()}

        def run(self):
            self.runs += 1
            self.state["status"] = "blocked" if self.runs == 1 else "done"
            return iter(())

        def close(self):
            pass

    agents = []
    runner = FlowRunner(agent_factory=lambda *a, **k: agents.append(GivesUpOnce()) or agents[-1],
                        outbound_filters=lambda: [], value_chooser=StubValues, sleep=lambda s: None,
                        clock=iter(i * 0.25 for i in range(10000)).__next__, wait_stable=lambda *a, **k: True)
    result = runner.run(flow(expect=[{"url_contains": "/dashboard"}, {"text_visible": "Projects"}], settle_s=0))
    assert agents[0].browser.scrolls == 1 and agents[0].runs == 2 and result.passed


def test_live_content_does_not_use_up_the_scrolls():
    """A ticker changes the page every second: after one fresh look, the next give-up must scroll."""

    class Ticking(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.scrolls, self.ticks = 0, 0
            self.url, self.text = "https://app.example.com/", "Hero"

        def observe(self, screenshot=False):
            self.ticks += 1
            return {**self.page(), "fingerprint": f"tick-{self.ticks}"}

        def evaluate(self, expression):
            if expression.startswith("[scrollY"):
                return [self.scrolls * 600, 780, 14000]
            return super().evaluate(expression)

        def call(self, method, **params):
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseWheel":
                self.scrolls += 1
                if self.scrolls == 1:
                    self.text = "Hero Paste your website URL"
                return {}
            return super().call(method, **params)

    class GivesUpUntilItSeesTheField:
        def __init__(self, *_a, **_k):
            self.browser, self.screenshots, self.runs = Ticking(), False, 0
            self.state = {"status": "ready", "page": {"fingerprint": "tick-0"}}

        def snapshot(self):
            return {"status": self.state["status"], "history": [], "decisions": [], "text_calls": [],
                    "page": self.browser.page()}

        def run(self):
            self.runs += 1
            self.state["status"] = "done" if "Paste" in self.browser.text else "blocked"
            return iter(())

        def close(self):
            pass

    agents = []
    runner = FlowRunner(agent_factory=lambda *a, **k: agents.append(GivesUpUntilItSeesTheField()) or agents[-1],
                        outbound_filters=lambda: [], value_chooser=StubValues, sleep=lambda s: None,
                        clock=iter(i * 0.25 for i in range(10000)).__next__, wait_stable=lambda *a, **k: True)
    result = runner.run(flow(expect=[{"text_visible": "Paste your website URL"}], settle_s=0))
    # blocked -> fresh look (page ticked) -> blocked -> scroll -> field in view
    assert agents[0].browser.scrolls == 1 and agents[0].runs == 3 and result.passed


def test_whole_page_controls_count_for_element_checks():
    from everyrole.runner import read_facts

    class Page:
        def evaluate(self, expression):
            if "querySelectorAll" in expression:
                return '["Create report", "Paste a link to your page"]'
            return "https://app.io/" if "location" in expression else "hero text"

    facts = read_facts(Page(), {"actions": [{"label": "Pricing"}]})
    assert {"Pricing", "Create report", "Paste a link to your page"} <= set(facts.labels)


def test_agent_stuck_at_the_bottom_goes_back_to_the_top_once():
    """What it needs (a cart icon, a menu) is often in the header: after scrolling down, look at the top."""

    class Scrolled(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.y, self.url, self.text = 2200, "https://app.example.com/inventory", "Products"

        def observe(self, screenshot=False):
            return {**self.page(), "fingerprint": f"y-{self.y}"}

        def evaluate(self, expression):
            if expression.startswith("[scrollY"):
                return [self.y, 780, 3000]  # already at the bottom
            return super().evaluate(expression)

        def call(self, method, **params):
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseWheel":
                self.y = max(0, self.y + params["deltaY"])
                if self.y == 0:
                    self.text = "Products Cart"
                return {}
            return super().call(method, **params)

    class NeedsTheHeader:
        def __init__(self, *_a, **_k):
            self.browser, self.screenshots, self.runs = Scrolled(), False, 0
            self.state = {"status": "ready", "page": {"fingerprint": "y-2200"}}

        def snapshot(self):
            return {"status": self.state["status"], "history": [], "decisions": [], "text_calls": [],
                    "page": self.browser.page()}

        def run(self):
            self.runs += 1
            self.state["status"] = "done" if "Cart" in self.browser.text else "blocked"
            return iter(())

        def close(self):
            pass

    agents = []
    runner = FlowRunner(agent_factory=lambda *a, **k: agents.append(NeedsTheHeader()) or agents[-1],
                        outbound_filters=lambda: [], value_chooser=StubValues, sleep=lambda s: None,
                        clock=iter(i * 0.25 for i in range(10000)).__next__, wait_stable=lambda *a, **k: True)
    result = runner.run(flow(expect=[{"text_visible": "Cart"}], settle_s=0))
    assert agents[0].browser.y == 0 and result.passed


def test_a_used_up_budget_is_a_result_not_a_machine_problem():
    from jev_ultrafast.agent import BudgetExceeded

    from everyrole import verdict

    runner, _, _ = make_runner([{"label": "Help"}, BudgetExceeded("Reached the model-call budget before the goal")])
    result = runner.run(flow(settle_s=0))
    assert result.status == "budget_exceeded" and not result.passed
    assert verdict.cause_of(result.as_dict()) == verdict.APP  # the goal was never reached: the test fails
