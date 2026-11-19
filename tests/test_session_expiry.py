"""A session that expires during a run: sign in again once, run the affected tests once more, never loop, and never
call it an app failure when signing in again fails."""

import tempfile
import threading

from everyrole import verdict
from everyrole.assertions import PageFacts
from everyrole.signin import SignedOut
from everyrole.spec import Config, Flow, Persona
from everyrole.suite import SuiteRunner
from everyrole.worker import _mark_signed_out


def make_config(**overrides):
    return Config(
        login={"timeout_s": 60, "start": "{{BASE_URL}}/login"},
        personas={"paid": Persona("paid", {}), "guest": Persona("guest", {}, False),
                  "admin": Persona("admin", {}, signin={"start": "{{BASE_URL}}/admin/sign-in", "expect": []})},
        denied_when=({"text_visible": "Please sign in to continue"},),
        access_settle_s=1,
        **overrides,
    )


def facts(url, text="", passwords=0):
    return PageFacts(url=url, text=text, labels=(), passwords=passwords)


def test_the_apps_own_sign_in_pages_and_refusal_signal_say_signed_out():
    check = SignedOut.of(make_config(), "paid", base_url="https://app.io")
    assert "sign-in page" in check(facts("https://app.io/login?next=/billing", passwords=1))
    assert "sign-in page" in check(facts("https://app.io/login/", passwords=1))
    # the sign-in page's address with no password asked: a sign-in that stays there once signed in (a pop-up on the
    # home page, a form that turns into the app): the address alone never says signed out
    assert check(facts("https://app.io/login", "Welcome back")) == ""
    assert "refusal signal" in check(facts("https://app.io/billing", "Please sign in to continue"))
    assert check(facts("https://app.io/billing", "Your invoices")) == ""
    assert check(facts("https://other.io/login")) == ""  # another site's sign-in page is not this app's
    admin = SignedOut.of(make_config(), "admin", base_url="https://app.io")
    assert admin.page("https://app.io/admin/sign-in") and admin.page("https://app.io/login")  # its own and the app's
    assert SignedOut.of(make_config(), "guest", base_url="https://app.io") is None  # never signed in: nothing to lose


def test_a_page_check_sent_to_the_sign_in_page_is_marked_only_when_it_should_open():
    rows = [{"path": "/billing", "expected": "allow", "observed": "denied", "url": "https://app.io/login"},
            {"path": "/admin", "expected": "deny", "observed": "denied", "url": "https://app.io/login"},
            {"path": "/films", "expected": "allow", "observed": "denied", "url": "https://app.io/"}]
    from everyrole.site import Site

    marked = _mark_signed_out([dict(r) for r in rows], make_config(), "paid", Site.single("https://app.io"), {})
    assert [bool(r.get("signed_out")) for r in marked] == [True, False, False]
    unsigned = _mark_signed_out([dict(r) for r in rows], make_config(), "paid", Site.single("https://app.io"), None)
    assert not any(r.get("signed_out") for r in unsigned)  # no session restored: nothing could expire


