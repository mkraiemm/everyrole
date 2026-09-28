"""Machine problems are never shown as app failures."""

import tempfile

import httpx

from everyrole import reachability, report, verdict
from everyrole.cli import exit_code
from everyrole.spec import Config, Flow, Persona
from everyrole.suite import SuiteRunner


def flow_result(name, *, passed=False, status="failed_expect", cause=None, **extra):
    result = {"name": name, "passed": passed, "status": "passed" if passed else status, "duration_ms": 1000,
              "steps": [], "expectations": [], "warnings": [], **extra}
    if cause:
        result["cause"] = cause
    return result


def test_causes_decide_the_verdict():
    assert verdict.of(flow_result("a", passed=True)) == verdict.PASSED
    assert verdict.of(flow_result("a", cause="app")) == verdict.FAILED
    assert verdict.of(flow_result("a", cause="machine")) == verdict.NOT_CHECKED
    assert verdict.of(flow_result("a", cause="skipped")) == verdict.NOT_RUN
    assert verdict.of(flow_result("a", cause="setup")) == verdict.FAILED
    # results written before causes existed: an exception inside the run = machine
    assert verdict.of(flow_result("a", status="error")) == verdict.NOT_CHECKED
    assert verdict.of({"observed": "error", "passed": False}) == verdict.NOT_CHECKED


def summary_of(results, access=()):
    return report.summarize({"results": results, "access": list(access), "duration_ms": 1}, base_url="b",
                            environment="e")


def test_machine_problem_is_not_a_failure_in_the_report():
    results = [flow_result("ok", passed=True),
               flow_result("slow", status="error", cause="machine", error="Page.captureScreenshot timed out")]
    access = [{"path": "/admin", "persona": "free", "expected": "deny", "observed": "error", "passed": False,
               "detail": "could not open", "cause": "machine"}]
    summary = summary_of(results, access)
    assert (summary["failed"], summary["not_checked"]) == (0, 2)
    md = report.markdown(summary, results, access)
    assert "❌" not in md
    assert "⚠️ 2 of 3 could not be checked (machine problem, not your app)" in md
    assert "| `slow` | ⚠️ not checked |" in md and "machine problem, not your app" in md
    assert "| `/admin` | ⚠️ not checked |" in md
    xml = report.junit(summary, results, access)
    assert 'failures="0"' in xml and 'skipped="2"' in xml and "<failure" not in xml
    assert exit_code(summary) == 0


def test_app_failure_still_fails_next_to_machine_problems():
    results = [flow_result("broken", cause="app"), flow_result("slow", status="error", cause="machine")]
    summary = summary_of(results)
    assert "❌ 1 of 2 checks failed" in report.markdown(summary, results)
    assert exit_code(summary) == 1


def test_nothing_checked_at_all_is_a_tool_error_not_an_app_failure():
    summary = summary_of([flow_result("a", status="error", cause="machine")])
    assert exit_code(summary) == 2


def test_passing_on_the_second_try_is_labelled():
    results = [flow_result("a", passed=True, attempts=2, warnings=["first try failed (x); tried again"])]
    md = report.markdown(summary_of(results), results)
    assert "✅ passed on 2nd try" in md and "tried again" in md


def make_config():
    return Config(login={"timeout_s": 60}, personas={"guest": Persona("guest", {}, False)},
                  access={"/admin": {"deny": ["guest"]}}, access_settle_s=1)


def with_spawn(spawn, **options):
    original, SuiteRunner._spawn = SuiteRunner._spawn, spawn
    try:
        runner = SuiteRunner(parallel=1, cdp_url="x", base_url="https://app.io", output_dir=tempfile.mkdtemp(),
                             env_file=".env", **options)
        return runner.run([Flow("home", "https://app.io/", "g", ({"url_contains": "/"},), source="h.json")],
                          make_config())
    finally:
        SuiteRunner._spawn = original


class Chrome:
    def __init__(self):
        self.healed = 0

    def ensure(self):
        self.healed += 1


def test_machine_problem_heals_the_browser_then_retries():
    chrome, calls = Chrome(), []

    def spawn(self, args, *, name, timeout_s, source=""):
        calls.append(name)
        if args[0] == "access":
            return [{"path": "/admin", "persona": "guest", "expected": "deny", "observed": "denied", "passed": True}]
        if calls.count(name) == 1:
            return {"name": name, "passed": False, "status": "error", "cause": "machine", "error": "IPC timeout"}
        return {"name": name, "passed": True, "status": "passed"}

    suite = with_spawn(spawn, chrome=chrome)
    (home,) = suite["results"]
    assert home["passed"] and home["attempts"] == 2 and chrome.healed == 1
    assert "could not be checked (IPC timeout)" in home["warnings"][0]


