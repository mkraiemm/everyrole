"""Sign-ins a person did by hand (e.g. to pass a security check), reused by every run until they expire.

A saved sign-in is a session file (cookies + local storage) plus the page the person landed on after signing in.
Before a run uses it, that page is opened with the session, without any AI: if the app sends the browser to the
sign-in page (or any `denied_when` check matches), the sign-in has expired and the person is asked to sign in
again. That is never reported as an app failure.
"""

import time
from pathlib import Path

from . import verdict
from .session import load

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
    if checker is None:
        from .access import AccessChecker

        checker = AccessChecker()
    page = config.personas[persona].saved_signin.get("check") or "/"
    (row,) = checker.run(base_url=base_url, site=site, persona=persona, rules=[(page, "allow")],
                         denied_when=config.denied_when, settle_s=config.access_settle_s, session_state=state)
    if row.passed:
        return _result(name, persona, True, "passed", "", "", started, clock), state
    if row.cause == verdict.MACHINE:  # the page could not be opened or never finished loading: not an expiry
        return _result(name, persona, False, "error", row.detail, verdict.MACHINE, started, clock), None
    label = persona.replace("-", " ").replace("_", " ")
    return _result(name, persona, False, "failed_expect", EXPIRED.format(persona=label), verdict.SIGNIN, started,
                   clock), None


def _result(name, persona, passed, status, error, cause, started, clock):
    return {"name": name, "persona": persona, "status": status, "passed": passed, "error": error, "cause": cause,
            "duration_ms": round((clock() - started) * 1000), "expectations": [], "steps": [], "warnings": [],
            "saved_signin": True}