class Fake:
    """Stands in for the workers. `logins` answers each sign-in in turn; `flow(name, session, n)` answers a test's
    n-th run with that session."""

    def __init__(self, logins, flow, access=None):
        self.logins, self.flow, self.access = list(logins), flow, access
        self.calls, self.lock = [], threading.Lock()

    def spawn(self, runner, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
        session = args[args.index("--session") + 1] if "--session" in args else None
        with self.lock:
            self.calls.append((args[0], name, session))
            n = sum(1 for kind, other, _ in self.calls if kind == args[0] and other == name)
        if args[0] == "login":
            ok = self.logins.pop(0)
            return {"name": name, "passed": ok, "status": "passed" if ok else "failed_expect",
                    "cause": "" if ok else verdict.SIGNIN, "error": "" if ok else "Your saved sign-in expired",
                    "usage": {}}
        if args[0] == "access":
            return self.access(session, args)
        return {"name": name, "persona": "paid", **self.flow(name, session, n)}

    def run(self, flows, config=None):
        original, SuiteRunner._spawn = SuiteRunner._spawn, lambda runner, *a, **k: self.spawn(runner, *a, **k)
        try:
            return SuiteRunner(parallel=2, cdp_url="x", base_url="https://app.io", output_dir=tempfile.mkdtemp(),
                               env_file=".env").run(flows, config or make_config())
        finally:
            SuiteRunner._spawn = original

    def count(self, kind, name=None):
        return sum(1 for k, n, _ in self.calls if k == kind and (name is None or n == name))


def paid_flow(name):
    return Flow(f"{name}@paid", f"https://app.io/{name}", "g", ({"url_contains": f"/{name}"},), persona="paid",
                source=f"{name}.json")


SIGNED_OUT = {"passed": False, "status": "failed_expect", "cause": "app",
              "signed_out": "it ended on the sign-in page (https://app.io/login)"}


def test_signed_out_mid_run_signs_in_again_once_and_runs_each_affected_test_once_more():
    def flow(name, session, n):
        return SIGNED_OUT if not session.endswith("-again.json") else {"passed": True, "status": "passed"}

    fake = Fake([True, True], flow)
    suite = fake.run([paid_flow("a"), paid_flow("b")])
    results = {r["name"]: r for r in suite["results"]}
    assert results["a@paid"]["passed"] and results["b@paid"]["passed"]
    assert fake.count("login") == 2  # the first sign-in, then one more for the whole run (not one per test)
    # never a blind retry with the expired session; a test that starts after signing in again gets the new one
    assert all(fake.count("flow", n) in (1, 2) for n in ("a@paid", "b@paid")) and fake.count("flow") in (3, 4)
    assert all(session.endswith("-again.json") for kind, _, session in fake.calls[-1:] if kind == "flow")
    rerun = [r for r in results.values() if r.get("attempts") == 2]
    assert rerun and all("signed in again and ran it once more" in r["warnings"][-1] for r in rerun)


def test_signing_in_again_fails_so_the_tests_could_not_be_checked_not_broken():
    fake = Fake([True, False], lambda name, session, n: SIGNED_OUT)
    suite = fake.run([paid_flow("a"), paid_flow("b")])
    for result in (r for r in suite["results"] if not r["name"].startswith("login@")):
        assert verdict.of(result) == verdict.NOT_CHECKED and result["cause"] == verdict.SIGNIN
        assert result["error"].startswith("couldn't check: signed out mid-run")
        assert "signing in again failed (Your saved sign-in expired)" in result["error"]
    assert fake.count("login") == 2 and fake.count("flow") == 2  # nothing ran again


def test_a_test_signed_out_again_with_the_new_session_is_not_run_again():
    fake = Fake([True, True], lambda name, session, n: SIGNED_OUT)
    suite = fake.run([paid_flow("a")])
    result = next(r for r in suite["results"] if r["name"] == "a@paid")
    assert fake.count("flow") == 2 and fake.count("login") == 2  # once more, then it stops: never a loop
    assert not result["passed"] and result.get("signed_out")


def test_a_test_that_starts_with_the_new_session_and_is_signed_out_is_not_run_again():
    runner = SuiteRunner(parallel=1, cdp_url="x", base_url="https://app.io", output_dir="o", env_file=".env")
    rerun = []
    runner._flow = lambda *a, **k: rerun.append(a) or {}
    result = runner._flow_signed_out(paid_flow("a"), dict(SIGNED_OUT), "s-again.json", ("s-again.json", {}))
    assert not rerun and "not run again" in result["warnings"][-1]


def test_page_checks_sent_to_sign_in_are_checked_again_after_signing_in_again():
    def access(session, args):
        again = session.endswith("-again.json")
        out = {} if again else {"signed_out": "it ended on the sign-in page (https://app.io/login)"}
        observed = "allowed" if again else "denied"
        return [{"path": "/films", "persona": "paid", "expected": "allow", "observed": observed,
                 "passed": again, "url": "https://app.io/films" if again else "https://app.io/login",
                 "detail": "ok" if again else "redirected to /login", **out}]

    config = make_config(access={"/films": {"allow": ["paid"]}})
    fake = Fake([True, True], lambda *a: {"passed": True, "status": "passed"}, access)
    suite = fake.run([], config)
    (row,) = suite["access"]
    assert row["passed"] and "checked again after signing in again" in row["detail"]
    assert fake.count("login") == 2 and fake.count("access") == 2
