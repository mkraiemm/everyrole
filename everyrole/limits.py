"""How much of the computer a test job may use: a memory cap and a CPU cap, enforced by the operating system.

A test job (`everyrole run`) sets up, before it starts any browser, a container
the OS enforces, and every worker and every Chrome it starts runs inside it (`Limits.launch` / `Limits.run`):

  Windows   job objects: committed memory and a hard CPU rate. Each browser and worker starts suspended, outside
            whatever job started EveryRole when it may (a terminal's job may let children silently break away, which
            takes them out of every job below it), is put in a job, then runs: all it starts stays in. A process
            already in a job of its own (a Microsoft Store Python always is) can only join a job in that job's
            family, so each family gets its own job with the same caps and the governor keeps the sum under
            them. Closing the jobs (the command ending, even by a crash) ends every process still in them.
  Linux     a cgroup v2 group with memory.max and cpu.max: created directly when EveryRole may write the cgroup tree
            (root on a VPS), else a systemd user scope (the command restarts itself inside it). In a container
            whose cgroup is read-only, the container's own limits are the caps and are reported as such.

The caps are shares of this computer (`run.limits` in everyrole.config.json), measured at start. On top of them a
governor starts a new worker only when what the running ones use, plus what one more of them uses on average, fits
under the caps: the job slows down instead of hitting the wall.
"""

import atexit
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import heartbeat, processes
from .spec import _defaults, schema

SAMPLE_S = 1.0  # how often a held-back worker looks again, and the shortest window a CPU rate is measured over
SCOPE_ENV = "EVERYROLE_SCOPE"  # set in the command restarted inside a systemd scope, so it does not restart again


# ---------------------------------------------------------------------------------------------------- the caps

def settings_of(run=None):
    """`run.limits` with the schema's defaults filled in."""
    props = schema("config.schema.json")["properties"]["run"]["properties"]["limits"]["properties"]
    return _defaults(((run or {}).get("limits") or {}), props)


@dataclass(frozen=True)
class Budget:
    memory_bytes: int
    cpu_percent: float  # of the whole computer (all cores = 100)
    cpus: int

    @classmethod
    def of(cls, settings, machine):
        return cls(memory_bytes=int(machine.memory_bytes * settings["memory_percent"] / 100),
                   cpu_percent=float(settings["cpu_percent"]), cpus=machine.cpus)

    def describe(self):
        return f"{_gb(self.memory_bytes)} memory and {self.cpu_percent:g}% CPU of {self.cpus} cores"


@dataclass(frozen=True)
class Machine:
    memory_bytes: int
    cpus: int

    @classmethod
    def this(cls):
        return cls(memory_bytes=_total_memory(), cpus=os.cpu_count() or 1)


def _total_memory():
    if sys.platform == "win32":
        return _win.total_memory()
    if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        own = _cgroup_limit(Cgroup.own_dir(), "memory.max")  # a container's limit is this computer's size
        return min(total, own) if own else total
    raise OSError("cannot read this computer's memory size")


def _gb(n):
    return f"{n / 2**30:.1f} GB"


# ---------------------------------------------------------------------------------------------------- containers

class Container:
    """Where a job runs. `enforced` says which caps the OS really holds it to, read back from the OS."""
    how = "not contained"
    enforced = {"memory": False, "cpu": False}
    release_at_exit = False  # True: `close` must run when the job ends (the OS does not do it)

    def memory_used(self):
        return None

    def cpu_seconds(self):
        return None

    def launch(self, args, **popen):
        """Starts a process inside the container."""
        return subprocess.Popen(args, **popen)

    def close(self):
        pass


