"""Sign-in by email (Invite EveryRole): a test account whose email is one of EveryRole's inboxes signs in with the link
or code the app emails. An app with no password (magic links) is signed in to; a password app needs no email; an
email that was already in the inbox (an earlier link) is never opened."""

import json

from test_journeys import BASE, Browser, JourneyAgent, Values

from everyrole import mailbox
from everyrole.runner import FlowRunner
from everyrole.spec import EMAIL_ONLY_SIGNIN, EMAIL_SIGNIN, FINISH_SIGNIN, load_config, login_flow

KEY = mailbox.signing_key("server-secret")
DOMAIN = "inbox.everyrole.dev"
ADDRESS = mailbox.address(DOMAIN, KEY, 42, "member")


def env(tmp_path, **secrets):
    return {mailbox.DOMAIN_ENV: DOMAIN, mailbox.KEY_ENV: KEY, mailbox.PROJECT_ENV: "42",
            mailbox.DIR_ENV: str(tmp_path / "inbox"), "SW_MEMBER_EMAIL": ADDRESS, **secrets}


def config(tmp_path, email="{{secrets.SW_MEMBER_EMAIL}}", signin_by=""):
    raw = {"login": {"start": "{{BASE_URL}}/login", "goal": "Log in",
                     "expect": [{"url_not_contains": "/login"}, {"text_visible": "Your apps"}]},
           "personas": {"member": {"values": {"username_or_email": email,
                                              "password": "{{secrets.SW_MEMBER_PASSWORD}}"},
                                   **({"signin_by": signin_by} if signin_by else {})}}}
    path = tmp_path / "everyrole.config.json"
    path.write_text(json.dumps(raw))
    return load_config(path)


def test_an_inbox_account_with_no_password_signs_in_by_email(tmp_path):
    flow = login_flow(config(tmp_path), "member", base_url=BASE, env=env(tmp_path))
    assert flow.inbox == str(tmp_path / "inbox" / "member") and flow.is_login
    assert "password" not in flow.values and flow.values["username_or_email"] == ADDRESS
    assert [s["goal"] for s in flow.steps] == [EMAIL_ONLY_SIGNIN, "", FINISH_SIGNIN]
    assert flow.steps[1]["open_email"] and not flow.steps[1]["email_if_needed"]  # no password: always the email


def test_an_account_known_to_sign_in_by_email_never_tries_its_password(tmp_path):
    """Seen on EveryRole itself: the invited account was made by an emailed link, its generated password was never
    set; asking the model to pick between two forms got it stuck. Known by email: the email path only."""
    flow = login_flow(config(tmp_path, signin_by="email"), "member", base_url=BASE,
                      env=env(tmp_path, SW_MEMBER_PASSWORD="pw-123456"))
    assert "password" not in flow.values and flow.steps[0]["goal"] == EMAIL_ONLY_SIGNIN


def test_an_account_known_to_use_its_password_signs_in_as_usual(tmp_path):
    flow = login_flow(config(tmp_path, signin_by="password"), "member", base_url=BASE,
                      env=env(tmp_path, SW_MEMBER_PASSWORD="pw-123456"))
    assert not flow.steps and flow.goal == "Log in" and flow.values["password"] == "pw-123456"


def test_an_inbox_account_keeps_its_password_for_apps_without_links(tmp_path):
    flow = login_flow(config(tmp_path), "member", base_url=BASE, env=env(tmp_path, SW_MEMBER_PASSWORD="pw-123456"))
    assert flow.values["password"] == "pw-123456" and flow.inbox and len(flow.steps) == 3  # not known yet
    assert flow.steps[0]["goal"] == "Log in" + EMAIL_SIGNIN and flow.steps[1]["email_if_needed"]


def test_an_ordinary_test_account_signs_in_as_before(tmp_path):
    flow = login_flow(config(tmp_path), "member", base_url=BASE,
                      env=env(tmp_path, SW_MEMBER_EMAIL="member@example.com", SW_MEMBER_PASSWORD="pw-123456"))
    assert not flow.inbox and not flow.steps and flow.goal == "Log in"


LINK = f"{BASE}/login/link/tok123"
SCRIPTS = {"Log in": [{"label": "username_or_email", "kind": "fill"},
                      {"label": "Email me a link", "url": f"{BASE}/login/link", "text": "Check your email"}],
           EMAIL_ONLY_SIGNIN: [{"label": "username_or_email", "kind": "fill"},
                               {"label": "Email me a link", "url": f"{BASE}/login/link", "text": "Check your email"}],
           FINISH_SIGNIN: []}


