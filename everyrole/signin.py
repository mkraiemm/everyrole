"""Sign-ins a person did by hand (e.g. to pass a security check), reused by every run until they expire.

A saved sign-in is a session file (cookies + local storage) plus the page the person landed on after signing in.
Before a run uses it, that page is opened with the session, without any AI: if the app sends the browser to the
sign-in page (or any `denied_when` check matches), the sign-in has expired and the person is asked to sign in
again. That is never reported as an app failure.

A session can also expire DURING a run. `SignedOut` tells, by the app's own signals (its sign-in page, its
`denied_when` checks), that a page a signed-in test ended on says "not signed in": the run then signs that user
type in again once and runs the test again once (suite.py).
"""

import time
from pathlib import Path

from . import assertions, verdict
from .session import load
from .spec import signin_starts, site_of

EXPIRED = "Your saved sign-in for {persona} has expired. Sign in again and save the new session (`saved_signin`)."


def file_of(config, persona):
    """The saved sign-in file for this persona, resolved next to the config, or None."""
    saved = config.personas[persona].saved_signin if persona in config.personas else None
    if not saved:
        return None
    base = Path(config.source).parent if config.source else Path(".")
    path = Path(saved["file"])
    return path if path.is_absolute() else base / path


def load_state(config, persona):
    path = file_of(config, persona)
    return load(path) if path and path.is_file() else None


def check(config, persona, *, base_url=None, site=None, checker=None, clock=time.monotonic):
    """Open the landing page with the saved session. -> (result dict like a FlowResult, state or None)."""
    started = clock()
    state = load_state(config, persona)
    name = f"login@{persona}"
    if state is None:
        return _result(name, persona, False, "error", "No saved sign-in for this user type yet. Sign in yourself "
                       "and save the session to its `saved_signin` file.", verdict.SIGNIN, started, clock), None
    page = config.personas[persona].saved_signin.get("check") or "/"
    row = session_holds(config, persona, state, page, base_url=base_url, site=site, checker=checker)
    if row.passed:
        return _result(name, persona, True, "passed", "", "", started, clock), state
    if row.cause == verdict.MACHINE:  # the page could not be opened or never finished loading: not an expiry
        return _result(name, persona, False, "error", row.detail, verdict.MACHINE, started, clock), None
    label = persona.replace("-", " ").replace("_", " ")
    return _result(name, persona, False, "failed_expect", EXPIRED.format(persona=label), verdict.SIGNIN, started,
                   clock), None


def session_holds(config, persona, state, page, *, base_url=None, site=None, checker=None):
    """Open `page` with a saved session, without any AI: the access row says if it is still signed in (passed), sent
    to sign in (the app's `denied_when`), or couldn't be opened (cause machine)."""
    if checker is None:
        from .access import AccessChecker

        checker = AccessChecker()
    (row,) = checker.run(base_url=base_url, site=site, persona=persona, rules=[(page, "allow")],
                         denied_when=config.denied_when, settle_s=config.access_settle_s, session_state=state)
    return row


def home_of(config, persona, state, *, site, checker=None):
    """Where the app sends this user type once signed in, the page their tests start from: its sign-in page opened
    with the session, without any AI. An app sends a signed-in person on to their home (EveryRole: /login -> /apps),
    wherever the sign-in itself ended (an invitation's page, a page its checks led to). None when it leaves them on a
    sign-in page or the page couldn't be opened: nothing was learned."""
    starts, signed_out = signin_starts(config, persona, site=site), SignedOut.of(config, persona, site=site)
    if not starts or signed_out is None or state is None:
        return None
    row = session_holds(config, persona, state, site.page(starts[-1]) or starts[-1], site=site, checker=checker)
    landed = site.page(row.url) if row.url else None
    if row.cause == verdict.MACHINE or not landed or signed_out.page(row.url):
        return None
    return landed


def why_not_signed_in(result, where=""):
    """A sign-in that didn't go through ("Check sign-in"), said plainly from what it saw: never just "couldn't sign
    in". `where`: the page it stopped on, as people read it."""
    stopped = f" It stopped on {where}." if where else ""
    asked_again = any(e.get("kind") == "password_field_absent" and not e.get("passed")
                      for e in result.get("expectations") or [])
    said = [w for w in result.get("signin_said") or [] if isinstance(w, str) and w.strip()]
    if asked_again and said:  # the app's own answer (or the browser's check of a field) says why, in its words
        answer = ", ".join(f"“{w.strip()}”" for w in said)
        return f"After the sign-in form was sent, the page still asked for a password and said {answer}." + stopped
    if asked_again:
        return ("After the sign-in form was sent, the page still asked for a password: the app didn't take this email "
                "or username and password, or it asks for one more step." + stopped)
    if result.get("status") in ("blocked", "budget_exceeded"):
        return "It couldn't get through the sign-in form: a step it needed didn't work." + stopped
    if result.get("status") in ("left_allowed_hosts",):
        return "Signing in took it off your app’s addresses (another site signs people in)." + stopped
    return "It didn't get signed in." + stopped


def _result(name, persona, passed, status, error, cause, started, clock):
    return {"name": name, "persona": persona, "status": status, "passed": passed, "error": error, "cause": cause,
            "duration_ms": round((clock() - started) * 1000), "expectations": [], "steps": [], "warnings": [],
            "saved_signin": True}


class SignedOut:
    """Does a page say this user type is signed out? By the app's own signals only: the page is one of its sign-in
    pages (`login.start`, or the user type's own `signin.start`) and asks for a password, or one of its `denied_when`
    checks holds. The sign-in page's address alone isn't enough: a sign-in in a pop-up lives on a page signed-in
    people see too. `by_address`: this user type signs in by an emailed link, so no password is ever asked there."""

    def __init__(self, site, signin_urls, denied_when, by_address=False):
        self._site = site
        self._pages = {_page(site, url) for url in signin_urls} - {None}
        self._checks = assertions.build(denied_when)
        self._by_address = by_address

    @classmethod
    def of(cls, config, persona, *, base_url=None, site=None, env=None):
        """The check for this user type, or None when it doesn't sign in (nothing to lose mid-run)."""
        if persona not in config.personas or not config.personas[persona].login:
            return None
        site = site_of(base_url, site)
        return cls(site, signin_starts(config, persona, site=site, env=env), config.denied_when,
                   by_address=config.personas[persona].signin_by == "email")

    def page(self, url):
        """Why this address says signed out ("" when it doesn't): it is a sign-in page."""
        return f"it ended on the sign-in page ({url})" if url and _page(self._site, url) in self._pages else ""

    def __call__(self, facts):
        """Why this page says signed out ("" when it doesn't): a sign-in page, or one of the app's refusal signals."""
        hits = [o for o in assertions.evaluate(self._checks, facts) if o.passed]
        if hits and hits[0].kind == "signin_form_shown":
            return f"it ended on the sign-in page, asked for a password ({facts.url})"
        if hits:
            return f"the page showed the app's refusal signal ({hits[0].kind} “{hits[0].expected}”)"
        return self.page(facts.url) if facts.passwords or self._by_address else ""


def _page(site, url):
    """The page an address is in the app, without a trailing slash (None outside it, or with no site)."""
    page = site.page(url) if site else None
    return (page.rstrip("/") or "/") if page and not page.endswith(":/") else page
