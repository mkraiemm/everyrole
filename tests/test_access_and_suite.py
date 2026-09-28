import json
import tempfile

from everyrole import report
from everyrole.access import AccessChecker, expected_rules
from everyrole.assertions import PageFacts
from everyrole.spec import Config, Flow, Persona
from everyrole.suite import SuiteRunner


class Tab:
    """A page that lands on `final_url` after `redirect_after` reads."""

    def __init__(self, url, final_url=None, redirect_after=0, busy=False, **options):
        self.url, self.final_url, self.reads, self.redirect_after = url, final_url, 0, redirect_after
        self.options, self.closed = {**options, "busy": busy}, False

    def observe(self, screenshot=False):
        self.reads += 1
        if self.final_url and self.reads > self.redirect_after:
            self.url = self.final_url
        return {"url": self.url, "actions": []}

    def close(self):
        self.closed = True


def checker(pages):
    opened = []

    def factory(url, **options):
        spec = pages[url]
        if isinstance(spec, Exception):
            raise spec
        opened.append(Tab(url, **spec, **options))
        return opened[-1]

    ticks = iter(i * 0.25 for i in range(10000))
    facts = lambda browser, page: PageFacts(url=page["url"], text="", labels=())  # noqa: E731
    loading = lambda browser: ("loading", False) if browser.options.get("busy") else ("loaded", True)  # noqa: E731
    return AccessChecker(browser_factory=factory, facts_reader=facts, clock=lambda: next(ticks),
                         sleep=lambda s: None, page_state=loading), opened


def test_access_denied_allowed_and_errors():
    run, opened = checker({
        "https://app.io/billing": {"final_url": "https://app.io/login?next=/billing", "redirect_after": 2},
        "https://app.io/projects": {},
        "https://app.io/admin": {},
        "https://app.io/broken": RuntimeError("boom"),
    })
    rules = [("/billing", "deny"), ("/projects", "allow"), ("/admin", "deny"), ("/broken", "allow")]
    results = run.run(base_url="https://app.io/", persona="free", rules=rules,
                      denied_when=[{"url_contains": "/login"}], settle_s=3, session_state={"cookies": []})
    got = {r.path: (r.observed, r.passed) for r in results}
    assert got == {"/billing": ("denied", True), "/projects": ("allowed", True), "/admin": ("allowed", False),
                   "/broken": ("error", False)}
    assert [r.path for r in results] == [p for p, _ in rules]  # stable order
    assert all(t.closed for t in opened) and all(t.options["isolated"] for t in opened)
    assert all("prepare" in t.options for t in opened)  # the persona's session is restored


def test_a_page_still_loading_is_not_called_open():
    # the app is still asking its server whether this user may see the page: it may yet send them away
    run, opened = checker({"https://app.io/admin/budgets": {"busy": True}, "https://app.io/films": {}})
    rows = run.run(base_url="https://app.io/", persona="paid", rules=[("/admin/budgets", "allow"), ("/films", "allow")],
                   denied_when=[], settle_s=2)
    budgets, films = rows
    assert (budgets.observed, budgets.passed, budgets.cause) == ("unsure", False, "machine")
    assert "still loading" in budgets.detail and len(opened) == 3  # checked again alone before reporting
    assert (films.observed, films.passed) == ("allowed", True)


def test_expected_rules_per_persona():
    config = Config(access={"/a": {"allow": ["paid"], "deny": ["guest"]}, "/b": {"deny": ["paid"]}})
    assert expected_rules(config, "paid") == [("/a", "allow"), ("/b", "deny")]
    assert expected_rules(config, "guest") == [("/a", "deny")]
    assert expected_rules(config, "free") == []


def make_config():
    return Config(
        login={"timeout_s": 60},
        personas={"paid": Persona("paid", {}), "free": Persona("free", {}), "guest": Persona("guest", {}, False)},
        access={"/projects": {"allow": ["paid", "free"], "deny": ["guest"]}},
        denied_when=({"url_contains": "/login"},),
        access_settle_s=1,
    )


