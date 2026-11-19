"""How much a test matters: critical, important or minor.

Not every test matters the same: "a visitor can read a blog post" is not "a visitor can sign up". The level is
saved in the test (`importance`, with a one-line `importance_why`) and drives the order people see tests in, the
health sentence and what a run's summary counts apart (`summary.by_importance`, so alerts can be about what
matters).

Who decides: the test writer, in the same call that writes the test (propose.INSTRUCTIONS has the rule), from what
it saw on the pages. No word or address list here decides a level. A person changes it in one click; a level set by
a person is marked (`importance_set_by: "person"`) and nothing automatic (exploring, a rewrite, a merge) changes it.
A test without a level is `important`: never silently minor.
"""

import re

CRITICAL, IMPORTANT, MINOR = "critical", "important", "minor"
LEVELS = (CRITICAL, IMPORTANT, MINOR)  # most important first: the order tests are listed in
DEFAULT = IMPORTANT
PERSON = "person"
LABELS = {CRITICAL: "Critical", IMPORTANT: "Important", MINOR: "Minor"}
WHY_MAX = 200

# the rule the test writer follows (propose.INSTRUCTIONS); kept here so the levels and their meaning live together
RULE = """- Give every test an "importance", from what you saw on its pages, and say why in "importance_why" (one short
  line): "critical" = money or access: signing up, logging in, a sign-in link, resetting a password, upgrading,
  paying, checking out, and the product's core action (the one thing people come to the app to do);
  "important" = daily use: settings, invites, alerts, each user type's main pages; "minor" = content: blog, legal,
  docs, marketing pages. When unsure between two levels, pick the higher one."""


# the writer echoes the rule's shape in importance_why ("critical = money or access: pricing drives upgrades",
# "Daily use: the home page ..."): the level and the category are read from it, never shown as written
_ECHO = re.compile(r"^\s*(?:why\s+)?(?:critical|important|minor)\s*(?:=|:|-|—)\s*", re.I)
_CATEGORY = re.compile(r"^\s*(content|money(?:\s+or\s+access)?|access|daily\s+use)\s*(?::|=|-|—)\s*", re.I)


def reason(value, why="", expect=()):
    """A test's level and why, as one plain sentence: "Critical (money or access): pricing drives upgrades and
    revenue." The writer's "<level> = <category>: <text>" read apart: the level from the test, the category (one of
    the rule's: money or access, daily use, content) in brackets, then the writer's own words. When no words are
    left, what the test checks says it ("Minor: it only checks the page's text."), from its expectations. -> str"""
    text, category = " ".join(str(why or "").split()), ""
    while True:  # "Why minor: minor = content: ..." echoes it twice
        cleaned = _ECHO.sub("", text)
        found = _CATEGORY.match(cleaned)
        if found:
            category = " ".join(found.group(1).lower().split())
            cleaned = cleaned[found.end():]
        cleaned = cleaned.strip()
        if cleaned == text:
            break
        text = cleaned
    text = text.rstrip(".").strip()
    kinds = {next(iter(e)) for e in expect or () if isinstance(e, dict) and e}
    if not text and kinds and kinds <= {"text_visible", "text_absent"}:
        text = "it only checks the page's text"
    head = LABELS[level(value)] + (f" ({category})" if category else "")
    if not text:
        return f"{head}."
    if len(text) > 1 and not text[1].isupper():  # "Legal pages ..." -> "legal pages ...", "API keys" stays
        text = text[0].lower() + text[1:]
    return f"{head}: {text}."


def level(value):
    """A stored or suggested level -> one of LEVELS; anything else (missing, misspelled) is `important`."""
    text = str(value or "").strip().casefold()
    return text if text in LEVELS else DEFAULT


def of(flow):
    """The level of a flow (a test file or a draft's flow)."""
    return level((flow or {}).get("importance"))


def rank(value):
    """0 for critical, 1 for important, 2 for minor: sort by it to list the most important first."""
    return LEVELS.index(level(value))


def by_person(flow):
    return (flow or {}).get("importance_set_by") == PERSON


def suggested(proposal):
    """{"importance", "importance_why"} from what the test writer returned for one test."""
    why = " ".join(str((proposal or {}).get("importance_why") or "").split())[:WHY_MAX]
    out = {"importance": level((proposal or {}).get("importance"))}
    if why:
        out["importance_why"] = why
    return out


def keep_persons(new, old):
    """A rewrite or merge of `old` into `new` (both test dicts): a level a person set on `old` stays, whatever the
    writer suggested for `new`. -> `new`."""
    if by_person(old) and not by_person(new):
        for key in ("importance", "importance_why", "importance_set_by"):
            if key in old:
                new[key] = old[key]
            else:
                new.pop(key, None)
    return new


def set_by_person(flow, value):
    """A person chose the level: saved as theirs, so nothing automatic changes it again."""
    if value not in LEVELS:
        raise ValueError(f"unknown level {value!r} (one of {', '.join(LEVELS)})")
    flow["importance"] = value
    flow["importance_set_by"] = PERSON
    return flow


def summary(results):
    """{level: {"total", "passed", "failed", "not_checked", "not_run", "changed", "flaky"}} over a run's results
    (each result carries its test's `importance`; see suite.stamp_importance). Every level is always present."""
    from . import verdict

    out = {name: {"total": 0, "passed": 0, verdict.FAILED: 0, verdict.NOT_CHECKED: 0, verdict.NOT_RUN: 0,
                  verdict.CHANGED: 0, verdict.FLAKY: 0} for name in LEVELS}
    for result in results:
        counts = out[level(result.get("importance"))]
        counts["total"] += 1
        counts[verdict.of(result)] += 1
    return out