class WindowsJob(Container):
    how = "Windows job objects"
    ACCESS_DENIED = 5  # AssignProcessToJobObject: the process is in a job of another family

    def __init__(self, budget):
        self.budget = budget
        self._jobs = [self._new()]
        self.enforced = _win.job_limits(self._jobs[0])

    def _new(self):
        return _win.create_job(self.budget.memory_bytes, self.budget.cpu_percent)

    def add(self, process_handle):
        """Into the first job that can take it, else a new job with the same caps (an empty job takes anything)."""
        for job in self._jobs:
            try:
                _win.assign(job, process_handle)
                return self
            except OSError as error:
                if error.errno != self.ACCESS_DENIED:
                    raise
        job = self._new()
        _win.assign(job, process_handle)
        self._jobs.append(job)
        return self

    def pids(self):
        return [pid for job in self._jobs for pid in _win.job_pids(job)]

    def launch(self, args, **popen):
        if args and args[0] == sys.executable and sys._base_executable != sys.executable:
            # a venv's python.exe is a launcher that puts the real Python in a job of its own, which lets what
            # that Python starts break away; start the real one directly, told which venv it is (as the launcher
            # itself does)
            args = [sys._base_executable, *args[1:]]
            popen["env"] = {**(popen.get("env") or os.environ), "__PYVENV_LAUNCHER__": sys.executable}
        flags = popen.pop("creationflags", 0) | _Windows.CREATE_SUSPENDED
        try:
            process = subprocess.Popen(args, creationflags=flags | _Windows.CREATE_BREAKAWAY_FROM_JOB, **popen)
        except OSError:  # the job that started EveryRole allows no breakaway: its children stay in the jobs below it
            process = subprocess.Popen(args, creationflags=flags, **popen)
        try:
            self.add(int(process._handle))
        except OSError:
            process.kill()  # never let it run outside the caps
            raise
        _win.resume(int(process._handle))
        return process

    def memory_used(self):
        return sum(_win.job_memory(job) for job in self._jobs)

    def cpu_seconds(self):
        return sum(_win.job_cpu_seconds(job) for job in self._jobs)

    def close(self):
        """Ends every process still in the jobs (a Chrome left behind by a crash included)."""
        for job in self._jobs:
            _win.close(job)
        self._jobs = []


def _cgroup_limit(directory, name):
    try:
        value = (Path(directory) / name).read_text().split()[0]
    except (OSError, IndexError, TypeError):
        return None
    return None if value == "max" else int(value)


class Cgroup(Container):
    """A cgroup v2 group this process is in: reads usage, reports the caps the group really has."""
    ROOT = Path("/sys/fs/cgroup")

    def __init__(self, directory, how):
        self.dir = Path(directory)
        self.how = how
        self.enforced = {"memory": _cgroup_limit(self.dir, "memory.max") is not None,
                         "cpu": _cgroup_limit(self.dir, "cpu.max") is not None}

    @classmethod
    def own_dir(cls, root=None, proc="/proc/self/cgroup"):
        root = Path(root or cls.ROOT)
        try:
            for line in Path(proc).read_text().splitlines():
                if line.startswith("0::"):
                    return root / line[3:].lstrip("/")
        except OSError:
            return None
        return None

    def memory_used(self):
        try:
            return int((self.dir / "memory.current").read_text())
        except (OSError, ValueError):
            return None

    def cpu_seconds(self):
        try:
            for line in (self.dir / "cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec "):
                    return int(line.split()[1]) / 1e6
        except (OSError, ValueError):
            return None
        return None


class OwnCgroup(Cgroup):
    """A group EveryRole made for this job under a cgroup tree it may write (root on a VPS)."""
    release_at_exit = True
    PERIOD_US = 100_000  # cpu.max period; the quota is this times the share of all cores

    def __init__(self, budget, *, root=None, pid=None, proc="/proc/self/cgroup"):
        root = Path(root or self.ROOT)
        pid = pid or os.getpid()
        self._home = self.own_dir(root, proc)
        base = root / "everyrole"
        base.mkdir(exist_ok=True)
        for parent in (root, base):
            _enable(parent, ("memory", "cpu"))
        group = base / f"job-{pid}"
        group.mkdir(exist_ok=True)
        (group / "memory.max").write_text(str(budget.memory_bytes))
        if (group / "memory.swap.max").exists():
            (group / "memory.swap.max").write_text("0")  # swapping out would let the job past its cap
        quota = int(self.PERIOD_US * budget.cpus * budget.cpu_percent / 100)
        (group / "cpu.max").write_text(f"{quota} {self.PERIOD_US}")
        (group / "cgroup.procs").write_text(str(pid))
        self._pid = pid
        super().__init__(group, "a cgroup made for this job")

    def close(self):
        """Moves this process back home and ends everything still in the group, then removes it."""
        if self._home is None or not self.dir.exists():
            return
        try:
            (self._home / "cgroup.procs").write_text(str(self._pid))
        except OSError:
            return
        if (self.dir / "cgroup.kill").exists():
            (self.dir / "cgroup.kill").write_text("1")
        else:
            for pid in (self.dir / "cgroup.procs").read_text().split():
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass
        deadline = time.monotonic() + 10
        while (self.dir / "cgroup.procs").read_text().strip() and time.monotonic() < deadline:
            time.sleep(0.1)
        try:
            self.dir.rmdir()
        except OSError:
            pass


