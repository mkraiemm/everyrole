"""The accounts EveryRole creates on an app it tests.

The rules (a day of test journeys once left hundreds of test accounts on an app, production included):
- a new account only when a test is about new accounts (a sign-up step); every other journey logs in with an
  account the team gave;
- at most one per role: per change (all its walks, journeys and tries) or per test run. The first journey that signs
  up creates it; every other one, and every retry, logs in with it;
- deleted at the end, the app's own way (account_cleanup.py); when the app has no way to delete an account, or it
  still logs in after, the team is told.

The book is a file: a change's folder (its walks share it) or a run's output folder. Its passwords never leave it:
reports hide them like any secret, and `view` leaves them out."""

import json
import os
import threading
import time
from pathlib import Path

ENV = "EVERYROLE_ACCOUNTS_FILE"  # where a run's workers find the book (they inherit the run's environment)
FILE = "accounts.json"
NEW_USER = "new user"  # the role of an account a journey signs up (a visitor who becomes a user)

RESERVED = "reserved"  # its identity is chosen; no journey reached the sign-up yet
CREATED = "created"  # a journey reached the sign-up with it: it may exist on the app, so it is cleaned up
DELETED = "deleted"  # deleted, and it no longer logs in
NO_DELETE = "no_delete"  # the app offers no way to delete an account: it is still there
STILL_THERE = "still_there"  # EveryRole deleted it the app's way, and it still logs in
UNSURE = "unsure"  # the check couldn't run (our machine): the next cleanup tries again
NOT_MADE = "not_made"  # the app never let it log in, delete journey included: the sign-up never made it
REFUSED = "refused"  # its sign-up worked, but the app later refused its log-in: EveryRole couldn't get in to delete it

NOTES = {
    DELETED: "Deleted through the app's own settings; it no longer logs in (or the app says it is deleted).",
    NO_DELETE: "The app offers no way to delete an account, so it is still there: remove it on your side.",
    STILL_THERE: "EveryRole deleted it the app's way, but it still logs in: remove it on your side.",
    UNSURE: "EveryRole couldn't check that it was deleted this time (a problem on our side, not in your app); it "
            "tries again at the next clean-up.",
    RESERVED: "Never used: no account was made.",
    NOT_MADE: "The sign-up never finished: the app doesn't know this account, so there was nothing to delete.",
    REFUSED: "The sign-up worked, but later the app refused to log in to this account, so EveryRole couldn't delete "
             "it. If your app still has it, remove it on your side.",
    CREATED: "In use by this change; deleted when it ends.",
}
WARN = (NO_DELETE, STILL_THERE, REFUSED)

_lock = threading.Lock()  # a run's journeys mark it from parallel threads


class AccountBook:
    def __init__(self, path):
        self.path = Path(path)

    def rows(self):
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def identity(self, role, mint):
        """The role's account in this book; the first time, or once it was deleted, a new identity from `mint()`
        ({email, password, name, inbox}, or None when there is no inbox), written down before any journey types it.
        A deleted account is never signed up again: an app may keep its email (it answers "This account is
        deleted"), so a sign-up with it would test nothing. -> the row, or None."""
        with _lock:
            rows = self.rows()
            known = next((r for r in reversed(rows) if r.get("role") == role and r.get("state") != DELETED), None)
            if known is not None:
                return known
            account = mint()
            if not account:
                return None
            row = {**account, "role": role, "state": RESERVED, "at": _now()}
            self._save([*rows, row])
            return row

    def row(self, email):
        """The book's row of this account, or None."""
        return next((r for r in self.rows() if r.get("email") == email), None)

    def reached(self, email):
        """A journey got to the sign-up with this account: it may exist on the app from now on (again, for one left
        on the app and used again: its cleanup tries again at the end)."""
        self._set(email, lambda r: r.get("state") in (RESERVED, UNSURE, *WARN), state=CREATED)

    def made(self, email):
        """A journey's sign-up step passed with this account: the app made it (so a refused log-in later doesn't mean
        it never existed)."""
        self._set(email, lambda r: True, signed_up=True)

    def settle(self, email, state, proof=None):
        """`proof`: what the cleanup's journeys did, step by step with their pictures (account_cleanup.proof)."""
        self._set(email, lambda r: True, state=state, checked=_now(), **({"proof": proof} if proof else {}))

    def to_clean(self):
        """The accounts that may exist on the app and aren't known to be gone."""
        return [r for r in self.rows() if r.get("state") in (CREATED, UNSURE)]

    def view(self):
        """What people and agents see: never a password."""
        return [{"role": r.get("role", ""), "email": r.get("email", ""), "state": r.get("state", ""),
                 "note": NOTES.get(r.get("state"), ""), **({"proof": r["proof"]} if r.get("proof") else {})}
                for r in self.rows() if r.get("state") != RESERVED]

    def warning(self):
        """One sentence when an account is left on the app, else ""."""
        left = [r["email"] for r in self.rows() if r.get("state") in WARN]
        if not left:
            return ""
        return (f"{len(left)} test account{'s' if len(left) > 1 else ''} EveryRole created could not be deleted "
                f"({', '.join(left)}): remove {'them' if len(left) > 1 else 'it'} on your side.")

    def _set(self, email, when, **fields):
        with _lock:
            rows = self.rows()
            changed = False
            for row in rows:
                if row.get("email") == email and when(row):
                    row.update(fields)
                    changed = True
            if changed:
                self._save(rows)

    def _save(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")


def from_env(env=None):
    """The book this run uses, or None (a run without one mints a new identity per journey, as before)."""
    path = (os.environ if env is None else env).get(ENV)
    return AccountBook(path) if path else None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
