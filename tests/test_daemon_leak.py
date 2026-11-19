"""No browser-control daemon outlives the worker that started it (33 daemons once still ran on one
PC after an afternoon of test runs, the server and its worker still up).

Each browser a worker opens checks that its daemon answers within a second, else starts a new one; the new one
takes over the harness's only record of it (one pid file). On a busy machine the old one, too slow to answer, was
forgotten and ran on: every stop (the worker's own, its runner's, the job's, the next run's sweep) reads that file.
And on Windows a group is a process tree, which "taskkill /T" only walks from a process still running: once the
worker had ended, nothing found the daemons it started."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from everyrole import daemons, limits, processes
from everyrole.limits import Budget, Governor

windows = pytest.mark.skipif(sys.platform != "win32", reason="a daemon is found by its parent on Windows")

# A worker opens a browser, its daemon is too busy to answer (suspended for 4 s, as a loaded machine does), the
# worker opens a second browser: the harness starts a second daemon. The worker says which daemons it started, then
# ends the way argv[1] says: "crash" (nothing of its own runs), "own" (the worker's own cleanup).
WORKER = """
import ctypes, json, os, sys, threading, time, urllib.parse
from pathlib import Path
from jev_ultrafast.browser import Browser
from everyrole import daemons, processes

home = Path(os.environ[daemons.ENV])
url = 'data:text/html,' + urllib.parse.quote('<p>hi</p>')
Browser(url, isolated=True)
first = daemons._pid(home / 'bu.pid')
k32, nt = ctypes.WinDLL('kernel32'), ctypes.WinDLL('ntdll')
k32.OpenProcess.restype = ctypes.c_void_p
handle = ctypes.c_void_p(k32.OpenProcess(0x0800, False, first))  # suspend/resume
nt.NtSuspendProcess(handle)
resumed = threading.Timer(4, lambda: nt.NtResumeProcess(handle))
resumed.start()
Browser(url, isolated=True)
resumed.join()
print(json.dumps({'daemons': [first, daemons._pid(home / 'bu.pid')], 'started': processes.children(os.getpid())}),
      flush=True)
if sys.argv[1] == 'own':
    from everyrole import worker
    worker.stop_daemons()
os._exit(0)
"""


def _gone(pids, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        alive = [pid for pid in pids if processes.process_alive(pid)]
        if not alive:
            return []
        time.sleep(0.25)
    return alive


def _run_worker(tmp_path, how, run):
    import jev_ultrafast

    import everyrole
    from everyrole.chrome import chrome_session

    home = tmp_path / "daemons"
    home.mkdir()
    code = os.pathsep.join(str(Path(m.__file__).parents[1]) for m in (everyrole, jev_ultrafast))
    with chrome_session() as chrome:
        env = {**os.environ, "BU_CDP_URL": chrome.url, "BU_NAME": f"leak-{how}-{os.getpid()}", daemons.ENV: str(home),
               "PYTHONPATH": code}
        done = run([sys.executable, "-c", WORKER, how], env=env, text=True, encoding="utf-8", timeout=150)
        assert done.returncode == 0, done.stderr[-800:]
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        # the leak's cause, really happened: a second daemon replaced the first in the record, both started here
        assert len(set(seen["daemons"])) == 2 and len(seen["started"]) == 2, seen
        left = _gone(seen["daemons"] + seen["started"])
        if left:  # never leave them running on the machine, even when the test fails
            for pid in left:
                processes.stop_tree(pid)
    return left


@windows
def test_a_daemon_the_record_forgot_is_stopped_when_its_worker_crashed(tmp_path, monkeypatch):
    """The worker ends without any cleanup of its own: what runs it (limits.run, as every run's workers are) stops
    every daemon it started, recorded or not. Daemons it started: 2 while it ran, 0 once it ended."""
    budget = Budget(memory_bytes=4 * 2**30, cpu_percent=50, cpus=4)
    job = limits.WindowsJob(budget)
    try:
        assert _run_worker(tmp_path, "crash", limits.Limits(budget, job, Governor(budget, job)).run) == []
    finally:
        job.close()


@windows
def test_the_worker_itself_stops_a_daemon_the_record_forgot(tmp_path):
    """Run as a plain process (nothing stops what it left): the worker's own end stops both daemons."""
    assert _run_worker(tmp_path, "own", lambda *a, **k: subprocess.run(*a, capture_output=True, **k)) == []


@pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="which process started which: Windows, /proc")
def test_what_a_process_started_is_stopped_but_not_the_process(tmp_path):
    """stop_children(): what this process started goes (a group of its own or not); the process itself stays."""
    script = ("import json, subprocess, sys, time\n"
              "from everyrole import processes\n"
              "kids = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], **opts)\n"
              "        for opts in ({}, processes.own_group())]\n"
              "time.sleep(1)\n"
              "pids = [k.pid for k in kids]\n"
              "found = processes.children(processes.os.getpid())\n"
              "processes.stop_children()\n"
              "print(json.dumps({'pids': pids, 'found': found}), flush=True)\n")
    import everyrole

    env = {**os.environ, "PYTHONPATH": str(Path(everyrole.__file__).parents[1])}
    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]  # it stopped its children, not itself
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert set(seen["pids"]) <= set(seen["found"])
    assert _gone(seen["found"]) == []