def test_app_failure_is_retried_without_healing_and_setup_errors_are_not_retried():
    chrome, calls = Chrome(), []

    def spawn(self, args, *, name, timeout_s, source=""):
        calls.append(name)
        if args[0] == "access":
            return [{"path": "/admin", "persona": "guest", "expected": "deny", "observed": "denied", "passed": True}]
        return {"name": name, "passed": False, "status": "failed_expect", "cause": "app"}

    suite = with_spawn(spawn, chrome=chrome)
    assert calls.count("home") == 2 and chrome.healed == 0 and not suite["results"][0]["passed"]

    calls.clear()

    def setup(self, args, *, name, timeout_s, source=""):
        calls.append(name)
        if args[0] == "access":
            return []
        return {"name": name, "passed": False, "status": "error", "cause": "setup", "error": "bad file"}

    with_spawn(setup)
    assert calls.count("home") == 1


def test_access_worker_that_died_twice_is_not_checked_per_page():
    def spawn(self, args, *, name, timeout_s, source=""):
        if args[0] == "access":
            return {"name": name, "passed": False, "status": "error", "cause": "machine", "error": "worker hung"}
        return {"name": name, "passed": True, "status": "passed"}

    suite = with_spawn(spawn, chrome=Chrome())
    (row,) = suite["access"]
    assert row["path"] == "/admin" and verdict.of(row) == verdict.NOT_CHECKED


def test_site_down_is_the_only_failure_and_everything_else_is_not_run():
    down = {"name": reachability.NAME, "passed": False, "status": "failed_expect", "cause": "app",
            "error": "https://app.io answered HTTP 503", "duration_ms": 0, "expectations": [], "steps": [],
            "warnings": []}

    def spawn(self, args, *, name, timeout_s, source=""):
        raise AssertionError("nothing may run when the site is down")

    suite = with_spawn(spawn, site_check=lambda url: down)
    summary = summary_of(suite["results"], suite["access"])
    assert summary["failed"] == 1 and summary["not_run"] == 2


class Answer:
    def __init__(self, status_code):
        self.status_code = status_code


def fake_get(site, reference):
    def get(url):
        outcome = site if "app.io" in url else reference
        if isinstance(outcome, Exception):
            raise outcome
        return Answer(outcome)
    return get


def test_reachability_tells_site_down_from_no_network():
    assert reachability.check("https://app.io", "https://model.example", get=fake_get(200, 200)) is None
    assert reachability.check("https://app.io", "https://model.example", get=fake_get(404, 200)) is None
    down = reachability.check("https://app.io", "https://model.example", get=fake_get(502, 401))
    assert down["cause"] == "app" and "HTTP 502" in down["error"]
    refused = httpx.ConnectError("refused")
    down = reachability.check("https://app.io", "https://model.example", get=fake_get(refused, 200))
    assert down["cause"] == "app"
    offline = reachability.check("https://app.io", "https://model.example", get=fake_get(refused, refused))
    assert offline["cause"] == "machine" and "no network" in offline["error"]


def test_access_row_that_disagrees_is_checked_again_alone():
    from everyrole.access import AccessChecker
    from everyrole.assertions import PageFacts

    opened = []

    class Tab:
        def __init__(self, url):
            self.url = url
            opened.append(url)
            # busy machine: the first time the redirect is too slow to be seen
            self.final = url if len(opened) == 1 else "https://app.io/login"

        def observe(self, screenshot=False):
            return {"url": self.final}

        def close(self):
            pass

    ticks = iter(i * 0.25 for i in range(10000))
    checker = AccessChecker(browser_factory=lambda url, **o: Tab(url),
                            facts_reader=lambda b, p: PageFacts(url=p["url"], text="", labels=()),
                            clock=lambda: next(ticks), sleep=lambda s: None,
                            page_state=lambda browser: ("loaded", True))
    (row,) = checker.run(base_url="https://app.io", persona="free", rules=[("/admin", "deny")], denied_when=[],
                         settle_s=1)
    assert len(opened) == 2 and row.passed and "checked again alone" in row.detail


def test_own_chrome_is_restarted_but_a_given_one_never_is(monkeypatch):
    from everyrole import chrome as chrome_module

    answers = iter([chrome_module.ChromeError("no answer"), {}])

    def devtools(url, timeout_s):
        outcome = next(answers)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    launched = []
    monkeypatch.setattr(chrome_module, "wait_for_devtools", devtools)
    own = chrome_module.Chrome()
    monkeypatch.setattr(own, "_launch", lambda: launched.append(own.url))
    own.ensure()
    assert launched == [own.url]  # same address: running workers keep working

    answers = iter([chrome_module.ChromeError("no answer")])
    given = chrome_module.Chrome("http://127.0.0.1:9222")
    try:
        given.ensure()
        raise AssertionError("a Chrome we did not start must not be restarted")
    except chrome_module.ChromeError:
        pass
