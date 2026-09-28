import json
import os
import subprocess
import sys

from everyrole import daemons
from everyrole.processes import process_alive


def _daemon(job, worker, record):
    """A daemon the way the harness registers it: plain bu.pid in the worker's own folder."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (job / worker).mkdir(parents=True, exist_ok=True)
    (job / worker / "bu.pid").write_text(record(child.pid))
    return child


def test_a_job_stops_every_daemon_its_workers_left_and_removes_their_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(daemons.JOB_ENV, raising=False)
    folder = daemons.claim()
    assert os.environ[daemons.JOB_ENV] == str(folder) and folder.name.startswith("everyrole-daemons-")
    # each worker's daemon registers in its own subfolder: the harness would name them all bu.* otherwise
    assert daemons.for_worker("sw-1-a") == {daemons.ENV: str(folder / "sw-1-a")}
    plain = _daemon(folder, "sw-1-a", str)
    recorded = _daemon(folder, "sw-1-b", lambda pid: json.dumps({"pid": pid, "started": 1}))
    daemons.release()
    for child in (plain, recorded):
        child.wait(timeout=10)
        assert not process_alive(child.pid)
    assert not folder.exists()


def test_one_workers_daemon_can_be_stopped_alone(tmp_path):
    mine, other = _daemon(tmp_path, "sw-1-login-paid", str), _daemon(tmp_path, "sw-1-projects-paid", str)
    try:
        assert daemons.stop(tmp_path, name="sw-1-login-paid") == 1
        mine.wait(timeout=10)
        assert process_alive(other.pid)
    finally:
        other.kill()
        other.wait()
