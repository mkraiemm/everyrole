"""Changing a test account's password, for real, without ever locking it out.

A kind of user whose app lets them change their password gets one built-in suggestion: "A free user can change
their password and sign in with the new one". It changes data (the account's password), so it only runs once a
person approves it, and only where EveryRole itself keeps that password (EveryRole's own browsers): in a CI run
the password lives in the CI's secrets, which EveryRole can't update, so there it is not run.

How a run keeps the account usable:
1. Before the run, the host (where the tests are kept) makes a new password and saves it as
   pending next to the current one, then hands it to the run as EVERYROLE_NEW_PASSWORD_<USER TYPE>.
2. The test types the current password and the new one, and saves (suite.py runs it after the user type's other
   tests, one at a time).
3. The run signs in with the new password, then, if that fails, with the old one, and records which one works
   (`signin_after`: new | old | neither). The test passes only if signing in with the new password works.
4. After the run, the host (Store.settle_password_changes) keeps the password that works; if neither does, it keeps
   both and tells the owner, who can see them in the settings. A run that stopped half-way leaves both known too.
"""

import re

from .site import split as split_page

NEW_PASSWORD_ENV = "EVERYROLE_NEW_PASSWORD_{}"
# set by the host when the app runs in more than one environment (production, staging...) that share the test
# accounts: the test then changes the password back once the new one is proven, so it stays the same everywhere
CHANGE_BACK_ENV = "EVERYROLE_PASSWORD_CHANGE_BACK"
# set by the host for a kind of user whose last password change is not confirmed yet: its test waits (not run), and
# the run says so instead of the CI reason
WAITING_ENV = "EVERYROLE_PASSWORD_WAITING_{}"
WAITING = ("not run: waiting: the last password change isn't confirmed yet — see Settings → Users & sign-in")
ELSEWHERE = ("not run: it changes the test account's password, which only runs where EveryRole keeps that password "
             "(EveryRole's own browsers), never from a CI run")
