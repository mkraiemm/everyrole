"""Deterministic `expect` checks. These are the only source of pass/fail; the agent's DONE is never trusted.

Each check type is one class registered by its schema key, so adding a type never edits existing ones.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

SIMILAR = 0.6  # how alike a text on the page must be to count as the same text, reworded


@dataclass(frozen=True)
class PageFacts:
    """What the checks may look at: the current URL, the page's rendered text, and visible element labels."""

    url: str
    text: str
    labels: tuple
    blocked: tuple = ()  # write attempts stopped by the write-guard: (method, url)
    passwords: int = 0  # visible password fields (a page still asking for a password is not signed in)
    typed: tuple = ()  # a journey: what it typed in its earlier steps (secrets never), for the "kept" check
    # a journey: it typed earlier, but on another page than this one (the runner judges "kept" there, not here)
    kept_elsewhere: bool = False
    emails: tuple = ()  # a journey that signed up: the emails its new account got, as "subject" + newline + "text"
    # the messages the page showed in browser dialogs (alert, confirm) during this step: they are on screen too
    dialogs: tuple = ()


@dataclass(frozen=True)
class Outcome:
    kind: str
    expected: str
    passed: bool
    detail: str
    closest: str = ""  # a failed text/label check: the most similar text on the page (it was reworded)

    def as_dict(self):
        out = {"kind": self.kind, "expected": self.expected, "passed": self.passed, "detail": self.detail}
        return {**out, "closest": self.closest} if self.closest else out


# typographic quotes and dashes read as the plain ones: a check typed "don't" finds a page's "don’t"
PLAIN = str.maketrans({"‘": "'", "’": "'", "‚": "'", "′": "'", "“": '"', "”": '"',
                       "„": '"', "″": '"', "–": "-", "—": "-", "−": "-"})


def plain_text(text):
    """A text as checks compare it: one space between words, plain quotes and dashes, no case."""
    return re.sub(r"\s+", " ", str(text or "")).translate(PLAIN).strip().casefold()


def _normalize(text):
    return plain_text(text)


def _contains(expected, text):
    """`expected` is in `text`. Exploring shows numbers to the AI as "#" (so a count that changes does not break a
    test), so a "#" in a check stands for any number on the page: "#. Refunds" matches "1. Refunds"."""
    wanted, have = _normalize(expected), _normalize(text)
    if wanted in have:
        return True
    return "#" in wanted and wanted in re.sub(r"\d+", "#", have)


