"""How long each test takes, measured: tests are classed by the time they need, so a deploy's check never waits for
the length of the longest one.

A test's time is the median of its last runs that really ran it (never one that couldn't start), so one slow run
doesn't move it and the app getting faster or slower does. Nothing is guessed: a test never run has no time yet.

Classes:
  quick   under QUICK_S
  medium  up to LONG_S
  long    more than LONG_S: it waits on the app's own slow work (a film being made, an export). A long test never
          runs in a deploy's check (people would wait for it); it runs with the daily run and when run by hand.
"""

import json
from pathlib import Path
from statistics import median

from .file_memo import FileMemo

QUICK_S = 120
LONG_S = 600
LAST_RUNS = 5  # the runs a test's time is measured on
RAN = {"passed", "failed_expect", "blocked", "budget_exceeded", "forbidden_action", "failed"}  # it really ran

QUICK, MEDIUM, LONG = "quick", "medium", "long"


def measured(past, last=LAST_RUNS):
    """[(summary, results)] oldest first (flaky.past_runs) -> {test name: its time in ms}, from its last runs that
    really ran it."""
    times = {}
    for _summary, results in past:
        for result in results:
            ms = result.get("duration_ms") or 0
            if ms > 0 and result.get("name") and result.get("status") in RAN:
                times.setdefault(result["name"], []).append(ms)
    return {name: int(median(seen[-last:])) for name, seen in times.items()}


def timings(path):
    """A run's results.json -> (its summary, each test's name, status and time): all `measured` reads."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return (data.get("summary") or {}, [{k: r.get(k) for k in ("name", "status", "duration_ms")}
                                        for r in data.get("results") or [] if isinstance(r, dict)])


# each run's timings while its results.json stays the same (file_memo.py): the Tests tab read every run in full
RUN_TIMINGS = FileMemo(timings, size=4096)


def past_of(run_dirs):
    """Run folders (each with its results.json) -> [(summary, results)] oldest first, like flaky.past_runs; each result
    only its name, status and time (shared: never changed)."""
    found = []
    for folder in sorted((Path(d) for d in run_dirs), key=lambda d: d.name):
        try:
            found.append(RUN_TIMINGS.get(folder / "results.json"))
        except (OSError, ValueError):
            continue
    return found


def with_first(times, flows):
    """`times`, completed with each kept journey's time from the change's run it was kept from (`measured_ms`): a
    test kept from a change has a time before its first run as a test."""
    first = {f["name"]: f["measured_ms"] for f in flows if isinstance(f, dict) and f.get("measured_ms")}
    return {**first, **times}


def class_of(ms):
    """A test's time in ms -> quick, medium or long (None: never measured)."""
    if not ms:
        return None
    seconds = ms / 1000
    return QUICK if seconds < QUICK_S else MEDIUM if seconds <= LONG_S else LONG


def is_long(name, times):
    return class_of(times.get(name)) == LONG


def estimate(names, times, parallel):
    """About how long running these tests takes, in ms, with `parallel` at once: the longest one, or all of them
    shared between the parallel lanes, whichever is longer. Tests never measured count as the median of the
    measured ones (None when nothing was ever measured)."""
    known = [times[n] for n in names if times.get(n)]
    if not known:
        return None
    typical = int(median(known))
    all_times = [times.get(n) or typical for n in names]
    return max(max(all_times), int(sum(all_times) / max(1, parallel)))


def without_long(flows, past):
    """A deploy's check: the parsed flows without the long ones. -> (flows to run, {long one's name: its time})."""
    times = {**{f.name: f.measured_ms for f in flows if f.measured_ms}, **measured(past)}
    long = {f.name: times[f.name] for f in flows if is_long(f.name, times)}
    return [f for f in flows if f.name not in long], long


def of_tests(flows, run_dirs, parallel):
    """An app's kept tests (their JSON) and its run folders -> {"times": {name: ms}, "full_run", "deploy_check"
    (spoken; "" when it is the full run), "long": [names]}: what the Tests page and the agents show."""
    times = with_first(measured(past_of(run_dirs)), flows)
    names = [f["name"] for f in flows]
    long = [n for n in names if is_long(n, times)]
    quick = [n for n in names if n not in long]
    return {"times": times, "full_run": spoken(estimate(names, times, parallel)),
            "deploy_check": spoken(estimate(quick, times, parallel)) if long else "", "long": long}


def spoken(ms):
    """A time people read: "about 40 s", "about 4 min", "about 38 min"."""
    if not ms:
        return ""
    seconds = ms / 1000
    if seconds < 90:
        return f"about {max(5, round(seconds / 5) * 5)} s"
    return f"about {round(seconds / 60)} min"