def fake_suite(login_ok):
    calls = []

    def spawn(self, args, *, name, timeout_s, source=""):
        calls.append((args[0], name, args))
        if args[0] == "login":
            persona = args[args.index("--persona") + 1]
            return {"name": name, "passed": login_ok[persona], "status": "passed" if login_ok[persona] else "failed"}
        if args[0] == "access":
            persona = args[args.index("--persona") + 1]
            return [{"path": "/projects", "persona": persona, "expected": "allow", "observed": "allowed",
                     "passed": True}]
        return {"name": name, "passed": True, "status": "passed", "args": args}

    SuiteRunner._spawn, original = spawn, SuiteRunner._spawn
    return calls, original


def run_suite(login_ok, flows):
    calls, original = fake_suite(login_ok)
    try:
        suite = SuiteRunner(parallel=2, cdp_url="http://cdp", base_url="https://app.io", output_dir=tempfile.mkdtemp(),
                            env_file=".env").run(flows, make_config())
    finally:
        SuiteRunner._spawn = original
    return suite, calls


def test_suite_signs_in_once_per_persona_then_reuses_sessions():
    flows = [Flow("a@paid", "https://app.io/a", "g", ({"url_contains": "/a"},), persona="paid", source="a.json"),
             Flow("b@paid", "https://app.io/b", "g", ({"url_contains": "/b"},), persona="paid", source="b.json"),
             Flow("home", "https://app.io/", "g", ({"url_contains": "/"},), source="home.json")]
    suite, calls = run_suite({"paid": True, "free": True}, flows)
    logins = [c for c in calls if c[0] == "login"]
    assert sorted(c[1] for c in logins) == ["login@free", "login@paid"]  # once each, incl. access-only persona
    assert calls.index(logins[-1]) < min(i for i, c in enumerate(calls) if c[0] != "login")  # logins first
    runs = {r["name"]: r for r in suite["results"] if not r["name"].startswith("login@")}
    assert "--session" in runs["a@paid"]["args"] and "--session" not in runs["home"]["args"]
    assert {a["persona"] for a in suite["access"]} == {"paid", "free", "guest"}


def test_failed_login_skips_that_personas_work_only():
    flows = [Flow("a@free", "https://app.io/a", "g", ({"url_contains": "/a"},), persona="free", source="a.json"),
             Flow("b@paid", "https://app.io/b", "g", ({"url_contains": "/b"},), persona="paid", source="b.json")]
    suite, calls = run_suite({"paid": True, "free": False}, flows)
    results = {r["name"]: r for r in suite["results"]}
    assert results["a@free"]["error"] == "not run: sign-in as 'free' failed"
    assert results["b@paid"]["passed"]
    free_access = [a for a in suite["access"] if a["persona"] == "free"]
    assert free_access and all(a["observed"] == "skipped" and a["cause"] == "skipped" for a in free_access)
    assert results["a@free"]["cause"] == "skipped"
    assert not any(c[0] == "flow" and c[1] == "a@free" for c in calls)


def test_report_access_matrix_and_denied_label():
    results = [{"name": "invite@free", "persona": "free", "status": "blocked", "passed": True, "expect_denied": True,
                "duration_ms": 4000, "steps": [], "expectations": [], "warnings": []}]
    access = [
        {"path": "/billing", "persona": "paid", "expected": "allow", "observed": "allowed", "passed": True},
        {"path": "/billing", "persona": "free", "expected": "deny", "observed": "allowed", "passed": False,
         "detail": "no denial signal within 5s", "url": "https://app.io/billing"},
    ]
    summary = report.summarize({"results": results, "access": access, "duration_ms": 9000}, base_url="b",
                               environment="e")
    assert summary["failed"] == 1 and summary["access_total"] == 2
    md = report.markdown(summary, results, access)
    assert "✅ denied as expected" in md and "| free |" in md
    assert "| `/billing` | ✅ allowed | ❌ allowed (should deny) |" in md
    xml = report.junit(summary, results, access)
    assert 'name="/billing as free"' in xml and "expected deny, got allowed" in xml
    json.dumps(summary)


