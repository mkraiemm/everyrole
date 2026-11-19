"""What a result means for the app under test.

Only an app problem is ever shown as a failure. A machine problem (our Chrome stopped answering, the model
provider timed out, a worker hung) says nothing about the app: it is shown as "not checked" and never turns
a pull request red on its own.

Every non-passing result carries a `cause`, set where the problem happened:
  app      the app did not do what the test expects (the only real failure)
  machine  EveryRole could not check (browser / model / worker trouble)
  setup    the test itself is invalid (bad flow file or config)
  skipped  not run because something it depends on failed (e.g. its user type could not sign in)
  held     not run on purpose, for its own reason (e.g. a password-change test waits until the last change is
           confirmed). Shown as "not run" with that reason, never as "couldn't check"
  signin   a person must sign in for EveryRole: the sign-in has a security check (CAPTCHA) or the saved
           sign-in expired. Shown as "couldn't check", with a "sign in yourself" action
  unavailable  the app's server didn't answer the page (it answered 5xx, or 429: too many requests): nothing
           about the test can be said. Shown as "couldn't check", with what the server answered; checked again

A test that failed and then passed again over its recent runs is flaky (`flaky` on its result, see flaky.py): set
aside, whether it passed or failed this time. It is never counted as a failure and never alarms anyone.
"""

APP, MACHINE, SETUP, SKIPPED, SIGNIN, HELD = "app", "machine", "setup", "skipped", "signin", "held"
UNAVAILABLE = "unavailable"
# the app works but says it differently now: every check that failed found a very similar text on the page.
# Shown as "wording changed" (amber, never a failure), with the new wording and a one-click update of the test
WORDING = "wording"

PASSED, FAILED, NOT_CHECKED, NOT_RUN, CHANGED = "passed", "failed", "not_checked", "not_run", "changed"
FLAKY = "flaky"  # set aside: it keeps failing and passing on the same app


# "its checks held before the step acted" (runner.py), in its old wording and its current one: the test's own mistake,
# which a newer runner can judge (it leaves out checks that held on the page before)
HELD_BEFORE_WORDS = ("checks describe the page it started on", "already showed before the step did anything")


def held_before(error):
    """True when a run failed only because its checks held before its step acted."""
    return any(w in (error or "") for w in HELD_BEFORE_WORDS)


def cause_of(result):
    """The cause of a non-passing flow result or access row ("" when it passed)."""
    if result.get("passed"):
        return ""
    if result.get("cause"):
        return result["cause"]
    # Results written before causes existed: an exception inside the run is the only machine signal.
    if result.get("observed") == "error" or result.get("status") == "error":
        return MACHINE
    return APP


def of(result):
    """passed | failed | not_checked | not_run | changed | flaky"""
    cause = cause_of(result)
    if result.get("flaky") and cause in ("", APP):
        return FLAKY
    if not cause:
        return PASSED
    outcomes = {MACHINE: NOT_CHECKED, SIGNIN: NOT_CHECKED, UNAVAILABLE: NOT_CHECKED, SKIPPED: NOT_RUN,
                HELD: NOT_RUN, WORDING: CHANGED}
    return outcomes.get(cause, FAILED)


def reworded(expectations):
    """The checks of a finished test that failed, when every one of them found its text reworded on the page:
    [(expected, now on the page)]; [] otherwise (something really is missing or wrong)."""
    failed = [e for e in expectations or [] if not e.get("passed")]
    if failed and all(e.get("closest") for e in failed):
        return [(e["expected"], e["closest"]) for e in failed]
    return []


def counts(results):
    tally = {PASSED: 0, FAILED: 0, NOT_CHECKED: 0, NOT_RUN: 0, CHANGED: 0, FLAKY: 0}
    for result in results:
        tally[of(result)] += 1
    return tally
