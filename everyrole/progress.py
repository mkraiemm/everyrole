"""A run's live state for people watching it: progress.json in the run folder, rewritten after every change.

Units, in the order they run: the site check, one sign-in per kind of user, then every test and every kind of
user's page-access checks (run together). Each unit: queued -> running -> passed | retry | failed | not_checked |
not_run, with its start and end times and, when it did not pass, the reason. Written atomically (a reader never
sees half a file); a run without an output folder keeps no file.

A test's whole result is also kept the moment it ends (finished/<key>.json), so its replay can be watched while the
rest of the run goes on; the report (results.json) comes only at the end.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from . import verdict

FINISHED = "finished"  # next to progress.json: each test's result as soon as it ends


def finished_path(run_dir, key):
    return Path(run_dir) / FINISHED / (re.sub(r"[^A-Za-z0-9@._-]", "_", key) + ".json")


class Progress:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.lock = threading.Lock()
        self.state = {"started": clock(), "ended": 0, "units": []}

    def plan(self, units):
        """units: [{"key", "kind" (site | signin | test | access), "persona"}], in running order."""
        with self.lock:
            self.state["units"] = [{**u, "state": "queued", "started": 0, "ended": 0, "detail": ""} for u in units]
            self._write()

    def track(self, key, work):
        """Run `work()` as unit `key`, recording when it starts and how it ended. -> work's result."""
        self._update(key, state="running", started=self.clock())
        result = work()
        state, detail = outcome(result)
        self._keep(key, result)  # before it shows as ended: whoever sees it ended can open its replay
        self._update(key, state=state, detail=detail, ended=self.clock(), result=brief(result))
        return result

    def _keep(self, key, result):
        if self.path is None or not isinstance(result, dict) or not result.get("name"):
            return
        path = finished_path(self.path.parent, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def done(self):
        with self.lock:
            self.state["ended"] = self.clock()
            self._write()

    def _update(self, key, **changes):
        with self.lock:
            for unit in self.state["units"]:
                if unit["key"] == key:
                    unit.update(changes)
            self._write()

    def _write(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


def outcome(result):
    """A unit's result -> (state, detail). A page-access unit returns a list of rows; a test or sign-in, one dict;
    the site check, None when the site answered."""
    if result is None:
        return "passed", ""
    if isinstance(result, list):
        states = [verdict.of(row) for row in result]
        failed = [row for row, state in zip(result, states) if state == verdict.FAILED]
        if failed:
            return "failed", f"{len(failed)} of {len(result)} pages not as the rules say"
        if verdict.NOT_CHECKED in states:
            return "not_checked", "some pages could not be checked"
        if verdict.NOT_RUN in states:
            return "not_run", (result[0].get("detail") or "") if result else ""
        return "passed", f"{len(result)} page{'s' if len(result) != 1 else ''} as the rules say"
    state = verdict.of(result)
    if state == verdict.PASSED:
        return ("retry" if result.get("attempts") == 2 else "passed"), ""
    return state, (result.get("error") or result.get("status") or "")[:240]


def brief(result):
    """What a live page needs to word a test's result like the report does (never its steps or pictures)."""
    if not isinstance(result, dict):
        return None
    keep = ("name", "status", "passed", "cause", "error", "attempts", "warnings")
    return {**{k: result[k] for k in keep if k in result},
            "expectations": [e for e in result.get("expectations", []) if not e.get("passed")][:3]}
