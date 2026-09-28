"""Deterministic `expect` checks. These are the only source of pass/fail; the agent's DONE is never trusted.

Each check type is one class registered by its schema key, so adding a type never edits existing ones.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PageFacts:
    """What the checks may look at: the current URL, the page's rendered text, and visible element labels."""

    url: str
    text: str
    labels: tuple
    blocked: tuple = ()  # write attempts stopped by the write-guard: (method, url)
    passwords: int = 0  # visible password fields (a page still asking for a password is not signed in)


@dataclass(frozen=True)
class Outcome:
    kind: str
    expected: str
    passed: bool
    detail: str

    def as_dict(self):
        return {"kind": self.kind, "expected": self.expected, "passed": self.passed, "detail": self.detail}


def _normalize(text):
    return re.sub(r"\s+", " ", text or "").strip().casefold()


class Expectation:
    kind = ""

    def __init__(self, expected):
        self.expected = expected

    def check(self, facts):
        passed, detail = self._check(facts)
        return Outcome(self.kind, self.expected, passed, detail)

    def _check(self, facts):
        raise NotImplementedError


REGISTRY = {}


def register(cls):
    REGISTRY[cls.kind] = cls
    return cls


@register
class UrlContains(Expectation):
    kind = "url_contains"

    def _check(self, facts):
        return self.expected in facts.url, f"URL is {facts.url}"


@register
class UrlMatches(Expectation):
    kind = "url_matches"

    def _check(self, facts):
        return re.search(self.expected, facts.url) is not None, f"URL is {facts.url}"


@register
class TextVisible(Expectation):
    kind = "text_visible"

    def _check(self, facts):
        found = _normalize(self.expected) in _normalize(facts.text)
        return found, "found on page" if found else "not found in page text"


@register
class TextAbsent(Expectation):
    kind = "text_absent"

    def _check(self, facts):
        found = _normalize(self.expected) in _normalize(facts.text)
        return not found, "present on page" if found else "absent"


@register
class ElementVisible(Expectation):
    kind = "element_visible"

    def _check(self, facts):
        wanted = _normalize(self.expected)
        match = next((label for label in facts.labels if wanted in _normalize(label)), None)
        return match is not None, f"matched {match!r}" if match else "no visible control with that label"


@register
class ElementAbsent(Expectation):
    kind = "element_absent"

    def _check(self, facts):
        wanted = _normalize(self.expected)
        match = next((label for label in facts.labels if wanted in _normalize(label)), None)
        return match is None, f"found {match!r}" if match else "no visible control with that label"


@register
class UrlNotContains(Expectation):
    kind = "url_not_contains"

    def _check(self, facts):
        return self.expected not in facts.url, f"URL is {facts.url}"


@register
class TriedToSave(Expectation):
    kind = "tried_to_save"

    def _check(self, facts):
        hits = [f"{m} {u}" for m, u in facts.blocked if m != "BEACON" and (self.expected == "*" or self.expected in u)]
        if hits:
            return True, f"blocked {hits[0]}"
        return False, "no save attempt" if not facts.blocked else f"other attempts: {facts.blocked[0][1]}"


@register
class PasswordFieldAbsent(Expectation):
    """Signed in means no password is being asked for any more: a two-step sign-in's password screen, or a
    sign-in form shown again after a refused attempt, still has one."""

    kind = "password_field_absent"

    def _check(self, facts):
        return facts.passwords == 0, "no password field" if facts.passwords == 0 else "a password field is shown"


def build(expect):
    """expect: iterable of single-key dicts, already schema-validated."""
    return [REGISTRY[kind](value) for item in expect for kind, value in item.items()]


def evaluate(expectations, facts):
    return [e.check(facts) for e in expectations]
