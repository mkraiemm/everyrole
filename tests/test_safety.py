import json

import pytest

from everyrole.guard import ForbiddenAction, Guard, LeftAllowedHosts, address_words
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


@pytest.mark.parametrize("href", ["https://shop.io/delete_account", "https://shop.io/account/delete-account?x=1",
                                  "https://shop.io/deleteAccount", "/settings/Delete%20Account"])
def test_a_link_is_refused_by_the_words_of_its_address(href):
    # a plain link deletes as surely as a button: "/delete_account" reads "delete account"
    guard = Guard(forbid=["Delete"])
    assert address_words(href).lower().split()[-2:] == ["delete", "account"]
    assert guard.forbidden_link("Account", href=href) == "Delete"
    with pytest.raises(ForbiddenAction):
        guard.check_action({"label": "🗑", "href": href, "kind": "click"})


def test_a_link_is_refused_by_any_of_its_names_but_not_by_a_longer_word():
    guard = Guard(forbid=["Sign out", "Delete"])
    assert guard.forbidden_link("", "Sign out", href="https://app.io/session") == "Sign out"  # its aria-label
    assert guard.forbidden_link("Bye", href="https://app.io/sign-out") == "Sign out"
    for href in ("https://app.io/deleted-items", "https://app.io/products?delete=1", "https://app.io/sign-up"):
        assert guard.forbidden_link("Open", href=href) is None  # another word, or only in the query
    guard.check_action({"label": "Deleted items", "href": "https://app.io/deleted-items", "kind": "click"})


def test_allowed_hosts_include_subdomains_only():
    guard = Guard(allowed_hosts=["shop.example.com"])
    guard.check_url("https://shop.example.com/dashboard")
    guard.check_url("https://app.shop.example.com/x")
    guard.check_url("about:blank")
    for bad in ("https://evil.io", "https://shop.example.com.evil.io", "https://notshop.example.com"):
        with pytest.raises(LeftAllowedHosts):
            guard.check_url(bad)