class Scope(Cgroup):
    """The systemd scope this command restarted itself in. Its caps are the job's budget (read back, since this
    process now sees the scope's own memory cap as the computer's size)."""
    release_at_exit = True

    def __init__(self, directory):
        super().__init__(directory, "a systemd scope made for this job")

    def budget(self, cpus):
        quota, period = (self.dir / "cpu.max").read_text().split()
        return Budget(memory_bytes=_cgroup_limit(self.dir, "memory.max"), cpus=cpus,
                      cpu_percent=100.0 if quota == "max" else int(quota) / int(period) / cpus * 100)

    def close(self):
        """Ends whatever this job left in its scope (a Chrome after a crash), so the scope can go away."""
        try:
            pids = [int(p) for p in (self.dir / "cgroup.procs").read_text().split()]
        except OSError:
            return
        for pid in pids:
            if pid != os.getpid():
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass


def _enable(directory, controllers):
    listed = (directory / "cgroup.subtree_control").read_text().split()
    missing = [c for c in controllers if c not in listed]
    if missing:
        (directory / "cgroup.subtree_control").write_text(" ".join(f"+{c}" for c in missing))


def _systemd_scope_command(budget, argv):
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            "-p", f"MemoryMax={budget.memory_bytes}", "-p", "MemorySwapMax=0",
            "-p", "OOMPolicy=continue",  # a tab over the cap ends alone, not the whole job
            "-p", f"CPUQuota={budget.cpu_percent * budget.cpus:g}%", "--", *argv]


def _linux_container(budget):
    own = Cgroup.own_dir()
    if own is None or not (Cgroup.ROOT / "cgroup.controllers").exists():
        return Container()  # no cgroup v2 here
    if os.environ.get(SCOPE_ENV):
        return Scope(own)
    if os.access(Cgroup.ROOT / "cgroup.subtree_control", os.W_OK):
        try:
            return OwnCgroup(budget)
        except OSError:
            pass  # a read-only or delegated tree: try the next way
    if shutil.which("systemd-run") and subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"],
            capture_output=True).returncode == 0:
        sys.stdout.flush()
        os.environ[SCOPE_ENV] = "1"
        argv = [sys.executable, *sys.orig_argv[1:]]
        os.execvp("systemd-run", _systemd_scope_command(budget, argv))
    return Cgroup(own, "this container's own limits")


# ---------------------------------------------------------------------------------------------------- governor

@dataclass
class Governor:
    """Starts a worker only when it fits: what the running workers use now plus what one of them uses on average
    stays under both caps. One worker always may run, so a job always moves forward."""
    budget: Budget
    container: Container
    running: int = 0
    most_at_once: int = 0
    held: int = 0
    peak_memory: int = 0
    _cond: threading.Condition = field(default_factory=threading.Condition)
    _cpu_mark: tuple = None
    _cpu_rate: float = None

    @contextmanager
    def slot(self):
        with self._cond:
            waited = False
            while not self._fits():
                if not waited:
                    self.held += 1
                    waited = True
                self._cond.wait(SAMPLE_S)
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
        try:
            yield
        finally:
            with self._cond:
                self.running -= 1
                self._cond.notify_all()

    def _fits(self):
        memory = self.container.memory_used()
        if memory is not None:
            self.peak_memory = max(self.peak_memory, memory)
        cpu = self._cpu_percent()
        if self.running == 0:
            return True
        if memory is not None and memory + memory / self.running > self.budget.memory_bytes:
            return False
        return cpu is None or cpu + cpu / self.running <= self.budget.cpu_percent

    def _cpu_percent(self):
        """The job's CPU use over the last window, as a share of the whole computer."""
        seconds, now = self.container.cpu_seconds(), time.monotonic()
        if seconds is None:
            return None
        if self._cpu_mark is None:
            self._cpu_mark = (seconds, now)
        elif now - self._cpu_mark[1] >= SAMPLE_S:
            used, then = self._cpu_mark
            self._cpu_rate = (seconds - used) / (now - then) / self.budget.cpus * 100
            self._cpu_mark = (seconds, now)
        return self._cpu_rate


# ---------------------------------------------------------------------------------------------------- the job

