"""One app on several addresses (parts) x environments: pages, templates, access, reports."""

import json

import pytest

from everyrole.access import AccessChecker
from everyrole.assertions import PageFacts
from everyrole.site import Site, resolve, split
from everyrole.spec import Config, SpecError, load_config, parse_flows
from everyrole.suite import SuiteRunner

SITE = Site.of({"app": "https://app.example.com", "site": "https://example.com"})


# ---------------------------------------------------------------- pages and addresses

def test_pages_are_written_with_their_part():
    assert split("site:/privacy") == ("site", "/privacy") and split("/pricing") == ("", "/pricing")
    assert split("https://example.com/x") == ("", "https://example.com/x")  # a URL is never a page
    assert SITE.url("/dashboard") == "https://app.example.com/dashboard"
    assert SITE.url("site:/privacy") == "https://example.com/privacy"
    assert SITE.page("https://example.com/privacy?x=1") == "site:/privacy"
    assert SITE.page("https://app.example.com/dashboard") == "/dashboard"
    assert SITE.page("https://github.com/example") is None
    assert SITE.label("site:/privacy") == "example.com/privacy"
    assert SITE.label("/dashboard") == "app.example.com/dashboard"
    assert Site.single("https://a.io").label("/x") == "/x"  # one address: plain paths, as before
    assert SITE.hosts == ("app.example.com", "example.com")


@pytest.mark.parametrize("parts, message", [
    ({}, "at least one address"),
    ({"App": "https://a.io"}, "part name"),
    ({"app": "a.io"}, "full address"),
    ({"app": "https://a.io", "site": "https://a.io/"}, "same address"),
])
def test_bad_parts_are_refused(parts, message):
    with pytest.raises(SpecError, match=message):
        Site.of(parts)


KIT = Config(environments={
    "production": {"app": "https://app.example.com", "site": "https://example.com"},
    "staging": {"app": "https://staging-app.example.com", "site": "https://staging.example.com"},
})


def test_each_environment_resolves_every_part():
    assert resolve(KIT, environment="production") == SITE
    staging = resolve(KIT, environment="staging")
    assert staging.url("site:/privacy") == "https://staging.example.com/privacy"
    # a preview deploy of the app, the site on its production address given explicitly
    preview = resolve(KIT, base_url="https://pr-9.app.example.com", parts=["site=https://example.com"])
    assert preview.main_url == "https://pr-9.app.example.com" and preview.url_of("site") == "https://example.com"
    # a one-address app: exactly as before
    assert resolve(Config(), base_url="https://a.io/") == Site.single("https://a.io")


@pytest.mark.parametrize("kwargs, message", [
    ({"environment": "preview", "base_url": "https://pr-9.app.example.com"}, r"no address for part\(s\) site"),
    ({"environment": "qa"}, "not in the config"),
    ({"environment": "production", "parts": ["docs=https://docs.example.com"]}, "not a part of this app"),
    ({"environment": "production", "parts": ["site"]}, "name=address"),
])
def test_missing_or_unknown_addresses_are_said_plainly(kwargs, message):
    with pytest.raises(SpecError, match=message):
        resolve(KIT, **kwargs)
    with pytest.raises(SpecError, match="pass --base-url"):
        resolve(Config())


# ---------------------------------------------------------------- config and flows

def write_config(tmp_path, extra):
    path = tmp_path / "everyrole.config.json"
    path.write_text(json.dumps({"environments": KIT.environments, **extra}), encoding="utf-8")
    return path


def test_flows_start_on_any_part_and_may_go_anywhere_in_the_app(tmp_path):
    config = load_config(write_config(tmp_path, {"access": {"site:/privacy": {"allow": []}}}))
    assert config.parts == ["app", "site"] and "site:/privacy" in config.access
    raw = {"name": "privacy", "start": "{{URL.site}}/privacy", "goal": "Read the privacy policy",
           "expect": [{"text_visible": "Privacy Policy"}]}
    (flow,) = parse_flows(raw, config=config, site=resolve(config, environment="staging"))
    assert flow.start == "https://staging.example.com/privacy"
    assert set(flow.allowed_hosts) == {"staging-app.example.com", "staging.example.com"}
    with pytest.raises(SpecError, match="no part 'docs'"):
        parse_flows({**raw, "start": "{{URL.docs}}/"}, config=config, site=SITE)


def test_config_environments_must_agree(tmp_path):
    bad = {"environments": {"production": {"app": "https://a.io", "site": "https://s.io"},
                            "staging": {"app": "https://sa.io"}}}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SpecError, match="same parts"):
        load_config(path)
    with pytest.raises(SpecError, match="not one of the app's other parts"):
        load_config(write_config(tmp_path, {"access": {"docs:/x": {"allow": []}}}))


# ---------------------------------------------------------------- access rules on any part

class Tab:
    def __init__(self, url, **options):
        self.url = FINAL.get(url, url)

    def observe(self, screenshot=False):
        return {"url": self.url, "actions": []}

    def close(self):
        pass


FINAL = {"https://example.com/admin": "https://app.example.com/login"}


def test_access_rules_open_each_page_on_its_own_address():
    ticks = iter(i * 0.25 for i in range(10000))
    checker = AccessChecker(browser_factory=Tab, clock=lambda: next(ticks), sleep=lambda s: None,
                            page_state=lambda browser: ("loaded", True),
                            facts_reader=lambda browser, page: PageFacts(url=page["url"], text="", labels=()))
    rows = checker.run(site=SITE, persona="guest", rules=[("site:/privacy", "allow"), ("site:/admin", "deny")],
                       denied_when=[], settle_s=1)
    assert [(r.path, r.observed, r.passed) for r in rows] == [("site:/privacy", "allowed", True),
                                                              ("site:/admin", "denied", True)]
    assert "redirected to /login" in rows[1].detail  # landed on the app's sign-in page


def test_workers_get_every_address():
    runner = SuiteRunner(parallel=1, cdp_url="http://x", output_dir="out", env_file=".env", site=SITE)
    assert runner.base_url == "https://app.example.com"
    assert SITE.as_args() == ["--base-url", "https://app.example.com", "--part", "site=https://example.com"]


# ---------------------------------------------------------------- reports and sign-in

def test_reports_show_real_addresses_not_part_names():
    from everyrole import report

    access = [{"path": "site:/privacy", "persona": "guest", "expected": "allow", "observed": "allowed", "passed": True}]
    summary = report.summarize({"results": [], "access": access, "duration_ms": 1}, base_url=SITE.main_url,
                               environment="production", parts=dict(SITE.parts))
    text = report.markdown(summary, [], access)
    assert "`example.com/privacy`" in text and "site:/privacy" not in text


def test_a_sign_in_is_done_only_when_no_password_is_asked_any_more():
    from everyrole.assertions import PageFacts, build, evaluate

    (flow,) = parse_flows({"name": "x", "start": "{{BASE_URL}}/login", "goal": "g",
                           "expect": [{"url_contains": "/"}, {"password_field_absent": True}]}, base_url="https://a.io")
    checks = build(flow.expect)
    two_step = PageFacts(url="https://a.io/login/password", text="Enter your password", labels=(), passwords=1)
    done = PageFacts(url="https://a.io/home", text="Welcome", labels=(), passwords=0)
    assert not all(o.passed for o in evaluate(checks, two_step)) and all(o.passed for o in evaluate(checks, done))
