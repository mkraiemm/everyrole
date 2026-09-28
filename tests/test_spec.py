import json

import pytest

from everyrole import assertions
from everyrole.spec import SpecError, load_flows, parse_flow, schema

ENV = {"QA_EMAIL": "qa@example.com", "QA_PASSWORD": "p@ss\"word"}


def flow(**overrides):
    raw = {
        "name": "login",
        "start": "{{BASE_URL}}/login",
        "goal": "Log in and open the dashboard",
        "values": {"email": "{{secrets.QA_EMAIL}}", "password": "{{ secrets.QA_PASSWORD }}", "plan": "pro"},
        "expect": [{"url_contains": "/dashboard"}],
    }
    raw.update(overrides)
    return raw


def test_interpolates_base_url_and_secrets():
    f = parse_flow(flow(), base_url="https://app.example.com/", env=ENV)
    assert f.start == "https://app.example.com/login"
    assert f.values == {"email": "qa@example.com", "password": 'p@ss"word', "plan": "pro"}
    assert f.secrets == {"QA_EMAIL": "qa@example.com", "QA_PASSWORD": 'p@ss"word'}


def test_defaults_come_from_schema():
    f = parse_flow(flow(), base_url="https://app.example.com", env=ENV)
    props = schema()["properties"]
    assert f.max_steps == props["max_steps"]["default"]
    assert f.timeout_s == props["timeout_s"]["default"]
    assert f.forbid == () and f.run_on == ()


def test_allowed_hosts_default_to_start_host():
    f = parse_flow(flow(), base_url="https://app.example.com", env=ENV)
    assert f.allowed_hosts == ("app.example.com",)
    f = parse_flow(flow(allowed_hosts=["{{BASE_HOST}}", "auth.example.com"]), base_url="https://a.b.c", env=ENV)
    assert f.allowed_hosts == ("a.b.c", "auth.example.com")


def test_secret_used_in_goal_is_tracked():
    f = parse_flow(flow(goal="Log in as {{secrets.QA_EMAIL}}", values={}), base_url="https://x.io", env=ENV)
    assert f.secrets == {"QA_EMAIL": "qa@example.com"}


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"name": "Bad Name"}, "name"),
        ({"expect": []}, "expect"),
        ({"expect": [{"url_contains": "a", "text_visible": "b"}]}, "expect/0"),
        ({"expect": [{"sparkles": "x"}]}, "expect/0"),
        ({"surprise": 1}, "surprise"),
        ({"max_steps": 0}, "max_steps"),
    ],
)
def test_schema_errors_name_the_field(overrides, message):
    with pytest.raises(SpecError) as err:
        parse_flow(flow(**overrides), base_url="https://x.io", env=ENV, source="flows/login.json")
    assert "flows/login.json" in str(err.value) and message in str(err.value)


def test_missing_secret_and_unknown_template_fail_clearly():
    with pytest.raises(SpecError, match="needs env var QA_EMAIL"):
        parse_flow(flow(), base_url="https://x.io", env={"QA_PASSWORD": "x"})
    with pytest.raises(SpecError, match="unknown template"):
        parse_flow(flow(goal="{{NOPE}}"), base_url="https://x.io", env=ENV)
    with pytest.raises(SpecError, match="no --base-url"):
        parse_flow(flow(), base_url=None, env=ENV)


def test_start_must_be_http():
    with pytest.raises(SpecError, match="http"):
        parse_flow(flow(start="javascript:alert(1)"), base_url="https://x.io", env=ENV)


def test_load_flows_rejects_duplicates_and_bad_json(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(flow()))
    (tmp_path / "b.json").write_text(json.dumps(flow()))
    with pytest.raises(SpecError, match="Duplicate flow name"):
        load_flows([tmp_path], base_url="https://x.io", env=ENV)
    (tmp_path / "b.json").write_text("{ nope")
    with pytest.raises(SpecError, match="invalid JSON"):
        load_flows([tmp_path], base_url="https://x.io", env=ENV)


def test_run_on_filter():
    f = parse_flow(flow(run_on=["preview"]), base_url="https://x.io", env=ENV)
    assert f.runs_in("preview") and not f.runs_in("production")
    assert parse_flow(flow(), base_url="https://x.io", env=ENV).runs_in("anything")


def test_schema_and_assertion_registry_agree():
    keys = set(schema()["$defs"]["check"]["properties"])
    assert keys == set(assertions.REGISTRY)


@pytest.mark.parametrize("example", ["quickstart", "saas-app"])
def test_examples_are_valid(example):
    from pathlib import Path

    from everyrole.spec import load_config, login_flow

    root = Path(__file__).resolve().parents[1] / "examples" / example
    config = load_config(root / "everyrole.config.json")
    env = {name: "placeholder-value" for name in ("DEMO_EMAIL", "DEMO_PASSWORD", "ADMIN_EMAIL", "ADMIN_PASSWORD",
                                                  "PRO_EMAIL", "PRO_PASSWORD", "FREE_EMAIL", "FREE_PASSWORD")}
    for persona in (p for p, spec in config.personas.items() if spec.login):
        login_flow(config, persona, base_url="https://app.example.com", env=env, run_id="t")
    flows = load_flows([root / "flows"], config=config, base_url="https://app.example.com", env=env, run_id="t")
    assert flows and all(f.expect for f in flows)
