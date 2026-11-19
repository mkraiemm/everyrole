"""Sessions kept for user types that sign in by email: one sign-in, reused by every later job while it still works.

An app that signs in by an emailed link limits how many links an address may ask for (EveryRole itself: 3 per email
and 5 per visitor address every 15 minutes), and every job signing in again would soon be refused, besides waiting
for an email each time. So once a sign-in went through the emailed link, its session is kept in the app's folder
(like a sign-in a person did by hand) with the page it landed on. Before signing in again, that page is opened with
the session, without any AI: still signed in, the session is reused; sent to sign in (the app's own signals,
`denied_when`), the kept session is dropped and the user type signs in by email again.

A sign-in that must really happen (accepting an invitation, the "Check sign-in" button) sets FRESH_ENV and never
reuses one.
"""

import json
import os
import time
from pathlib import Path

FOLDER = "kept-signins"
FRESH_ENV = "EVERYROLE_FRESH_SIGNIN"


def _paths(config, persona):
    base = Path(config.source).parent if config.source else Path(".")
    folder = base / FOLDER
    return folder / f"{persona}.json", folder / f"{persona}.page.json"


def keep(config, persona, state_file, landed_page):
    """Keep the session a sign-in by email just saved (state_file), with the page it landed on."""
    state_path, page_path = _paths(config, persona)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    data = Path(state_file).read_text(encoding="utf-8")
    for path, text in ((state_path, data), (page_path, json.dumps({"check": landed_page or "/", "at": time.time()}))):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)  # parallel workers never read half a file


def forget(config, persona):
    for path in _paths(config, persona):
        path.unlink(missing_ok=True)


def reuse(config, persona, *, site=None, checker=None, env=None):
    """The kept session of this user type when it still signs in, else None (and a session that stopped working is
    dropped). -> (state, page it was checked on)."""
    env = os.environ if env is None else env
    state_path, page_path = _paths(config, persona)
    if env.get(FRESH_ENV) or not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        page = json.loads(page_path.read_text(encoding="utf-8")).get("check") or "/"
    except (OSError, ValueError):
        forget(config, persona)
        return None
    from . import verdict
    from .signin import session_holds

    row = session_holds(config, persona, state, page, site=site, checker=checker)
    if row.passed:
        return state, page
    if row.cause != verdict.MACHINE:  # the app sent it to sign in: expired (a page that didn't load says nothing)
        forget(config, persona)
    return None
