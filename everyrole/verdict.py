"""What a result means for the app under test.

Only an app problem is ever shown as a failure. A machine problem (our Chrome stopped answering, the model
provider timed out, a worker hung) says nothing about the app: it is shown as "not checked" and never turns
a pull request red on its own.

Every non-passing result carries a `cause`, set where the problem happened:
  app      the app did not do what the test expects (the only real failure)
  machine  EveryRole could not check (browser / model / worker trouble)
  setup    the test itself is invalid (bad flow file or config)
  skipped  not run because something it depends on failed (e.g. its user type could not sign in)
  signin   a person must sign in for EveryRole: the sign-in has a security check (CAPTCHA) or the saved
           sign-in expired. Shown as "couldn't check", with a "sign in yourself" action
"""

APP, MACHINE, SETUP, SKIPPED, SIGNIN = "app", "machine", "setup", "skipped", "signin"

PASSED, FAILED, NOT_CHECKED, NOT_RUN = "passed", "failed", "not_checked", "not_run"


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
    """passed | failed | not_checked | not_run"""
    cause = cause_of(result)
    if not cause:
        return PASSED
    return {MACHINE: NOT_CHECKED, SIGNIN: NOT_CHECKED, SKIPPED: NOT_RUN}.get(cause, FAILED)


def counts(results):
    tally = {PASSED: 0, FAILED: 0, NOT_CHECKED: 0, NOT_RUN: 0}
    for result in results:
        tally[of(result)] += 1
    return tally