@dataclass
class Limits:
    budget: Budget
    container: Container
    governor: Governor

    def slot(self):
        return self.governor.slot()

    def launch(self, args, **popen):
        """subprocess.Popen, inside the caps."""
        return self.container.launch(args, **popen)

    def run(self, args, *, timeout=None, input=None, quiet=None, **popen):
        """subprocess.run with captured output, inside the caps. Whatever the command started is stopped with it,
        however it ended. Raises TimeoutExpired after stopping it and everything it started. `quiet`: (heartbeat
        file, seconds): also stopped once that file went quiet for longer (heartbeat.py: a test frozen on one call)."""
        process = self.launch(args, stdin=subprocess.PIPE if input is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, **{**processes.own_group(), **popen})
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            wait = left if quiet is None else min(WATCH_S, left if left is not None else WATCH_S)
            try:
                out, err = process.communicate(input, timeout=wait)
                processes.end_group(process)  # what it started and left running (a browser daemon it lost) goes too
                return subprocess.CompletedProcess(args, process.returncode, out, err)
            except subprocess.TimeoutExpired:
                input = None  # already sent
                over = deadline is not None and time.monotonic() >= deadline
                if over or (quiet is not None and heartbeat.quiet_for(quiet[0]) > quiet[1]):
                    _stop(process)
                    raise subprocess.TimeoutExpired(args, timeout) from None
            except BaseException:  # stopped itself (Ctrl-C, the job stopped): its group of its own goes with it
                _stop(process)
                raise

    def describe(self):
        held = [k for k, on in self.container.enforced.items() if on]
        free = [k for k, on in self.container.enforced.items() if not on]
        text = f"limits: {self.budget.describe()}"
        if held:
            text += f", {' and '.join(held)} held by {self.container.how}"
        if free:
            text += f"; {' and '.join(free)} not enforced by this system, only paced"
        return text

    def report(self):
        g = self.governor
        text = f"limits: up to {g.most_at_once} at once"
        if g.peak_memory:
            text += f", peak {_gb(g.peak_memory)} of {_gb(self.budget.memory_bytes)}"
        if g.held:
            text += f", {g.held} held back until there was room"
        return text


WATCH_S = 5  # how often a watched process's heartbeat is looked at
KILL_WAIT_S = 15  # after stopping it: what is left to read of its output, then it is let go


def _stop(process):
    """Stops a process and its group, then reads what is left of its output for a short while only: a process it
    started that outlived it may hold its output open, and waiting for that without a limit froze the whole run."""
    processes.end_group(process)
    try:
        process.communicate(timeout=KILL_WAIT_S)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        for pipe in (process.stdout, process.stderr):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass


_active = None


def contain(run=None, *, machine=None):
    """Puts this process (and so everything it starts from now on) under the caps. Once per process."""
    global _active
    if _active is not None:
        return _active
    budget = Budget.of(settings_of(run), machine or Machine.this())
    if sys.platform == "win32":
        container = WindowsJob(budget)
    elif sys.platform.startswith("linux"):
        container = _linux_container(budget)
        if isinstance(container, Scope):
            budget = container.budget(budget.cpus)
    else:
        container = Container()
    if container.release_at_exit:  # a job object is closed by Windows itself when this process ends
        atexit.register(container.close)
    _active = Limits(budget, container, Governor(budget, container))
    return _active


def active():
    """The limits this process runs under; a job that never called `contain` is only counted, not paced."""
    return _active or Limits(None, Container(), Governor(None, Container()))


# ---------------------------------------------------------------------------------------------------- Windows

