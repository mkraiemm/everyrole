"""Security checks and sign-ins done by hand: never an app failure, always 'sign in yourself'."""

import json

from everyrole import challenge, report, verdict
from everyrole.access import AccessResult
from everyrole.signin import check
from everyrole.spec import load_config


def test_known_security_checks_are_recognised_by_where_they_load_from():
    assert challenge.match(["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"]) == \
        "Cloudflare Turnstile"
    assert challenge.match(["https://www.google.com/recaptcha/api2/anchor?k=x"]) == "Google reCAPTCHA"
    assert challenge.match(["https://app.io/_next/static/chunks/main.js"]) == ""
    assert "Sign in yourself" in challenge.message("Cloudflare Turnstile")


def test_a_security_check_is_not_checked_and_never_retried():
    result = {"name": "login@paid", "passed": False, "status": "blocked", "cause": verdict.SIGNIN,
              "error": challenge.message("Cloudflare Turnstile"), "duration_ms": 1, "steps": [],
              "expectations": [], "warnings": []}
    assert verdict.of(result) == verdict.NOT_CHECKED
    summary = report.summarize({"results": [result], "access": [], "duration_ms": 1}, base_url="b", environment="e")
    assert summary["failed"] == 0 and summary["not_checked"] == 1

    from everyrole.suite import SuiteRunner

    attempts = []
    runner = SuiteRunner(parallel=1, cdp_url="x", base_url="https://app.io", output_dir="o", env_file=".env")
    assert runner._twice(lambda: attempts.append(1) or dict(result)) and attempts == [1]


def saved_config(tmp_path, *, with_login=False):
    (tmp_path / "signins").mkdir()
    (tmp_path / "signins" / "paid.json").write_text(json.dumps({"cookies": [{"name": "sid", "value": "x"}],
                                                                "storage": {}}))
    raw = {"personas": {"paid": {"saved_signin": {"file": "signins/paid.json", "check": "/dashboard"}},
                        "guest": {"login": False}},
           "denied_when": [{"url_contains": "/login"}]}
    if with_login:
        raw["login"] = {"start": "{{BASE_URL}}/login", "goal": "g", "expect": [{"url_contains": "/"}]}
    (tmp_path / "everyrole.config.json").write_text(json.dumps(raw))
    return load_config(tmp_path / "everyrole.config.json")


class Checker:
    def __init__(self, observed):
        self.observed, self.calls = observed, []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        (path, expected), = kwargs["rules"]
        passed = self.observed == "allowed"
        return [AccessResult(path, kwargs["persona"], expected, self.observed, passed)]


def test_a_persona_signed_in_by_hand_needs_no_login_recipe_and_is_checked_without_ai(tmp_path):
    config = saved_config(tmp_path)  # no `login` section: nobody types a password
    ok = Checker("allowed")
    result, state = check(config, "paid", base_url="https://app.io", checker=ok)
    assert result["passed"] and state["cookies"][0]["name"] == "sid"
    assert ok.calls[0]["rules"] == [("/dashboard", "allow")] and ok.calls[0]["session_state"] == state


def test_an_expired_sign_in_asks_the_person_to_sign_in_again(tmp_path):
    config = saved_config(tmp_path)
    result, state = check(config, "paid", base_url="https://app.io", checker=Checker("denied"))
    assert state is None and not result["passed"] and result["cause"] == verdict.SIGNIN
    assert "Sign in again" in result["error"] and verdict.of(result) == verdict.NOT_CHECKED
    broken_browser, _ = check(config, "paid", base_url="https://app.io", checker=Checker("error"))
    assert broken_browser["cause"] == verdict.MACHINE  # our browser failed: not an expiry


def test_no_saved_sign_in_yet(tmp_path):
    config = saved_config(tmp_path)
    (tmp_path / "signins" / "paid.json").unlink()
    result, state = check(config, "paid", base_url="https://app.io", checker=Checker("allowed"))
    assert state is None and result["cause"] == verdict.SIGNIN and "Sign in yourself" in result["error"]
