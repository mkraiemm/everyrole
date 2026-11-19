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
    assert "| `slow` | Important | ⚠️ not checked |" in md and "machine problem, not your app" in md
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

    def spawn(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
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

    def spawn(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
        calls.append(name)
        if args[0] == "access":
            return [{"path": "/admin", "persona": "guest", "expected": "deny", "observed": "denied", "passed": True}]
        return {"name": name, "passed": False, "status": "failed_expect", "cause": "app"}

    suite = with_spawn(spawn, chrome=chrome)
    assert calls.count("home") == 2 and chrome.healed == 0 and not suite["results"][0]["passed"]

    calls.clear()

    def setup(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
        calls.append(name)
        if args[0] == "access":
            return []
        return {"name": name, "passed": False, "status": "error", "cause": "setup", "error": "bad file"}

    with_spawn(setup)
    assert calls.count("home") == 1


def test_access_worker_that_died_twice_is_not_checked_per_page():
    def spawn(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
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

    def spawn(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
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
    down = reachability.check("https://app.io", "https://model.example", get=fake_get(refused, 200),
                              sleep=lambda s: None)
    assert down["cause"] == "app"
    offline = reachability.check("https://app.io", "https://model.example", get=fake_get(refused, refused),
                                 sleep=lambda s: None)
    assert offline["cause"] == "machine" and "no network" in offline["error"]


def test_a_site_that_answers_is_never_said_not_to_answer():
    """A new customer's trial (2026-11-17): "not run: the site did not answer" for a site that answered. People read
    what really happened, and one dropped connection is tried again before the site is called down."""
    from everyrole.suite import SuiteRunner

    slow = reachability.check("https://app.io", "https://model.example", sleep=lambda s: None, timeout_s=30,
                              get=fake_get(httpx.ReadTimeout("slow"), 200))
    assert slow["said"] == "app.io didn’t answer within 30 seconds"
    error = reachability.check("https://app.io", "https://model.example", get=fake_get(503, 200))
    assert error["said"] == "app.io answered with a server error (503)"
    tries = iter([httpx.ConnectError("reset"), Answer(200)])

    def flaky(url):
        answer = next(tries)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert reachability.check("https://app.io", "https://model.example", get=flaky, sleep=lambda s: None) is None

    class Flow:
        name, persona, source = "checkout", "", "checkout.json"

    class Cfg:
        personas = {}

    runner = SuiteRunner.__new__(SuiteRunner)
    runner.run_id = "r1"
    suite = runner._site_down(error, [Flow()], Cfg(), 0)
    assert suite["results"][1]["error"] == ("not run: app.io answered with a server error (503) when EveryRole "
                                            "checked it before testing")


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


def test_a_test_whose_only_failures_are_new_wording_is_not_broken():
    from everyrole import report

    changed = {"name": "pricing", "passed": False, "status": "failed_expect", "cause": verdict.WORDING,
               "expectations": [{"kind": "text_visible", "expected": "Buy a film when you want one", "passed": False,
                                 "detail": "not found", "closest": "Pay the day you want a film"},
                                {"kind": "url_contains", "expected": "/pricing", "passed": True, "detail": "ok"}]}
    assert verdict.of(changed) == verdict.CHANGED
    assert verdict.reworded(changed["expectations"]) == [("Buy a film when you want one",
                                                          "Pay the day you want a film")]
    missing = [{"kind": "text_visible", "expected": "Your films", "passed": False, "detail": "not found",
                "closest": ""}, changed["expectations"][0]]
    assert verdict.reworded(missing) == []  # one check really fails: not a wording change
    summary = report.summarize({"results": [changed], "access": [], "duration_ms": 1}, base_url="b", environment="e")
    assert summary["failed"] == 0 and summary["changed"] == 1
    assert "new wording" in report.headline(summary) and "❌" not in report.headline(summary)


# ------------------------------------------------------------------ a test that changes its user type's password

def password_suite(spawn, env, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    original, SuiteRunner._spawn = SuiteRunner._spawn, spawn
    try:
        config = Config(login={"timeout_s": 60},
                        personas={"free": Persona("free", {"password": "{{secrets.SW_FREE_PASSWORD}}"})})
        runner = SuiteRunner(parallel=2, cdp_url="x", base_url="https://app.io", output_dir=tempfile.mkdtemp(),
                             env_file=".env")
        flows = [Flow("other@free", "https://app.io/", "g", ({"url_contains": "/"},), source="o.json", persona="free"),
                 Flow("free-changes-password@free", "https://app.io/settings/account", "Change the password",
                      ({"url_contains": "/settings"},), source="p.json", persona="free", changes_password="free")]
        return runner.run(flows, config)
    finally:
        SuiteRunner._spawn = original


def signing_in(works):
    """A fake worker: sign-ins pass only with the passwords in `works`; every test passes. -> (spawn, calls)."""
    calls = []

    def spawn(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
        calls.append((name, dict(env_extra or {})))
        if args[0] == "login":
            password = (env_extra or {}).get("SW_FREE_PASSWORD", "old-password")
            ok = password in works
            return {"name": name, "passed": ok, "status": "passed" if ok else "failed_expect",
                    "cause": "" if ok else "app"}
        return {"name": name, "passed": True, "status": "passed"}

    return spawn, calls


ENV = {"SW_FREE_PASSWORD": "old-password", "EVERYROLE_NEW_PASSWORD_FREE": "new-password"}


def test_a_password_change_passes_only_when_the_new_password_signs_in(monkeypatch):
    spawn, calls = signing_in({"old-password", "new-password"})
    (login, other, change) = password_suite(spawn, ENV, monkeypatch)["results"]
    assert change["passed"] and change["signin_after"] == "new"
    names = [name for name, _ in calls]
    assert names.index("free-changes-password@free") > names.index("other@free")  # after the user type's other tests
    after = [extra for name, extra in calls if name == "login@free"][1]
    assert after == {"SW_FREE_PASSWORD": "new-password"}  # signed in with the new one


def test_a_password_that_did_not_change_fails_and_the_old_one_is_kept(monkeypatch):
    spawn, _ = signing_in({"old-password"})
    change = password_suite(spawn, ENV, monkeypatch)["results"][-1]
    assert not change["passed"] and change["signin_after"] == "old" and "didn't change" in change["error"]


def test_when_neither_password_signs_in_both_are_kept_and_the_owner_is_told(monkeypatch):
    calls = {"n": 0}
    spawn, _ = signing_in({"old-password"})

    def locked(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
        if args[0] == "login":
            calls["n"] += 1
            if calls["n"] > 1:  # the first sign-in (before the tests) worked; after the change none does
                return {"name": name, "passed": False, "status": "failed_expect", "cause": "app"}
        return spawn(self, args, name=name, timeout_s=timeout_s, source=source, env_extra=env_extra)

    change = password_suite(locked, ENV, monkeypatch)["results"][-1]
    assert not change["passed"] and change["signin_after"] == "neither" and "kept both" in change["error"]


def test_without_a_prepared_password_it_is_not_run(monkeypatch):
    spawn, calls = signing_in({"old-password"})
    monkeypatch.delenv("EVERYROLE_NEW_PASSWORD_FREE", raising=False)
    from everyrole.spec import parse_flows

    flows = parse_flows({"name": "free-changes-password", "as": "free", "start": "{{BASE_URL}}/settings/account",
                         "goal": "Change the password",
                         "values": {"new_password": "{{secrets.EVERYROLE_NEW_PASSWORD_FREE}}"},
                         "expect": [{"url_contains": "/settings"}], "changes_password": True},
                        config=Config(personas={"free": Persona("free", {"password": "{{secrets.SW_FREE_PASSWORD}}"})}),
                        base_url="https://app.io", env={"SW_FREE_PASSWORD": "old-password"})
    assert flows[0].changes_password == "free" and "CI run" in flows[0].not_run


def test_with_other_environments_the_password_is_changed_back_once_proven(monkeypatch):
    spawn, calls = signing_in({"old-password", "new-password"})
    change = password_suite(spawn, {**ENV, "EVERYROLE_PASSWORD_CHANGE_BACK": "1"}, monkeypatch)["results"][-1]
    assert change["passed"] and change["changed_back"] and change["signin_after"] == "old"
    back = [extra for name, extra in calls if name == "free-changes-password@free"][-1]
    back = {k: v for k, v in back.items() if k != "EVERYROLE_ATTEMPT"}  # which try it is (mailbox.ATTEMPT_ENV)
    assert back == {"SW_FREE_PASSWORD": "new-password", "EVERYROLE_NEW_PASSWORD_FREE": "old-password"}


def test_settling_a_change_that_was_changed_back_keeps_the_password():
    import json
    import tempfile
    from pathlib import Path

    from everyrole import password_change

    flows = Path(tempfile.mkdtemp())
    (flows / "p.json").write_text(json.dumps({"name": "p", "as": "free", "changes_password": True}))
    secrets = {"SW_FREE_PASSWORD": "old-password"}
    env = password_change.prepare(secrets, flows, change_back=True)
    assert env["EVERYROLE_PASSWORD_CHANGE_BACK"] == "1" and secrets["SW_FREE_PASSWORD_NEW"]
    events = password_change.settle(secrets, flows, [{"persona": "free", "signin_after": "old", "changed_back": True}])
    assert events == [("free", "changed_back")] and secrets == {"SW_FREE_PASSWORD": "old-password"}




def test_the_ci_reason_is_kept_for_a_run_where_everyrole_does_not_keep_the_password():
    from everyrole import password_change

    assert password_change.not_run_reason("free", {}) == password_change.ELSEWHERE
    assert password_change.not_run_reason("free", {"EVERYROLE_PASSWORD_WAITING_FREE": "1"}) == password_change.WAITING
