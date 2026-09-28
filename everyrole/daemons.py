"""The browser-control daemons a job's workers start, and making sure none outlives the job.

Each worker starts a browser-harness daemon that runs detached (outside the worker's process tree), and stops it
when it exits normally. A worker stopped at its time limit, a crash, or a job stopped by a person used to leave it
running: hundreds piled up. So every job has a folder for its daemons, and each worker's daemon registers in its own
subfolder of it (BH_RUNTIME_DIR: the harness names its files plain "bu.*" there, so one folder per worker keeps them
apart); everything registered is stopped when a worker ends badly and when the job ends or is stopped.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from .processes import process_alive, stop_tree

ENV = "BH_RUNTIME_DIR"  # the harness's own setting: where a daemon registers (pid, socket or port files)
JOB_ENV = "EVERYROLE_DAEMONS"  # this job's folder; each worker's daemon gets a subfolder of it


def claim():
    """This job's daemon folder, made once."""
    if not os.environ.get(JOB_ENV):
        os.environ[JOB_ENV] = tempfile.mkdtemp(prefix="everyrole-daemons-")
    return Path(os.environ[JOB_ENV])


def for_worker(name):
    """Where the worker called `name` registers its daemon: {BH_RUNTIME_DIR: its own subfolder}."""
    folder = claim() / name
    folder.mkdir(parents=True, exist_ok=True)
    return {ENV: str(folder)}


def _pid(path):
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return int(text)
    except ValueError:
        try:
            record = json.loads(text)
        except ValueError:
            return None
        return record.get("pid") if isinstance(record, dict) else None


def stop(directory, name=None):
    """Stops the daemons registered in the job's `directory` (only worker `name`'s, when given). -> how many
    were running."""
    directory = Path(directory) / name if name else Path(directory)
    if not directory.is_dir():
        return 0
    stopped = 0
    for pid_file in directory.rglob("*.pid"):
        pid = _pid(pid_file)
        if pid and process_alive(pid):
            stop_tree(pid)
            stopped += 1
        pid_file.unlink(missing_ok=True)
    return stopped


def release(directory=None):
    """End of a job: stops whatever is still registered and removes the folder."""
    directory = Path(directory or os.environ.get(JOB_ENV) or "")
    if directory.name.startswith("everyrole-daemons-") and directory.is_dir():
        stop(directory)
        shutil.rmtree(directory, ignore_errors=True)
