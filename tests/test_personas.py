import json

import pytest

from everyrole.spec import SpecError, load_config, login_flow, parse_flows

ENV = {"PAID_EMAIL": "paid@example.com", "PAID_PASSWORD": "paid-secret", "FREE_EMAIL": "free@example.com",
       "FREE_PASSWORD": "free-secret"}

CONFIG = {
    "login": {"start": "{{BASE_URL}}/login", "goal": "Sign in", "expect": [{"text_absent": "SIGN IN"}]},
    "personas": {
        "paid": {"values": {"email": "{{secrets.PAID_EMAIL}}", "password": "{{secrets.PAID_PASSWORD}}"}},
        "free": {"values": {"email": "{{secrets.FREE_EMAIL}}", "password": "{{secrets.FREE_PASSWORD}}"}},
        "guest": {"login": False},
    },
    "access": {"/studio/films": {"allow": ["paid", "free"], "deny": ["guest"]}},
    "denied_when": [{"url_contains": "/login"}],
}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "everyrole.config.json"
    path.write_text(json.dumps(CONFIG))
    return load_config(path)


def test_config_loads_with_schema_defaults(config):
    assert set(config.personas) == {"paid", "free", "guest"}
    assert not config.personas["guest"].login and config.personas["paid"].login
    assert config.login["timeout_s"] == 60 and config.access_settle_s == 5


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda c: c["access"]["/x"].__setitem__("deny", ["ghost"]) if "/x" in c["access"] else
         c["access"].__setitem__("/x", {"deny": ["ghost"]}), "unknown persona 'ghost'"),
        (lambda c: c.pop("login"), "need a `login` section"),
        (lambda c: c["personas"].__setitem__("Bad Name", {}), "personas"),
    ],
)
def test_config_errors_are_clear(tmp_path, change, message):
    raw = json.loads(json.dumps(CONFIG))
    change(raw)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(SpecError, match=message):
        load_config(path)


def test_missing_default_config_means_no_personas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.personas == {} and config.access == {}


def flow(**overrides):
    raw = {"name": "invite", "start": "{{BASE_URL}}/team", "goal": "Invite everyrole-{{RUN_ID}}@example.com"}
    raw.update(overrides)
    return raw


def test_as_map_expands_one_run_per_persona(config):
    raw = flow(as_={})
    raw.pop("as_")
    raw["as"] = {
        "paid": {"expect": [{"text_visible": "Invitation sent"}]},
        "free": {"expect_denied": True, "expect": [{"element_absent": "Invite"}]},
    }
    runs = parse_flows(raw, config=config, base_url="https://app.io", env=ENV, run_id="r1")
    by = {f.persona: f for f in runs}
    assert [f.name for f in runs] == ["invite@paid", "invite@free"]
    assert {f.source for f in runs} == {"<flow>"}  # the real file path, never the "(as persona)" label
    assert by["free"].expect_denied and not by["paid"].expect_denied
    assert by["paid"].goal == "Invite everyrole-r1@example.com"
    assert by["paid"].values == {"email": "paid@example.com", "password": "paid-secret"}
    assert by["paid"].secrets == {"PAID_EMAIL": "paid@example.com", "PAID_PASSWORD": "paid-secret"}
    assert "PAID_PASSWORD" not in by["free"].secrets  # each run tracks only its own persona's secrets


def test_as_string_and_flow_values_override_persona_values(config):
    raw = flow(**{"as": "paid", "values": {"email": "other@example.com"}, "expect": [{"url_contains": "/team"}]})
    (run,) = parse_flows(raw, config=config, base_url="https://app.io", env=ENV, run_id="r1")
    assert run.persona == "paid" and run.values["email"] == "other@example.com"


def test_unknown_persona_and_missing_expect_fail(config):
    with pytest.raises(SpecError, match="unknown persona 'admin'"):
        parse_flows(flow(**{"as": "admin", "expect": [{"url_contains": "/"}]}), config=config,
                    base_url="https://app.io", env=ENV, run_id="r")
    with pytest.raises(SpecError, match="needs `expect`"):
        parse_flows(flow(**{"as": {"paid": {"expect_denied": True}}}), config=config,
                    base_url="https://app.io", env=ENV, run_id="r")


def test_run_id_template_requires_a_run_id(config):
    with pytest.raises(SpecError, match="no run id"):
        parse_flows(flow(expect=[{"url_contains": "/"}]), config=config, base_url="https://app.io", env=ENV)


def test_login_flow_built_from_config(config):
    login = login_flow(config, "free", base_url="https://app.io", env=ENV)
    assert login.name == "login@free" and login.is_login and login.persona == "free"
    assert login.start == "https://app.io/login" and login.values["password"] == "free-secret"
    assert login.secrets == {"FREE_EMAIL": "free@example.com", "FREE_PASSWORD": "free-secret"}
    assert login.timeout_s == 60 and login.allowed_hosts == ("app.io",)


def test_access_rules_work_without_declared_refusal_signals(tmp_path):
    raw = json.loads(json.dumps(CONFIG))
    raw.pop("denied_when")
    path = tmp_path / "c.json"
    path.write_text(json.dumps(raw))
    assert load_config(path).access  # redirect-away detection needs no configuration