def test_being_redirected_away_counts_as_refused_without_any_signal():
    run, _ = checker({
        "https://app.io/admin": {"final_url": "https://app.io/", "redirect_after": 1},  # sent home
        "https://app.io/admin/users": {},  # stays
    })
    results = run.run(base_url="https://app.io", persona="paid", rules=[("/admin", "deny"), ("/admin/users", "deny")],
                      denied_when=[], settle_s=2)
    by = {r.path: r for r in results}
    assert by["/admin"].observed == "denied" and by["/admin"].passed and "redirected to /" in by["/admin"].detail
    assert by["/admin/users"].observed == "allowed" and not by["/admin/users"].passed


def test_a_check_that_could_not_run_is_retried_alone():
    attempts = []

    def factory(url, **options):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError("Runtime.evaluate timed out after 5s waiting for the daemon")
        return Tab(url, final_url="https://app.io/login")

    ticks = iter(i * 0.25 for i in range(10000))
    facts = lambda b, p: PageFacts(url=p["url"], text="", labels=())  # noqa: E731
    checker = AccessChecker(browser_factory=factory, facts_reader=facts, clock=lambda: next(ticks),
                            sleep=lambda s: None)
    (result,) = checker.run(base_url="https://app.io", persona="guest", rules=[("/admin", "deny")], denied_when=[],
                            settle_s=1)
    assert len(attempts) == 2 and result.observed == "denied" and result.passed


def test_a_kind_of_user_with_many_pages_is_checked_in_batches_each_its_own_worker():
    from everyrole.spec import Config, Persona
    from everyrole.suite import SuiteRunner

    rules = {f"/admin/{i}": {"allow": ["admin"], "deny": ["guest"]} for i in range(20)}
    config = Config(personas={"admin": Persona("admin", {}), "guest": Persona("guest", {}, False)}, access=rules,
                    run={"parallel": 3, "access_batch": 8})
    batches = SuiteRunner._access_batches(config, ["admin", "guest"])
    assert [key for key, _, _ in batches] == ["access@admin 1/3", "access@admin 2/3", "access@admin 3/3",
                                             "access@guest 1/3", "access@guest 2/3", "access@guest 3/3"]
    assert [len(pages) for _, p, pages in batches if p == "admin"] == [8, 8, 4]
    assert sorted(p for _, who, pages in batches if who == "admin" for p in pages) == sorted(rules)


def test_tabs_a_killed_worker_left_open_are_closed(tmp_path):
    import json as _json
    import os
    import subprocess
    import sys
    import time
    import urllib.request

    from everyrole import daemons
    from everyrole.chrome import chrome_session, close_tabs
    from everyrole.suite import TABS_FILE_ENV

    tabs = tmp_path / "tabs"
    daemon_home = tmp_path / "daemons"  # the killed worker's daemon registers here, and is stopped below
    daemon_home.mkdir()
    with chrome_session() as chrome:
        env = {**os.environ, "BU_CDP_URL": chrome.url, "BU_NAME": f"sw-test-tabs-{os.getpid()}",
               TABS_FILE_ENV: str(tabs), daemons.ENV: str(daemon_home)}
        worker = subprocess.Popen([sys.executable, "-c",
                                   "from jev_ultrafast.browser import Browser; import time; "
                                   "Browser('about:blank', isolated=True); print('open', flush=True); time.sleep(120)"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = worker.stdout.readline().strip()
        assert line == "open", worker.stderr.read() if worker.poll() is not None else "no answer yet"
        worker.kill()  # like a worker that hung past its time limit
        worker.wait()
        left = tabs.read_text(encoding="utf-8").split()

        def open_ids():
            with urllib.request.urlopen(f"{chrome.url}/json/list") as response:
                return {t["id"] for t in _json.loads(response.read())}

        assert set(left) <= open_ids()  # still open in the shared browser after its worker died
        assert close_tabs(chrome.url, left) == len(left)
        time.sleep(0.5)
        assert not set(left) & open_ids()
        assert close_tabs(chrome.url, left) == 0  # closing again is harmless
        daemons.stop(daemon_home)  # like the job does for a worker that was killed
