"""Sessions kept for user types that sign in by email (kept_session.py): one emailed link, then the session is reused
while it works, so an app's link limits (EveryRole: 3 per email every 15 minutes) never block the next jobs."""

import json

from test_signin import Checker, saved_config

from everyrole import kept_session
from everyrole.access import AccessResult


def kept(tmp_path, config):
    state = tmp_path / "session.json"
    state.write_text(json.dumps({"cookies": [{"name": "sid", "value": "s"}], "storage": {}}))
    kept_session.keep(config, "paid", state, "/apps")


def test_a_kept_session_that_still_signs_in_is_reused_without_asking_for_a_link(tmp_path):
    config = saved_config(tmp_path)
    kept(tmp_path, config)
    ok = Checker("allowed")
    state, page = kept_session.reuse(config, "paid", checker=ok, env={})
    assert page == "/apps" and state["cookies"][0]["name"] == "sid"
    assert ok.calls[0]["rules"] == [("/apps", "allow")]  # checked on the page it landed on, no AI


def test_a_kept_session_the_app_sends_to_sign_in_is_dropped(tmp_path):
    config = saved_config(tmp_path)
    kept(tmp_path, config)
    assert kept_session.reuse(config, "paid", checker=Checker("denied"), env={}) is None
    assert not any((tmp_path / kept_session.FOLDER).glob("*.json"))  # the next job signs in by email again


def test_a_page_that_did_not_load_keeps_the_session(tmp_path):
    class Down(Checker):
        def run(self, **kwargs):
            (path, expected), = kwargs["rules"]
            return [AccessResult(path, kwargs["persona"], expected, "unknown", False, cause="machine")]

    config = saved_config(tmp_path)
    kept(tmp_path, config)
    assert kept_session.reuse(config, "paid", checker=Down("x"), env={}) is None
    assert (tmp_path / kept_session.FOLDER / "paid.json").is_file()  # says nothing about the session


def test_a_sign_in_that_must_really_happen_never_reuses_one(tmp_path):
    config = saved_config(tmp_path)
    kept(tmp_path, config)
    assert kept_session.reuse(config, "paid", checker=Checker("allowed"), env={kept_session.FRESH_ENV: "1"}) is None


class Lands(Checker):
    """Opens a page and ends on `url` (where the app sent the browser)."""

    def __init__(self, url, observed="allowed", cause=""):
        super().__init__(observed)
        self.url, self.cause = url, cause

    def run(self, **kwargs):
        self.calls.append(kwargs)
        (path, expected), = kwargs["rules"]
        return [AccessResult(path, kwargs["persona"], expected, self.observed, self.observed == "allowed",
                             url=self.url, cause=self.cause)]


def test_a_signed_in_user_starts_where_the_app_sends_them_not_where_a_sign_in_ended(tmp_path):
    """Seen on EveryRole: the member's kept sign-in had ended on the app's page, so every member test started
    there instead of /apps, where the app sends a signed-in member. Opening the sign-in page with the session asks
    the app."""
    from everyrole.signin import home_of
    from everyrole.spec import load_config, site_of

    raw = {"login": {"start": "{{BASE_URL}}/login", "goal": "Sign in", "expect": [{"url_contains": "/apps"}]},
           "personas": {"member": {"values": {"email": "m@a.io"}}}, "denied_when": [{"text_visible": "Log in"}]}
    (tmp_path / "everyrole.config.json").write_text(json.dumps(raw))
    config, site, state = load_config(tmp_path / "everyrole.config.json"), site_of("https://a.io"), {"cookies": []}
    sends_home = Lands("https://a.io/apps")
    assert home_of(config, "member", state, site=site, checker=sends_home) == "/apps"
    assert sends_home.calls[0]["rules"] == [("/login", "allow")]  # its sign-in page, no AI
    # left on the sign-in page (an app that doesn't send signed-in people on, or the session ended): nothing learned
    assert home_of(config, "member", state, site=site, checker=Lands("https://a.io/login", "denied")) is None
    assert home_of(config, "member", state, site=site, checker=Lands("", "error", cause="machine")) is None
    assert home_of(config, "member", None, site=site, checker=sends_home) is None
