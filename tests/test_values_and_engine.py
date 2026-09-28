import json

import pytest
from jev_ultrafast import model

from everyrole.redact import Redactor
from everyrole.values import WRITE, ValueChooser

CONTEXT = {
    "goal": "Log in with the test account",
    "field": {"label": "Password", "role": "textbox", "value": ""},
    "page": {"title": "Sign in", "text": "Sign in to Acme"},
    "recent_actions": [],
}


@pytest.fixture(autouse=True)
def provider_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setenv("TYPESAFE_URL", "https://gateway.example/typesafe/v1/systemone")
    monkeypatch.setenv("TYPESAFE_MODEL", "typesafe-ai/jev")


def answer(choice, names):
    probabilities = {n: (1.0 if n == choice else 0.0) for n in names}
    return {"answers": {"value": {"type": "choice", "choice": choice, "probabilities": probabilities,
                                  "confidence": 1}}, "usage": {"input_tokens": 42}}


def test_chooser_sends_only_value_names_and_types_literal_locally():
    sent = []
    values = {"email": "qa@example.com", "password": "hunter2-secret", "site": "https://example.com"}

    def post(url, key, body):
        sent.append((url, body))
        return answer("password", [*values, WRITE])

    chooser = ValueChooser(values, ("qa@example.com", "hunter2-secret"), post=post, write=None)
    text, helper = chooser(CONTEXT, {"label": "Password"}, {})
    assert text == "hunter2-secret"
    assert helper["value_choice"] == "password" and helper["usage"] == {"input_tokens": 42}
    url, body = sent[0]
    assert url == "https://gateway.example/typesafe/v1/systemone" and body["model"] == "typesafe-ai/jev"
    criteria = body["questions"]["value"]["criteria"]
    assert set(criteria) == {"email", "password", "site", WRITE}
    assert "hunter2-secret" not in json.dumps(body) and "qa@example.com" not in json.dumps(body)
    assert "https://example.com" in criteria["site"]  # non-secret values are shown so Jev can match their shape
    assert "hidden" in criteria["password"]


def test_chooser_write_falls_back_to_text_llm():
    written = []

    def write(context):
        written.append(context)
        return "free text", {"model": "text-llm", "latency_ms": 5}

    chooser = ValueChooser({"email": "qa@example.com"}, post=lambda *a: answer(WRITE, ["email", WRITE]), write=write)
    text, helper = chooser(CONTEXT, {"label": "Search"}, {})
    assert text == "free text" and helper["value_choice"] == WRITE and written == [CONTEXT]


def test_chooser_without_values_uses_text_llm_directly():
    chooser = ValueChooser({}, post=None, write=lambda c: ("x", {"model": "m"}))
    assert chooser(CONTEXT, {"label": "Search"}, {}) == ("x", {"model": "m"})


def test_chooser_rejects_invalid_model_answer():
    bad = {"answers": {"value": {"choice": "nope", "probabilities": {}, "confidence": 1}}}
    with pytest.raises(ValueError):
        ValueChooser({"email": "e"}, post=lambda *a: bad, write=None)(CONTEXT, {"label": "Email"}, {})


def test_engine_outbound_filter_redacts_every_request(monkeypatch):
    captured = []

    class Response:
        status_code, is_error = 200, False

        @staticmethod
        def json():
            return {"ok": True}

    monkeypatch.setattr(model.CLIENT, "post", lambda url, json, headers: captured.append(json) or Response())
    redactor = Redactor({"QA_PASSWORD": 'hun"ter2'})
    model.OUTBOUND_FILTERS.append(redactor.apply)
    try:
        model.post_json("https://x", "k", {"recent_actions": [{"text": 'hun"ter2'}], "n": 1})
    finally:
        model.OUTBOUND_FILTERS.remove(redactor.apply)
    assert captured == [{"recent_actions": [{"text": "[secret:QA_PASSWORD]"}], "n": 1}]


def test_engine_endpoint_resolution(monkeypatch):
    monkeypatch.delenv("TYPESAFE_URL")
    monkeypatch.delenv("TYPESAFE_MODEL")
    assert model.typesafe_url() == "https://api.typesafe.ai/v1/systemone"
    assert model.typesafe_model() == "jev-latest"


def test_engine_snapshot_masks_password_values():
    from pathlib import Path

    source = (Path(model.__file__).with_name("snapshot.js")).read_text(encoding="utf-8")
    assert "!['file','hidden'].includes(e.type)" in source  # password inputs are offered as targets
    assert "String(shown(e))" in source  # ...but their value is reported masked
    assert "secret(e)?e.value.length:e.value" in source  # fingerprints use length only
    assert "'tel','password'].includes(e.type)) return 'textbox'" in source  # and they get a textbox role
    assert "kind:'submit'" in source and "'Press Enter in '" in source  # P12: inputs can be submitted with Enter
