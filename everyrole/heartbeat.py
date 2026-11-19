"""A test's sign of life, so a test frozen on one call is stopped in minutes, not at the end of its whole budget.

A test's budget is its time per step times its steps, plus every wait it may do (spec.Flow.budget_s): 28 minutes for
a 7-step journey, twice with its retry. A test stuck on a single browser call (a page that never answers) used all of
it while the change's page showed nothing moving (staging, 2026-11-17: sign-up journeys "running" for 40+ minutes).
Now the test touches its heartbeat file at every action and every wait (runner.py); the run stops a test whose file
went quiet for longer than one step may take (suite.py), and reports it as hung.
"""

import os
import time
from pathlib import Path

ENV = "EVERYROLE_HEARTBEAT"  # the file a test's worker touches (set by the run for each worker)


def beat(env=None):
    """This test is still moving. Never fails the test (a file that can't be touched only means no watching)."""
    path = (os.environ if env is None else env).get(ENV)
    if not path:
        return
    try:
        Path(path).touch()
    except OSError:
        pass


def beating(sleep):
    """`sleep`, beating before and after: a test waiting on purpose (slow work, an email) is alive."""
    def waiting(seconds):
        beat()
        sleep(seconds)
        beat()

    return waiting


def quiet_for(path, clock=time.time):
    """Seconds since the test last moved (since the file was made, if it never did); 0 when there is no file."""
    try:
        return max(0.0, clock() - Path(path).stat().st_mtime)
    except OSError:
        return 0.0
