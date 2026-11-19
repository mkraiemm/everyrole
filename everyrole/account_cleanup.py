"""Deleting the accounts EveryRole created on an app (account_book.py), the app's own way, then making sure.

For each account that may exist: one journey logs in with it and deletes it through the app's own settings (never
anything else); then a fresh browser tries to log in with it. Refused: it is gone. Still logs in: it is left on the
app, and the team is told why (the app offers no way to delete an account, or deleting didn't work). The check, not
the agent's word, decides; a check that couldn't run (our machine) leaves it for the next cleanup."""

from . import account_book, verdict

DELETE = "delete-test-account"
CHECK = "check-test-account-gone"
OPEN_LOGIN = "Open the app's log-in page (never create an account)"
# an app may still accept the log-in of a deleted account and say so on the next page ("This account is
# deleted"): that is gone too, not "still logs in"
LOG_IN = ("Log in with new_account_email and new_account_password: this account already exists. Never create an "
          "account; if the app refuses them, or the page after says the account is deleted or closed, stop there")
DELETE_GOAL = ("Delete this account for good, the way the app offers it (its account, profile or settings page: "
               "\"Delete account\", \"Close account\" or the like), confirming when the app asks. Change or delete "
               "nothing else. If the app offers no way to delete an account, stop and say so")
# what people read on the change page for each of these goals: the goals above are written for the agent
SAID = {OPEN_LOGIN: "Open the app's log-in page", LOG_IN: "Log in with the test account",
        DELETE_GOAL: "Delete the account the app's own way"}
SIGNED_IN = [{"password_field_absent": True}]  # no password asked any more: the log-in went through


def journeys(config, email):
    """The two journeys for the book's account `email`, as flow files: from the app's own sign-in page when known,
    else its home page. Each logs in with that account (a book may hold several for one role: each is
    cleaned with its own log-in)."""
    start = (config.login or {}).get("start") or "{{BASE_URL}}/"
    log_in = [{"goal": OPEN_LOGIN, "checked_by_next": True, "expect": []},
              {"goal": LOG_IN, "sign_up": True, "expect": SIGNED_IN}]
    delete = {"name": DELETE, "start": start, "expect": SIGNED_IN, "account": email,
              "steps": [*log_in, {"goal": DELETE_GOAL, "checked_by_next": True, "expect": []}]}
    check = {"name": CHECK, "start": start, "steps": log_in, "account": email}
    return delete, check


def outcome(deleted, check, signed_up=False):
    """The account's state after the cleanup, from the two journeys' results (None: not run). `signed_up`: a sign-up
    step passed with it, so the app did make it."""
    if check is None or verdict.cause_of(check) in (verdict.MACHINE, verdict.SETUP, verdict.SKIPPED):
        return account_book.UNSURE  # the check couldn't say
    if not check.get("passed"):
        # the app refused the log-in: no such account any more. Deleted by EveryRole only when its delete journey got
        # in first; refused there too, the sign-up never made it (automationexercise, 2026-11-18: "Deleted through
        # the app's own settings" for an account the app had never heard of)
        logged_in = ((({s.get("step"): s for s in (deleted or {}).get("steps_judged") or []}).get(2) or {})
                     .get("passed"))
        # ... unless a sign-up step passed with it: then it was made, and the app refuses it now (Conduit, 2026-11-18:
        # signed in on every round, "never finished" at the clean-up)
        return (account_book.DELETED if logged_in else account_book.REFUSED if signed_up
                else account_book.NOT_MADE)
    steps = {s.get("step"): s for s in (deleted or {}).get("steps_judged") or []}
    if (steps.get(3) or {}).get("status") == "blocked":  # the agent found no way to delete an account
        return account_book.NO_DELETE
    if deleted and verdict.cause_of(deleted) == verdict.MACHINE:
        return account_book.UNSURE  # it never got to try
    return account_book.STILL_THERE


def proof(deleted, check):
    """What the two journeys did, for the team to see why an account is said gone or left: [{journey, step, goal,
    passed, picture (the file's name), url}] (no password: the goals name the values, never their contents)."""
    out = []
    for label, result in (("Delete it", deleted), ("Check it is gone", check)):
        for s in (result or {}).get("steps_judged") or []:
            out.append({"journey": label, "step": s.get("step"), "goal": SAID.get(s.get("goal", ""), s.get("goal", "")),
                        "passed": bool(s.get("passed")), "status": "Done" if s.get("passed") else "Didn't go through",
                        "picture": str(s.get("picture") or "").replace("\\", "/").rsplit("/", 1)[-1],
                        "url": (s.get("page") or {}).get("url", "")})
    return out


def clean(book, config, run):
    """Delete every account of the book that may exist. `run(flow_dicts)` runs journeys in order (a fresh browser
    each) -> {name: result}. -> [(email, state)]"""
    done = []
    for row in book.to_clean():
        delete, check = journeys(config, row["email"])
        deleted = run([delete]).get(DELETE)
        checked = run([check]).get(CHECK)
        state = outcome(deleted, checked, signed_up=bool(row.get("signed_up")))
        book.settle(row["email"], state, proof(deleted, checked))
        done.append((row["email"], state))
    return done
