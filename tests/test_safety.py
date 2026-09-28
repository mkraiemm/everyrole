import json

import pytest

from everyrole.guard import ForbiddenAction, Guard, LeftAllowedHosts
from everyrole.redact import Redactor


def test_redactor_replaces_plain_and_json_escaped_literals():
    r = Redactor({"QA_PASSWORD": 'p@ss"wörd\\', "QA_EMAIL": "qa@example.com"})
    payload = json.dumps({"typed": 'p@ss"wörd\\', "note": "user qa@example.com"})
    ascii_payload = json.dumps({"typed": 'p@ss"wörd\\'}, ensure_ascii=True)
    for text in (payload, ascii_payload, 'raw p@ss"wörd\\ here'):
        out = r.apply(text)
        assert "p@ss" not in out and "[secret:QA_PASSWORD]" in out
    assert "qa@example.com" not in r.apply(payload)


def test_redactor_prefers_longest_secret_and_scrubs_structures():
    r = Redactor({"SHORT": "abc", "LONG": "abcdef"})
    assert r.apply("x abcdef y abc") == "x [secret:LONG] y [secret:SHORT]"
    assert r.scrub({"a": ["abc", {"abcdef": 1}], "n": 3}) == {"a": ["[secret:SHORT]", {"[secret:LONG]": 1}], "n": 3}
    assert not Redactor({"EMPTY": ""}).active


def test_forbid_is_whole_word_and_case_insensitive():
    guard = Guard(forbid=["Pay", "Delete account"])
    with pytest.raises(ForbiddenAction):
        guard.check_action({"label": "pay now", "kind": "click"})
    with pytest.raises(ForbiddenAction):
        guard.check_action({"label": "Settings → DELETE ACCOUNT", "kind": "click"})
    for safe in ("Display settings", "Payment history", "Delete draft"):
        guard.check_action({"label": safe, "kind": "click"})


def test_allowed_hosts_include_subdomains_only():
    guard = Guard(allowed_hosts=["example.com"])
    guard.check_url("https://example.com/dashboard")
    guard.check_url("https://app.example.com/x")
    guard.check_url("about:blank")
    for bad in ("https://evil.io", "https://example.com.evil.io", "https://notexample.com"):
        with pytest.raises(LeftAllowedHosts):
            guard.check_url(bad)