class _Windows:
    """The few Win32 calls a job object needs, through ctypes (loaded on first use)."""
    JOB_MEMORY, KILL_ON_CLOSE = 0x200, 0x2000
    CREATE_SUSPENDED, CREATE_BREAKAWAY_FROM_JOB = 0x4, 0x01000000
    CPU_ENABLE, CPU_HARD_CAP = 0x1, 0x4
    ACCOUNTING, PID_LIST, EXTENDED, CPU_RATE = 1, 3, 9, 15

    def __getattr__(self, name):
        self._load()
        return object.__getattribute__(self, name)

    def _load(self):
        import ctypes
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class CpuRate(ctypes.Structure):
            _fields_ = [("ControlFlags", w.DWORD), ("CpuRate", w.DWORD)]

        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", w.DWORD), ("TotalProcesses", w.DWORD),
                        ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                        *((n, ctypes.c_size_t) for n in (
                            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                            "PeakPagefileUsage", "PrivateUsage"))]

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", w.DWORD), ("dwMemoryLoad", w.DWORD),
                        *((n, ctypes.c_uint64) for n in (
                            "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                            "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual"))]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = w.HANDLE
        k32.GetCurrentProcess.restype = w.HANDLE
        k32.OpenProcess.restype = w.HANDLE
        for name in ("SetInformationJobObject", "QueryInformationJobObject", "AssignProcessToJobObject",
                     "CloseHandle", "K32GetProcessMemoryInfo", "GlobalMemoryStatusEx"):
            getattr(k32, name).restype = w.BOOL
        k32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k32.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k32.CloseHandle.argtypes = [w.HANDLE]
        k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k32.K32GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
        self.__dict__.update(ctypes=ctypes, k32=k32, Extended=Extended, CpuRate=CpuRate, Accounting=Accounting,
                             Counters=Counters, MemoryStatus=MemoryStatus)

    def _check(self, ok, what):
        if not ok:
            raise OSError(self.ctypes.get_last_error(), f"{what} failed")

    def total_memory(self):
        status = self.MemoryStatus(dwLength=self.ctypes.sizeof(self.MemoryStatus))
        self._check(self.k32.GlobalMemoryStatusEx(self.ctypes.byref(status)), "GlobalMemoryStatusEx")
        return status.ullTotalPhys

    def create_job(self, memory_bytes, cpu_percent):
        job = self.k32.CreateJobObjectW(None, None)
        self._check(job, "CreateJobObject")
        limits = self.Extended()
        limits.Basic.LimitFlags = self.JOB_MEMORY | self.KILL_ON_CLOSE
        limits.JobMemoryLimit = memory_bytes
        self._check(self.k32.SetInformationJobObject(job, self.EXTENDED, self.ctypes.byref(limits),
                                                     self.ctypes.sizeof(limits)), "job memory limit")
        rate = self.CpuRate(ControlFlags=self.CPU_ENABLE | self.CPU_HARD_CAP,
                            CpuRate=max(1, min(10000, round(cpu_percent * 100))))  # in 1/100 of a percent
        self._check(self.k32.SetInformationJobObject(job, self.CPU_RATE, self.ctypes.byref(rate),
                                                     self.ctypes.sizeof(rate)), "job CPU rate")
        return job

    def job_limits(self, job):
        limits, rate = self.Extended(), self.CpuRate()
        self._check(self.k32.QueryInformationJobObject(job, self.EXTENDED, self.ctypes.byref(limits),
                                                       self.ctypes.sizeof(limits), None), "read job limits")
        self._check(self.k32.QueryInformationJobObject(job, self.CPU_RATE, self.ctypes.byref(rate),
                                                       self.ctypes.sizeof(rate), None), "read job CPU rate")
        return {"memory": bool(limits.Basic.LimitFlags & self.JOB_MEMORY and limits.JobMemoryLimit),
                "cpu": bool(rate.ControlFlags & self.CPU_HARD_CAP and rate.CpuRate)}

    def resume(self, process):
        ntdll = self.ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [self.ctypes.c_void_p]
        status = ntdll.NtResumeProcess(process)
        if status != 0:
            raise OSError(f"NtResumeProcess failed (0x{status & 0xFFFFFFFF:08x})")

    def assign(self, job, process):
        self._check(self.k32.AssignProcessToJobObject(job, process), "AssignProcessToJobObject")

    def job_pids(self, job):
        size = 256
        while True:
            class PidList(self.ctypes.Structure):
                _fields_ = [("Assigned", self.ctypes.c_uint32), ("Listed", self.ctypes.c_uint32),
                            ("Ids", self.ctypes.c_size_t * size)]
            pids = PidList()
            self._check(self.k32.QueryInformationJobObject(job, self.PID_LIST, self.ctypes.byref(pids),
                                                           self.ctypes.sizeof(pids), None)
                        or self.ctypes.get_last_error() == 234, "list job processes")  # 234: more data
            if pids.Assigned <= size:
                return list(pids.Ids[:pids.Listed])
            size = pids.Assigned * 2

    def job_memory(self, job):
        """Committed private memory of every process in the job: what the job's memory limit counts."""
        total = 0
        for pid in self.job_pids(job):
            handle = self.k32.OpenProcess(0x1000 | 0x0010, False, pid)  # query limited information, read memory
            if not handle:
                continue  # it just ended
            counters = self.Counters(cb=self.ctypes.sizeof(self.Counters))
            if self.k32.K32GetProcessMemoryInfo(handle, self.ctypes.byref(counters), counters.cb):
                total += counters.PrivateUsage
            self.k32.CloseHandle(handle)
        return total

    def job_cpu_seconds(self, job):
        info = self.Accounting()
        self._check(self.k32.QueryInformationJobObject(job, self.ACCOUNTING, self.ctypes.byref(info),
                                                       self.ctypes.sizeof(info), None), "job CPU time")
        return (info.TotalUserTime + info.TotalKernelTime) / 1e7  # 100 ns units

    def close(self, job):
        self.k32.CloseHandle(job)


_win = _Windows()
