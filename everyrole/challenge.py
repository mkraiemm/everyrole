"""Is this page guarded by a security check (a CAPTCHA) that only a person can pass?

Providers are data (challenges.json), recognised by where their frames and scripts load from. When one is found
on a page where a test gave up, the result is "sign in yourself", never "your app is broken": EveryRole does not and
will not try to solve security checks.
"""

import json
from functools import lru_cache
from importlib import resources

SOURCES = r"""(() => [...document.querySelectorAll('iframe[src],script[src]')].map(e => e.src))()"""


@lru_cache(maxsize=1)
def providers():
    return json.loads(resources.files("everyrole").joinpath("challenges.json").read_text(encoding="utf-8"))["providers"]


def match(sources):
    """-> the name of the first provider whose address appears among the page's frame/script sources, or ""."""
    for provider in providers():
        if any(needle in src for src in sources for needle in provider["sources"]):
            return provider["name"]
    return ""


def detect(browser):
    """-> provider name or "" (never raises: a page that can't be read has no known check)."""
    try:
        return match(browser.evaluate(SOURCES) or [])
    except Exception:
        return ""


def message(provider):
    return (f"The page has a security check ({provider}) that only a person can pass. "
            "Sign in yourself once and save that session as the user type's `saved_signin`: EveryRole reuses it.")