class LinkBrowser(Browser):
    """Opening the emailed link signs in: the apps page. Its log-in page has a password field."""

    def __init__(self):
        super().__init__()
        self.url = f"{BASE}/login"

    def evaluate(self, expression):
        if 'type="password"' in expression:
            return 1 if self.url.endswith("/login") else 0
        return super().evaluate(expression)

    def call(self, method, **params):
        if method == "Page.navigate":
            self.opened = params["url"]
            self.url, self.text = f"{BASE}/apps", "Your apps"
        return {}


def deliver(folder, link, name):
    folder.mkdir(parents=True, exist_ok=True)
    message = {"from": "hello@everyrole.dev", "subject": "Your sign-in link", "received": "2026-10-08T10:00:00+00:00",
               "text": f"Sign in: {link}", "links": [link], "codes": []}
    (folder / f"{name}.json").write_text(json.dumps(message), encoding="utf-8")


def run(flow, tmp_path, arrives=None):
    agents = []
    ticks = iter(i * 0.25 for i in range(100000))
    folder = tmp_path / "inbox" / "member"

    def make(url, goal, **hooks):
        agent = JourneyAgent(SCRIPTS, url, goal, **hooks)
        agent.browser = LinkBrowser()
        agents.append(agent)
        return agent

    def sleep(_seconds):  # the app's email arrives while the step waits for it
        if arrives and not (folder / "b-new.json").exists():
            deliver(folder, arrives, "b-new")

    runner = FlowRunner(agent_factory=make, outbound_filters=lambda: [], value_chooser=Values,
                        clock=lambda: next(ticks), sleep=sleep, wait_stable=lambda *a, **k: None)
    return runner.run(flow, output_dir=tmp_path), agents


def test_the_link_that_arrives_signs_in_and_an_earlier_one_is_never_opened(tmp_path):
    deliver(tmp_path / "inbox" / "member", f"{BASE}/login/link/OLD", "a-old")  # yesterday's link
    flow = login_flow(config(tmp_path), "member", base_url=BASE, env=env(tmp_path))
    result, agents = run(flow, tmp_path, arrives=LINK)
    assert result.passed, result.steps_judged
    assert agents[0].browser.opened == LINK


def test_no_new_email_means_no_sign_in_and_says_so(tmp_path):
    deliver(tmp_path / "inbox" / "member", f"{BASE}/login/link/OLD", "a-old")
    flow = login_flow(config(tmp_path), "member", base_url=BASE, env=env(tmp_path))
    result, agents = run(flow, tmp_path)
    assert not result.passed and result.failed_step == 2
    assert "no email arrived" in json.dumps(result.steps_judged[1]["expectations"])
    assert not hasattr(agents[0].browser, "opened")  # the old link stayed closed
    assert result.cause == "signin" and "no email arrived" in result.error  # said plainly, never the app's fault


def test_a_page_without_a_password_field_is_not_a_sign_in_the_email_still_comes(tmp_path):
    """EveryRole's own sign-up: after asking for the link, "Check your email" has no password field, so "no password
    field" holds there. Without the password typed, the sign-in still waits for the email and opens it."""
    raw = {"login": {"start": "{{BASE_URL}}/login", "goal": "Log in", "expect": [{"password_field_absent": True}]},
           "personas": {"member": {"values": {"username_or_email": "{{secrets.SW_MEMBER_EMAIL}}",
                                              "password": "{{secrets.SW_MEMBER_PASSWORD}}"}}}}
    path = tmp_path / "everyrole.config.json"
    path.write_text(json.dumps(raw))
    flow = login_flow(load_config(path), "member", base_url=BASE, env=env(tmp_path, SW_MEMBER_PASSWORD="pw-123456"))
    result, agents = run(flow, tmp_path, arrives=LINK)
    assert result.passed and agents[0].browser.opened == LINK  # waited for the link instead of a false pass


def test_a_password_sign_in_needs_no_email(tmp_path):
    scripts = {**SCRIPTS, "Log in": [{"label": "username_or_email", "kind": "fill"},
                                     {"label": "password", "kind": "fill"},
                                     {"label": "Log in", "url": f"{BASE}/apps", "text": "Your apps"}]}
    flow = login_flow(config(tmp_path), "member", base_url=BASE, env=env(tmp_path, SW_MEMBER_PASSWORD="pw-123456"))
    original = dict(SCRIPTS)
    SCRIPTS.update(scripts)
    try:
        result, agents = run(flow, tmp_path)  # no email ever comes
    finally:
        SCRIPTS.clear()
        SCRIPTS.update(original)
    assert result.passed and not hasattr(agents[0].browser, "opened")
