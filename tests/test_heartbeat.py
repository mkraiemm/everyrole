"""A test frozen on one call is stopped once it showed no sign of life for longer than a step may take, never at the
end of its whole budget, and the run never waits forever for its output (staging, 2026-11-17: sign-up journeys
"running" for 40+ minutes with nothing moving)."""

import subprocess
import sys
import time

import pytest

from everyrole import heartbeat, limits


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(limits, "WATCH_S", 0.2)
    monkeypatch.setattr(limits, "KILL_WAIT_S", 2)
    return limits.active()


def test_a_test_that_goes_quiet_is_stopped_long_before_its_budget(tmp_path, quick):
    alive = tmp_path / "heartbeat"
    alive.touch()
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        quick.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60, quiet=(alive, 1.5), text=True)
    assert time.monotonic() - started < 15


def test_a_test_that_keeps_beating_runs_to_its_end(tmp_path, quick, monkeypatch):
    alive = tmp_path / "heartbeat"
    alive.touch()
    beats = ("import os, pathlib, time\n"
             "for _ in range(8):\n"
             "    pathlib.Path(os.environ['EVERYROLE_HEARTBEAT']).touch(); time.sleep(0.5)\n"
             "print('done')")
    done = quick.run([sys.executable, "-c", beats], timeout=60, quiet=(alive, 1.5), text=True,
                     env={**__import__("os").environ, heartbeat.ENV: str(alive)})
    assert done.returncode == 0 and done.stdout.strip() == "done"  # 4 s of work, never quiet for 1.5 s


def test_a_child_left_holding_the_output_never_freezes_the_run(tmp_path, quick):
    """The test is stopped, but something it started still holds its output open: the run reads what it can for a
    short while and goes on (it used to wait for that output without a limit)."""
    alive = tmp_path / "heartbeat"
    alive.touch()
    holds = ("import subprocess, sys, time\n"
             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"  # inherits our output
             "time.sleep(120)")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        quick.run([sys.executable, "-c", holds], timeout=60, quiet=(alive, 1), text=True)
    assert time.monotonic() - started < 20


def test_waiting_on_purpose_is_a_sign_of_life(tmp_path, monkeypatch):
    alive = tmp_path / "heartbeat"
    monkeypatch.setenv(heartbeat.ENV, str(alive))
    heartbeat.beating(lambda s: None)(5)
    assert alive.is_file() and heartbeat.quiet_for(alive) < 5
    assert heartbeat.quiet_for(tmp_path / "missing") == 0.0
