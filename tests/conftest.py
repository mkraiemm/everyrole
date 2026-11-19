import pytest


@pytest.fixture(autouse=True)
def no_gateway_model_list(monkeypatch):
    """Tests never ask the real AI gateway which models it has: every model is treated as undescribed (requests
    keep their full shape) unless a test sets its own list."""
    monkeypatch.setattr("jev_ultrafast.model._gateway_models", lambda base: {})


@pytest.fixture(autouse=True)
def no_model_probe(monkeypatch):
    """Nor do they call a model to see whether it answers (the daily model check, `everyrole doctor`): the gateway
    is unreachable unless a test brings its own."""
    from everyrole import model_check

    monkeypatch.setattr(model_check, "probe",
                        lambda base, key, model, post=None: model_check.Answer(model, None, "tests call no model"))


