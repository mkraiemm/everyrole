"""Form-only tests: the write-guard blocks saves, `tried_to_save` proves the submit fired; failed units retry."""

from everyrole import assertions
from everyrole.assertions import PageFacts
from everyrole.writeguard import GUARD, app_writes


def facts(blocked=()):
    return PageFacts(url="https://app.io/projects", text="", labels=(), blocked=tuple(blocked))


def test_tried_to_save_check():
    check = assertions.build([{"tried_to_save": "/api/projects"}])
    assert assertions.evaluate(check, facts([("POST", "https://api.app.io/api/projects")]))[0].passed
    assert not assertions.evaluate(check, facts())[0].passed
    assert not assertions.evaluate(check, facts([("BEACON", "https://api.app.io/api/projects")]))[0].passed
    assert "other attempts" in assertions.evaluate(check, facts([("POST", "https://x/api/other")]))[0].detail


def test_guard_blocks_every_write_kind_and_app_writes_skip_beacons():
    for needle in ('"GET", "HEAD", "OPTIONS"', "XMLHttpRequest.prototype.send", "sendBeacon", '"submit"'):
        assert needle in GUARD
    assert app_writes([("POST", "/a"), ("BEACON", "/b"), ("FORM", "/c")]) == [("POST", "/a"), ("FORM", "/c")]


def test_failed_login_is_retried_once():
    from everyrole.suite import SuiteRunner

    attempts = []

    def spawn(self, args, *, name, timeout_s, source=""):
        attempts.append(name)
        return {"name": name, "passed": len(attempts) > 1, "status": "passed" if len(attempts) > 1 else "error",
                "error": "Model provider returned HTTP 503"}

    original, SuiteRunner._spawn = SuiteRunner._spawn, spawn
    try:
        runner = SuiteRunner(parallel=1, cdp_url="x", base_url="https://app.io", output_dir="o", env_file=".env")
        result = runner._login("free", "s.json", 60)
    finally:
        SuiteRunner._spawn = original
    assert attempts == ["login@free", "login@free"] and result["passed"]
    assert "tried again" in result["warnings"][0] and result["attempts"] == 2
