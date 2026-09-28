import httpx

from everyrole import doctor


def test_ready_machine():
    checks = doctor.run({"CHROME_PATH": "/usr/bin/chrome", "TYPESAFE_API_KEY": "k", "TEXT_MODEL_API_KEY": "k",
                         "TEXT_MODEL_BASE_URL": "u", "TEXT_MODEL": "m"},
                        provider_url="https://gateway.example", get=lambda url: None,
                        which=lambda name: "/usr/bin/ffmpeg", version=(3, 12, 1))
    text, code = doctor.report(checks)
    assert code == 0 and "Ready to test." in text and "FAIL" not in text


def test_missing_run_key_blocks_but_missing_extras_only_warn():
    def offline(url):
        raise httpx.ConnectError("refused")

    checks = doctor.run({"CHROME_PATH": "/usr/bin/chrome"}, provider_url="https://gateway.example", get=offline,
                        which=lambda name: None, version=(3, 13, 0))
    text, code = doctor.report(checks)
    assert code == 1
    assert "FAIL  Model keys for running tests: missing TYPESAFE_API_KEY" in text
    assert "warn  Model keys for typing free text (optional)" in text and "warn  ffmpeg" in text
    assert "FAIL  Model provider reachable" in text


def test_old_python_fails():
    assert not doctor.python_version((3, 11, 9)).ok
