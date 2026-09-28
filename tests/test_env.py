from everyrole.env import load_dotenv


def test_env_file_never_overrides_real_variables(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nTYPESAFE_API_KEY=from-file\nQA_EMAIL=qa@example.com\n\nBROKEN LINE\n")
    environ = {"TYPESAFE_API_KEY": "from-ci"}
    assert load_dotenv(env_file, environ) == ["QA_EMAIL"]
    assert environ == {"TYPESAFE_API_KEY": "from-ci", "QA_EMAIL": "qa@example.com"}


def test_empty_model_settings_count_as_unset(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TYPESAFE_URL=https://gateway.example/typesafe/v1/systemone\n")
    environ = {"TYPESAFE_URL": "", "TYPESAFE_MODEL": "", "QA_PASSWORD": ""}
    load_dotenv(env_file, environ)
    assert environ == {"TYPESAFE_URL": "https://gateway.example/typesafe/v1/systemone", "QA_PASSWORD": ""}


def test_missing_file_is_fine(tmp_path):
    assert load_dotenv(tmp_path / "absent.env", {}) == []
