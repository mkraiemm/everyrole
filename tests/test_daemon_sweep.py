"""A run killed from outside never reached its own release, and its browser-control daemons ran on (17 on the
one PC). The next run stops the daemons of folders whose owner process is gone."""

import os
import subprocess
import sys

from everyrole import daemons
from everyrole.processes import process_alive


def test_daemons_of_a_run_whose_process_is_gone_are_stopped(tmp_path):
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True,
                          text=True).stdout.strip()
    left = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        dead = tmp_path / (daemons.PREFIX + "dead")
        (dead / "worker-1").mkdir(parents=True)
        (dead / daemons.OWNER).write_text(gone, encoding="utf-8")
        (dead / "worker-1" / "bu.pid").write_text(str(left.pid), encoding="utf-8")
        alive = tmp_path / (daemons.PREFIX + "alive")
        (alive / "worker-1").mkdir(parents=True)
        (alive / daemons.OWNER).write_text(str(os.getpid()), encoding="utf-8")
        unmarked = tmp_path / (daemons.PREFIX + "hosted")
        unmarked.mkdir()
        assert daemons.sweep(tmp_path) == 1
        left.wait(timeout=20)
        assert not process_alive(left.pid) and not dead.exists()
        assert alive.exists() and unmarked.exists()  # a running owner's, and one with no mark: left alone
    finally:
        if left.poll() is None:
            left.kill()


def test_the_owner_mark_is_never_taken_for_a_daemon(tmp_path, monkeypatch):
    monkeypatch.delenv(daemons.JOB_ENV, raising=False)
    monkeypatch.setattr(daemons.tempfile, "mkdtemp", lambda prefix: str(tmp_path / (prefix + "x")) if
                        (tmp_path / (prefix + "x")).mkdir() is None else "")
    folder = daemons.claim()
    assert (folder / daemons.OWNER).read_text(encoding="utf-8") == str(os.getpid())
    assert daemons.stop(folder) == 0  # stop() reads *.pid files only: it never stops the run itself