SECRET = re.compile(r"\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NEW, OLD, NEITHER = "new", "old", "neither"
# kept next to a user type's password secret (e.g. SW_FREE_PASSWORD): the new one while a change is not confirmed,
# the one it replaced, and when it changed. A person typing a password in the settings replaces all three.
KEEPER = ("_NEW", "_PREVIOUS", "_CHANGED_AT")


def make_password():
    """A fresh password most apps accept: long, with upper and lower case letters, digits and a symbol."""
    import secrets as random

    return "Er" + random.token_urlsafe(15).replace("-", "x").replace("_", "y") + "7!"


def env_name(persona):
    """The environment variable a run reads a user type's new password from."""
    return NEW_PASSWORD_ENV.format(_env_key(persona))


def waiting_env_name(persona):
    """The environment variable that says a user type's last password change is not confirmed yet."""
    return WAITING_ENV.format(_env_key(persona))


def _env_key(persona):
    return re.sub(r"[^A-Z0-9]+", "_", persona.upper()).strip("_")


def not_run_reason(persona, env):
    """Why a password-change test gets no new password in this run: its last change isn't confirmed yet (the host
    said so), or it runs where EveryRole doesn't keep the password (a CI run)."""
    return WAITING if env.get(waiting_env_name(persona)) else ELSEWHERE


def password_secret(persona_values):
    """The secret holding a user type's current password, from its sign-in values ({"password":
    "{{secrets.SW_FREE_PASSWORD}}"} -> "SW_FREE_PASSWORD"), or "" when it signs in without one."""
    for key in sorted(persona_values, key=lambda k: (k != "password", k)):
        found = SECRET.search(str(persona_values[key]))
        if found and ("password" in key or found.group(1).endswith("PASSWORD")):
            return found.group(1)
    return ""


def suggestions(snapshots, config, *, site, kept_flows=(), removed=()):
    """Built-in drafts: for each kind of user that signs in with a password and whose pages have a form that sets a
    new password (exploring saw it, see discover.py `changes_password`), one password-change test, unless one is
    kept or was removed already. -> draft items, waiting for a person's approval."""
    taken = {f.get("changes_password_as") or _persona_of(f) for f in [*kept_flows, *removed]
             if f.get("changes_password")}
    items = []
    for persona, snap in snapshots.items():
        who = config.personas.get(persona)
        if who is None or persona in taken or not password_secret(who.values):
            continue
        found = next(((route, action) for route, page in snap.pages.items() for action in page.actions
                      if action.get("changes_password")), None)
        if found is None:
            continue
        route, action = found
        items.append(_item(persona, route, action, site))
    return items


def _persona_of(flow):
    who = flow.get("as")
    return who if isinstance(who, str) else ""


def _item(persona, route, action, site):
    label = str(action.get("label") or "Save")
    flow = {
        "name": f"{persona}-changes-password",
        "title": f"A {persona} user can change their password and sign in with the new one",
        "as": persona,
        "start": site.template(route),
        "goal": (f"Change the password: type the current password, type the new password in every new-password "
                 f"field, then press “{label}”"),
        "values": {"new_password": "{{secrets." + env_name(persona) + "}}"},
        # the proof is signing in with the new password afterwards (suite.py); this only says the page stayed up
        "expect": [{"url_contains": split_page(route)[1]}],
        "changes_password": True,
        "run_on": ["preview"],
        # EveryRole writes this test itself (no model): it is about access, so it says its level like the writer would
        "importance": "critical",
        "importance_why": "Access: the account's password changes, and signing in with the new one must work",
    }
    why = (f"It really changes {persona}’s password. EveryRole saves the new one before it is typed, signs in with "
           "it to prove the change worked, and keeps whichever password works, so the test account never gets "
           "locked out. Runs only once you approve it.")
    return {"proposal": {"name": flow["name"], "as": [persona], "start_path": route, "target_path": route,
                         "mode": "run", "why": why},
            "flow": flow, "status": "needs_preview", "reason": "changes the test account’s password: approve it first",
            "writes_data": True, "mode": "run", "why": why, "builtin": "password_change"}


# ---------------------------------------------------------------------------------------------- the host's side
# A host keeps test-account secrets as {name: value}; these work on that mapping, and the host saves it.

def secret_name(persona):
    """The secret holding a kind of user's password, as the hosts name it."""
    return f"SW_{persona.upper().replace('-', '_')}_PASSWORD"


def changing_types(flows_dir):
    """Kinds of user with a kept password-change test."""
    import json
    from pathlib import Path

    personas = set()
    for path in Path(flows_dir).glob("*.json") if flows_dir and Path(flows_dir).is_dir() else ():
        try:
            flow = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if flow.get("changes_password") and isinstance(flow.get("as"), str):
            personas.add(flow["as"])
    return personas


def prepare(secrets, flows_dir, change_back=False):
    """Before a run: a new password for each kind of user whose password-change test may run, put in `secrets` as
    pending (the host saves them BEFORE the run starts). A kind of user whose earlier change is not confirmed is
    left alone, and its test waits (WAITING_ENV). `change_back`: the app has other environments sharing the accounts
    (see CHANGE_BACK_ENV).
    -> the run's environment {EVERYROLE_NEW_PASSWORD_<USER TYPE>: new password, ...}."""
    env = {}
    for persona in sorted(changing_types(flows_dir)):
        key = secret_name(persona)
        if secrets.get(key + KEEPER[0]):
            env[waiting_env_name(persona)] = "1"
            continue
        if not secrets.get(key):
            continue
        new = make_password()
        secrets[key + KEEPER[0]] = new
        env[env_name(persona)] = new
    if change_back and any(name.startswith(NEW_PASSWORD_ENV.format("")) for name in env):
        env[CHANGE_BACK_ENV] = "1"
    return env


def settle(secrets, flows_dir, results):
    """After a run (every attempt): keep in `secrets` the password that signs in now (the host saves them).
    -> [(kind of user, "changed" | "changed_back" | "unchanged" | "unsure")]."""
    import time

    answers = {r.get("persona"): r.get("signin_after") for r in results or [] if "signin_after" in r}
    changed_back = {r.get("persona") for r in results or [] if r.get("changed_back")}
    events = []
    for persona in sorted(changing_types(flows_dir)):
        key = secret_name(persona)
        new = secrets.get(key + KEEPER[0])
        if not new:
            continue
        after = answers.get(persona)
        if after == OLD and persona in changed_back:
            secrets.pop(key + KEEPER[0])  # changed, proven, and changed back: the same password everywhere
            events.append((persona, "changed_back"))
        elif after == NEW:
            secrets[key + KEEPER[1]], secrets[key] = secrets.get(key, ""), new
            secrets[key + KEEPER[2]] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            secrets.pop(key + KEEPER[0])
            events.append((persona, "changed"))
        elif after == OLD:
            secrets.pop(key + KEEPER[0])
            events.append((persona, "unchanged"))
        else:
            events.append((persona, "unsure"))
    return events
