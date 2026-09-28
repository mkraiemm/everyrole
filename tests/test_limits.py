import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from everyrole import limits
from everyrole.limits import Budget, Container, Governor, Machine, OwnCgroup, settings_of


def test_defaults_come_from_the_schema_and_a_partial_setting_keeps_the_rest():
    assert settings_of(None) == {"memory_percent": 50, "cpu_percent": 50}
    assert settings_of({"parallel": 3, "limits": {"cpu_percent": 25}}) == {"memory_percent": 50, "cpu_percent": 25}


def test_the_budget_is_a_share_of_this_computer():
    budget = Budget.of({"memory_percent": 25, "cpu_percent": 40}, Machine(memory_bytes=8 * 2**30, cpus=4))
    assert budget == Budget(memory_bytes=2 * 2**30, cpu_percent=40.0, cpus=4)
    assert budget.describe() == "2.0 GB memory and 40% CPU of 4 cores"


class Meter(Container):
    def __init__(self, memory=None, cpu=None):
        self.memory, self.cpu = memory, cpu

    def memory_used(self):
        return self.memory

    def cpu_seconds(self):
        return self.cpu


def _second_waits(governor, container, *, free):
    """Holds one slot, tries a second from another thread; `free` makes room. -> whether the second had to wait."""
    entered = threading.Event()

    def second():
        with governor.slot():
            entered.set()

    with governor.slot():
        worker = threading.Thread(target=second)
        worker.start()
        waited = not entered.wait(limits.SAMPLE_S * 1.5)
        free(container)
    worker.join(5)
    assert entered.is_set()  # it always gets to run once there is room
    return waited


def test_a_worker_waits_while_one_more_would_pass_the_memory_cap():
    budget = Budget(memory_bytes=1000, cpu_percent=100, cpus=1)
    full = Meter(memory=600)  # one running worker uses 600: a second would bring it to 1200
    governor = Governor(budget, full)
    assert _second_waits(governor, full, free=lambda c: setattr(c, "memory", 100))
    assert governor.held == 1 and governor.peak_memory == 600


def test_a_worker_starts_at_once_when_it_fits():
    budget = Budget(memory_bytes=1000, cpu_percent=100, cpus=1)
    light = Meter(memory=300)
    governor = Governor(budget, light)
    assert not _second_waits(governor, light, free=lambda c: None)
    assert governor.held == 0 and governor.most_at_once == 2


class Busy(Container):
    """CPU time that grows at `share` of `cpus` cores while the clock runs."""
    def __init__(self, share, cpus):
        self.share, self.cpus, self.used, self.since = share, cpus, 0.0, time.monotonic()

    def set(self, share):
        self.used, self.since, self.share = self.cpu_seconds(), time.monotonic(), share

    def memory_used(self):
        return 1

    def cpu_seconds(self):
        return self.used + (time.monotonic() - self.since) * self.cpus * self.share


def test_a_worker_waits_while_the_job_uses_its_whole_cpu_share():
    busy = Busy(share=0.6, cpus=2)  # 60% of both cores: over the 50% share
    governor = Governor(Budget(memory_bytes=10**12, cpu_percent=50, cpus=2), busy)
    governor._cpu_percent()
    time.sleep(limits.SAMPLE_S)
    assert _second_waits(governor, busy, free=lambda c: c.set(0.1))


def test_the_first_worker_always_runs_even_over_the_caps():
    governor = Governor(Budget(memory_bytes=1, cpu_percent=5, cpus=1), Meter(memory=10**9))
    with governor.slot():
        assert governor.running == 1


def _fake_cgroup_tree():
    root = Path(tempfile.mkdtemp(prefix="everyrole-cgroup-"))
    (root / "cgroup.controllers").write_text("cpu memory pids")
    (root / "cgroup.subtree_control").write_text("pids")
    home = root / "user.slice"
    home.mkdir()
    (home / "cgroup.procs").write_text("")
    proc = root / "self-cgroup"
    proc.write_text("0::/user.slice\n")
    return root, proc