def _names(expected, label):
    """A control's label names `expected` as whole words: "Role" is not in "new-…@in.everyrole.dev" (a
    field's typed address once matched a check of a "Role" control). "#" stands for a number."""
    wanted, have = _normalize(expected), _normalize(label)
    if "#" in wanted:
        wanted, have = wanted, re.sub(r"\d+", "#", have)
    return re.search(r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", have) is not None


def _words(text):
    return re.findall(r"\w+", _normalize(text))


def closest(expected, candidates):
    """The candidate most like `expected` when it is alike enough to be the same text reworded, else "". Alike
    means similar letters AND similar words, and only for a text of 3 words or more: a short name that is gone
    (an item of the test account, "Your films") is missing, never "reworded"."""
    wanted, wanted_words = _normalize(expected), set(_words(expected))
    if len(_words(expected)) < 3:
        return ""
    best, score = "", 0.0
    for candidate in candidates:
        have = _normalize(candidate)
        if not have or not 0.5 <= len(have) / max(1, len(wanted)) <= 2:
            continue
        letters = SequenceMatcher(None, wanted, have).ratio()
        words = set(_words(candidate))
        shared = 2 * len(wanted_words & words) / max(1, len(wanted_words) + len(words))
        if letters >= SIMILAR and shared >= SIMILAR and min(letters, shared) > score:
            best, score = candidate.strip(), min(letters, shared)
    return best


def other_number(expected, text):
    """The page's text where only the numbers of `expected` differ ("Tests 38" -> "tests 32"), else "": a count that
    grew as the app was used says the same thing differently, it is never broken."""
    wanted = plain_text(expected)
    if not re.search(r"\d", wanted):
        return ""
    pattern = r"\d+".join(re.escape(part) for part in re.split(r"\d+", wanted))
    page = re.sub(r"\s+", " ", str(text or "")).translate(PLAIN)  # the page's own words, as people read them
    found = re.search(pattern, page, re.IGNORECASE)
    return found.group(0).strip() if found else ""


def _pieces(text):
    """A page's text as the lines and sentences a check could have been copied from, and runs of 2-3 lines (a
    heading split over lines: "Start free." / "Pay when you need it.")."""
    lines = [line.strip() for line in re.split(r"\n+", text or "") if line.strip()]
    sentences = [s.strip() for line in lines for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]
    runs = [" ".join(lines[i:i + n]) for n in (2, 3) for i in range(len(lines) - n + 1)]
    return list(dict.fromkeys(lines + sentences + runs))


class Expectation:
    kind = ""

    def __init__(self, expected):
        self.expected = expected

    def check(self, facts):
        passed, detail = self._check(facts)
        similar = "" if passed else self._reworded(facts)
        return Outcome(self.kind, self.expected, passed, detail, similar)

    def _reworded(self, facts):
        return ""  # only checks of a text or a label can have been reworded

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
        found = _contains(self.expected, facts.text)
        if found:
            return True, "found on page"
        # words written inside an empty field (its placeholder) are on screen too, though not in the page's text
        # (Conduit, 2026-11-18: "Email" and "Password" are its sign-up form's placeholders: 5 steps failed on them)
        field = next((label for label in facts.labels if _names(self.expected, label)), None)
        if field:
            return True, f"shown as {field!r}"
        # said in a browser alert (demoblaze, 2026-11-18: "Product added." and "Sign up successful." are alerts: the
        # same check failed 4 rounds as "EveryRole's guess")
        if any(_contains(self.expected, message) for message in facts.dialogs):
            return True, "shown in a browser alert"
        return False, "not found in page text"

    def _reworded(self, facts):
        return closest(self.expected, _pieces(facts.text)) or other_number(self.expected, facts.text)


@register
class TextAbsent(Expectation):
    kind = "text_absent"

    def _check(self, facts):
        found = _contains(self.expected, facts.text)
        return not found, "present on page" if found else "absent"


@register
class ElementVisible(Expectation):
    kind = "element_visible"

    def _check(self, facts):
        match = next((label for label in facts.labels if _names(self.expected, label)), None)
        return match is not None, f"matched {match!r}" if match else "no visible control with that label"

    def _reworded(self, facts):
        return closest(self.expected, facts.labels)


@register
class ElementAbsent(Expectation):
    kind = "element_absent"

    def _check(self, facts):
        match = next((label for label in facts.labels if _names(self.expected, label)), None)
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
        # a "#" stands for any number, as in text checks: "/workspaces/#/plan" matches whichever row was used
        hits = [f"{m} {u}" for m, u in facts.blocked if m != "BEACON" and (
            self.expected == "*" or self.expected in u
            or ("#" in self.expected and self.expected in re.sub(r"\d+", "#", u)))]
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


@register
class SigninFormShown(Expectation):
    """Signed out, from the page itself: its address is a sign-in page (the regex) AND it asks for a password. The
    address alone can't say it: a sign-in in a pop-up lives on a page every signed-in person sees too (the home page),
    and a sign-in form on the home page turns into the app there once signed in."""

    kind = "signin_form_shown"

    def _check(self, facts):
        if re.search(self.expected, facts.url) is None:
            return False, f"URL is {facts.url}"
        return facts.passwords > 0, "the sign-in form is shown" if facts.passwords else "no password field"


@register
class CountOf(Expectation):
    """How many times a text appears on the page ({"text", "equals" | "at_least" | "at_most"}): "the film started
    once and only once" is one card on the page after a reload, not two."""

    kind = "count"
    BOUNDS = (("equals", "exactly", lambda n, b: n == b), ("at_least", "at least", lambda n, b: n >= b),
              ("at_most", "at most", lambda n, b: n <= b))

    def check(self, facts):
        passed, detail = self._check(facts)
        return Outcome(self.kind, self.describe(), passed, detail)

    def describe(self):
        wanted = [f"{words} {self.expected[key]}" for key, words, _ in self.BOUNDS if key in self.expected]
        times = "time" if [self.expected[k] for k, _, _ in self.BOUNDS if k in self.expected] == [1] else "times"
        return f"“{self.expected['text']}” {' and '.join(wanted)} {times}"

    def _check(self, facts):
        found = _occurrences(self.expected["text"], facts.text)
        passed = all(test(found, self.expected[key]) for key, _, test in self.BOUNDS if key in self.expected)
        return passed, f"found {found} time{'s' if found != 1 else ''}"


KEPT_LATER = "judged once back on the page it was typed on"


@register
class Kept(Expectation):
    """Nothing lost: everything the journey typed in its earlier steps is still on the page (after signing up or
    logging in partway, the visitor's work is still there, not a fresh page)."""

    kind = "kept"

    def check(self, facts):
        passed, detail = self._check(facts)
        return Outcome(self.kind, "what was typed earlier is still on the page", passed, detail)

    def _check(self, facts):
        if facts.kept_elsewhere:  # not back on the page it was typed on yet: a later step judges it (runner.py)
            return True, KEPT_LATER
        typed = [t for t in dict.fromkeys(facts.typed) if len(_normalize(t)) >= 3]
        if not typed:
            return True, "nothing was typed earlier"
        missing = [t for t in typed if not _contains(t, facts.text) and not _contains(_bare(t), facts.text)]
        if missing:
            return False, "missing " + ", ".join(f"“{m[:60]}”" for m in missing[:3])
        return True, f"all {len(typed)} still on the page"


@register
class EmailReceived(Expectation):
    """An email to the journey's new account whose subject or text contains this (the welcome email)."""

    kind = "email_received"

    def _check(self, facts):
        found = next((e for e in facts.emails if _contains(self.expected, e)), None)
        if found:
            return True, f"got “{found.splitlines()[0][:80]}”"
        if not facts.emails:
            return False, "no email arrived"
        # what did arrive, by subject: the next plan checks those words (the walk), never a guess again
        subjects = ", ".join(f"“{e.splitlines()[0][:80]}”" for e in facts.emails[:3])
        return False, f"{len(facts.emails)} email(s) arrived, none says it: {subjects}"


def _bare(typed):
    """A typed address as pages show it back: "https://www.cal.com/" -> "cal.com"."""
    return re.sub(r"^(https?://)?(www\.)?", "", typed.strip(), flags=re.I).rstrip("/")


def _occurrences(expected, text):
    """How many times `expected` appears in `text`, not overlapping (same matching as text checks: spaces and case
    don't matter, "#" stands for any number)."""
    wanted, have = _normalize(expected), _normalize(text)
    if "#" in wanted:
        have = re.sub(r"\d+", "#", have)
    return have.count(wanted) if wanted else 0


def build(expect):
    """expect: iterable of single-key dicts, already schema-validated."""
    return [REGISTRY[kind](value) for item in expect for kind, value in item.items()]


def evaluate(expectations, facts):
    return [e.check(facts) for e in expectations]
