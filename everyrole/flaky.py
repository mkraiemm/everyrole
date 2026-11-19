"""Flaky tests: a test that fails and then passes again on the same app is set aside, never a red alarm.

Every run keeps its report next to the others (runs/<stamp>/results.json). A test's outcomes over its recent runs
on the same target (the environment, and for previews the same address: two previews are different apps) are read
oldest first, the run being reported last. Only answers about the app count: a pass, an app failure, or a pass on
the second try after an app failure (it failed, then passed: both at once). A check that couldn't run, or wasn't
run, says nothing either way.

  - a test that fails again after it passed in between (fail, pass, fail: two separate times) on the same version
    of the app, or that passes on the second try after an app failure (same app, same run), is flaky. A run a
    deploy started (`new_version` in its summary) begins another version: a test fixed, then broken again by a later
    deploy is a regression each time, broken and alerted, never set aside. A test that just broke (passes, then
    failures) is broken, in red; one that was fixed (failures, then passes) is simply passing again.
  - while flaky, a failure is shown as flaky (verdict.FLAKY), never as broken, and never alerts.
  - it is trusted again by itself after `run.flaky_clears_after` passes in a row, each on the first try.

Only the last `run.flaky_runs` answers are looked at. Sign-ins are not tests: they are never set aside.
"""

import json
from pathlib import Path

from . import verdict

FAIL, PASS, RETRY = "fail", "pass", "retry"  # RETRY: an app failure, then a pass on the second try


def answer(result):
    """What one run says about the test: FAIL | PASS | RETRY, or None when it says nothing about the app."""
    cause = verdict.cause_of(result)
    if not cause:
        return RETRY if _failed_first(result) else PASS
    return FAIL if cause == verdict.APP else None


def _failed_first(result):
    if result.get("attempts", 1) < 2:
        return False
    if "first_cause" in result:
        return result["first_cause"] == verdict.APP
    # reports written before first_cause existed: the retry's own warning says why
    return any(w.startswith("first try failed") for w in result.get("warnings", []))


def is_signin(result):
    return result.get("name", "").partition("@")[0] == "login"


def target(summary):
    """What runs are compared on: the environment, and for previews the address too (as the run pages do)."""
    environment = (summary or {}).get("environment") or "production"
    return environment, "" if environment == "production" else (summary or {}).get("base_url") or ""


def past_runs(output_dir):
    """The reports kept next to this run's folder, oldest first: [(summary, results)]. A folder that can't be read
    is left out (a run that was stopped writes no report)."""
    output_dir = Path(output_dir).resolve()
    if not output_dir.parent.is_dir():
        return []
    found = []
    for folder in sorted(output_dir.parent.iterdir(), key=lambda d: d.name):
        report = folder / "results.json"
        if folder == output_dir or folder.name.startswith("init") or not report.is_file():
            continue
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found.append((data.get("summary") or {}, data.get("results") or []))
    return found


def state(answers, clears_after, versions=()):
    """-> (flaky, passes in a row since it was set aside), reading the answers oldest first. A failure right after
    a failure is the same episode (still broken); a failure after a pass starts a new one. `versions`: for each
    answer, whether a deploy started that run: failures before it are another version's, never counted with it."""
    flaky, episodes, last, streak = False, 0, None, 0
    for index, one in enumerate(answers):
        if index < len(versions) and versions[index]:
            flaky, episodes, last, streak = False, 0, last, 0  # a new version of the app: its own story
        if one == RETRY:
            flaky = True  # failed and passed on the same app within one run
        elif one == FAIL and last != FAIL:
            episodes += 1
        if episodes >= 2:
            flaky = True
        last = FAIL if one == FAIL else PASS
        if flaky:
            streak = streak + 1 if one == PASS else 0
            if streak >= clears_after:
                flaky, episodes, streak = False, 0, 0
    return flaky, streak


def mark(results, summary, past, *, runs, clears_after):
    """Sets `flaky` on each test result of this run that is flaky now: {"failed": runs it failed in, "runs": runs
    looked at, "clears_after": passes in a row that trust it again, "passes_left": how many more it needs}.
    `past`: [(summary, results)] of earlier runs, oldest first. -> the results (changed in place)."""
    here = target(summary)
    history = {}
    for past_summary, past_results in past:
        if target(past_summary) != here:
            continue
        for result in past_results:
            one = answer(result)
            if one and not is_signin(result):
                history.setdefault(result.get("name"), []).append((one, bool(past_summary.get("new_version"))))
    for result in results:
        now = answer(result)
        result.pop("flaky", None)
        if now is None or is_signin(result):
            continue
        seen = [*history.get(result.get("name"), []), (now, bool((summary or {}).get("new_version")))][-runs:]
        answers = [one for one, _ in seen]
        flaky, streak = state(answers, clears_after, [new for _, new in seen])
        if flaky:
            result["flaky"] = {"failed": sum(a in (FAIL, RETRY) for a in answers), "runs": len(answers),
                               "clears_after": clears_after, "passes_left": clears_after - streak}
    return results
