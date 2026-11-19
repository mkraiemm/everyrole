"""Tests that sign up a new user, and tests that delete: when they run. Both are off by default; the team chooses
when they run (only after each release, one time, or once a week), and a test left out says so.

Every other test signs in with the app's existing test accounts and never destroys anything. A test with a sign-up
step makes a real account in the app each time it runs (and an app may limit sign-ups: "Rate limit
exceeded, retry in 1 hour"); a test that presses Delete or Remove account (it is about
deleting) destroys what it uses. So each environment says how often each kind runs:

  off        never (the default)
  once       the next run only, then never again (until chosen again)
  release    the runs that check a new version: a deploy hook, a new build the deploy watch saw
  weekly     the first run of each 7 days
  every_run  every run

The choice is the team's, kept per environment; a run leaves those tests out (`--skip-sign-up`, `--skip-delete`)
and says which tests are off (off_notices).
"""

from datetime import timedelta

OFF, ONCE, RELEASE, WEEKLY, EVERY_RUN = "off", "once", "release", "weekly", "every_run"
SCHEDULES = {OFF: "Off", ONCE: "Once (the next run)", RELEASE: "After each release", WEEKLY: "Once a week",
             EVERY_RUN: "Every run"}
DEFAULT = OFF
WEEK = timedelta(days=7)


def _steps(flow):
    return (flow.get("steps") if isinstance(flow, dict) else getattr(flow, "steps", None)) or ()


def signs_up(flow):
    """True when the test has a step that creates a new account (a saved test or a parsed one)."""
    return any(step.get("sign_up") for step in _steps(flow))


def deletes(flow):
    """True when the test is allowed to press Delete or Remove account (change_plan.gatekeep marks it)."""
    return bool(flow.get("deletes") if isinstance(flow, dict) else getattr(flow, "deletes", False))


# kind -> (the setting's name, its label, which tests, the run's option that leaves them out)
KINDS = {
    "sign_up": ("new_user_tests", "Tests that create a new user", signs_up, "--skip-sign-up"),
    "delete": ("delete_tests", "Tests that delete", deletes, "--skip-delete"),
}


def schedule_of(value, label="New-user tests"):
    """A saved or sent choice -> one of SCHEDULES (anything else: ValueError)."""
    value = (value or DEFAULT).strip().lower()
    if value not in SCHEDULES:
        raise ValueError(f"{label} run: {', '.join(SCHEDULES.values()).lower()}.")
    return value


def due(schedule, *, release, last_at, now):
    """Do this run's tests of that kind run? `release`: the run checks a new version; `last_at`: when they last ran
    on this environment (None: never)."""
    if schedule == EVERY_RUN:
        return True
    if schedule == ONCE:
        return last_at is None
    if schedule == RELEASE:
        return bool(release)
    if schedule == WEEKLY:
        return last_at is None or now - last_at >= WEEK
    return False


def off_notices(flows, saved):
    """What the Tests page says about tests that don't run: [(label, how many)] for each kind whose schedule on this
    environment (`saved`: its payment-testing view) is off while the app has such tests."""
    out = []
    for setting, label, of_kind, _ in KINDS.values():
        count = sum(1 for flow in flows if of_kind(flow))
        if count and (saved or {}).get(setting, DEFAULT) == OFF:
            out.append((label, count))
    return out