def test_linux_group_is_made_with_both_caps_and_holds_the_job():
    root, proc = _fake_cgroup_tree()
    real_mkdir = Path.mkdir

    def mkdir(path, *a, **k):  # the kernel creates a group's files; the fake tree does it here
        real_mkdir(path, *a, **k)
        for name in ("cgroup.subtree_control", "cgroup.procs", "memory.max", "memory.swap.max", "cpu.max"):
            if not (path / name).exists():
                (path / name).write_text("max" if name.endswith(".max") else "")

    Path.mkdir = mkdir
    try:
        group = OwnCgroup(Budget(memory_bytes=2 * 2**30, cpu_percent=50, cpus=4), root=root, pid=4242, proc=proc)
    finally:
        Path.mkdir = real_mkdir
    job = root / "everyrole" / "job-4242"
    assert (root / "cgroup.subtree_control").read_text() == "+memory +cpu"
    assert (root / "everyrole" / "cgroup.subtree_control").read_text() == "+memory +cpu"
    assert (job / "memory.max").read_text() == str(2 * 2**30)
    assert (job / "memory.swap.max").read_text() == "0"
    assert (job / "cpu.max").read_text() == "200000 100000"  # half of 4 cores
    assert (job / "cgroup.procs").read_text() == "4242"
    assert group.enforced == {"memory": True, "cpu": True}

    (job / "memory.current").write_text("123456")
    (job / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 2000000\n")
    assert group.memory_used() == 123456 and group.cpu_seconds() == 2.5

    (job / "cgroup.kill").write_text("")
    (job / "cgroup.procs").write_text("")  # the kernel empties the group once everything in it has ended
    group.close()
    assert (root / "user.slice" / "cgroup.procs").read_text() == "4242"  # moved back home
    assert (job / "cgroup.kill").read_text() == "1"  # and whatever was left in the group was ended


windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")

HUNGRY = "import sys; sys.stdin.readline(); b = bytearray({size}); print('allocated', flush=True)"


def _in_job(job, code):
    # the venv's python.exe on purpose: a launcher whose real Python is its child, like Chrome's own processes;
    # started suspended and added first, what it starts lands in the job too
    return job.launch([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True)


@windows
def test_windows_job_holds_memory_and_cpu_and_measures_them():
    job = limits.WindowsJob(Budget(memory_bytes=200 * 2**20, cpu_percent=25, cpus=4))
    try:
        assert job.enforced == {"memory": True, "cpu": True}
        child = _in_job(job, HUNGRY.format(size=500 * 2**20))  # past the 200 MB cap
        assert job.memory_used() > 0 and job.cpu_seconds() >= 0
        out, err = child.communicate("go\n", timeout=30)
        assert "allocated" not in out and "MemoryError" in err
        fits = _in_job(job, HUNGRY.format(size=20 * 2**20))
        out, _ = fits.communicate("go\n", timeout=30)
        assert "allocated" in out
    finally:
        job.close()


@windows
def test_a_chrome_started_under_the_caps_is_in_the_job_with_all_its_processes(monkeypatch):
    from everyrole.chrome import chrome_session

    budget = Budget(memory_bytes=4 * 2**30, cpu_percent=50, cpus=4)
    job = limits.WindowsJob(budget)
    monkeypatch.setattr(limits, "_active", limits.Limits(budget, job, Governor(budget, job)))
    try:
        with chrome_session():
            time.sleep(2)
            assert len(job.pids()) >= 3  # the browser and what it started: renderer, GPU...
            assert job.memory_used() > 50 * 2**20
    finally:
        job.close()


@windows
def test_a_worker_started_after_the_browser_is_held_too(monkeypatch):
    """Chrome starts outside any job, a Store Python inside a job of its own: in either order both are held."""
    from everyrole.chrome import chrome_session

    budget = Budget(memory_bytes=1536 * 2**20, cpu_percent=50, cpus=4)
    job = limits.WindowsJob(budget)
    monkeypatch.setattr(limits, "_active", limits.Limits(budget, job, Governor(budget, job)))
    try:
        with chrome_session():
            hog = _in_job(job, HUNGRY.format(size=3 * 2**30))  # past the cap
            out, err = hog.communicate("go\n", timeout=60)
            assert "allocated" not in out and "MemoryError" in err
            assert job.memory_used() > 50 * 2**20  # the browser's processes are counted
    finally:
        job.close()


@windows
def test_closing_the_windows_job_ends_what_is_left_in_it():
    job = limits.WindowsJob(Budget(memory_bytes=2**30, cpu_percent=50, cpus=4))
    left = _in_job(job, "import time; time.sleep(120)")  # like a Chrome a crashed run never closed
    job.close()
    assert left.wait(timeout=10) is not None
