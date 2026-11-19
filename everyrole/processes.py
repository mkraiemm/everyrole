"""Processes EveryRole started, seen from another process."""

import os
import sys


def process_alive(pid):
    """Is this process still running? (A job another EveryRole server started, or one from before a restart.)"""
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, int(pid))  # query limited information
        if not handle:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)  # signal 0 only asks (on Windows it would end the process, hence the branch above)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_tree(pid):
    """Stops a process and every process it started."""
    if sys.platform == "win32":
        import subprocess

        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    import signal

    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def children(pid):
    """The processes `pid` started that still run, also once `pid` itself has ended: Windows keeps a parent's id in
    each process it started. A process started before `pid` is never one of them (its parent was an earlier process
    with the same id). Linux: from /proc, while `pid` runs (the system hands an ended one's children to another
    parent). -> pids; [] where the system does not say which process started which (macOS), or `pid` is gone."""
    if sys.platform == "win32":
        return _windows_children(int(pid))
    return _proc_children(int(pid))


def stop_children(pid=None):
    """Stops every process `pid` (this one by default) started that still runs, each with all it started."""
    for child in children(os.getpid() if pid is None else pid):
        if sys.platform == "win32" or _own_group(child):
            stop_tree(child)
        else:  # it shares its parent's group: stopping that group would stop the parent too
            import signal

            try:
                os.kill(child, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def _own_group(pid):
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError):
        return False


def _windows_children(pid):
    import ctypes
    from ctypes import wintypes as w

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    started = _windows_started(k32, pid)
    if started is None:
        return []
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS: every process and its parent
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    found = []
    try:
        entry = Entry(dwSize=ctypes.sizeof(Entry))
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ParentProcessID == pid and entry.th32ProcessID != pid:
                found.append(entry.th32ProcessID)
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return [child for child in found if (_windows_started(k32, child) or 0) >= started]


def _windows_started(k32, pid):
    """When the process was created (100 ns units), None if it is gone."""
    import ctypes

    k32.OpenProcess.restype = ctypes.c_void_p
    handle = k32.OpenProcess(0x1000, False, pid)  # query limited information
    if not handle:
        return None
    created, ended, kernel, user = (ctypes.c_uint64() for _ in range(4))
    try:
        if not k32.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(ended),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return created.value
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def _proc_children(pid, proc="/proc"):
    from pathlib import Path

    def stat(entry):  # (parent, start time) from /proc/<pid>/stat; the name in () may hold spaces
        try:
            fields = (Path(proc) / str(entry) / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None
        return int(fields[1]), int(fields[19])

    if not Path(proc).is_dir() or (own := stat(pid)) is None:
        return []
    found = []
    for entry in Path(proc).iterdir():
        if entry.name.isdigit() and int(entry.name) != pid:
            seen = stat(entry.name)
            if seen and seen[0] == pid and seen[1] >= own[1]:
                found.append(int(entry.name))
    return found


def own_group():
    """Popen options that put a command and everything it starts (Chrome, ffmpeg) in a group of their own, so
    end_group() can stop all of them however the command ended."""
    import subprocess

    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def end_group(process):
    """Stops what is left of a command started with own_group(): the command itself if it still runs, and every
    process it started that outlived it (a Chrome its crash or timeout left behind). Each one holds threads; left
    running, they filled the container until it could not start a thread (a test server).
    On Windows a group is a process tree, and "taskkill /T" only walks it from a live command: what an ended command
    left running is found by its parent instead (see children()). A test's browser-control daemon it lost track of
    ran on that way: 33 of them on one PC after an afternoon."""
    if sys.platform == "win32":
        import subprocess

        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        # while `process` holds the command's handle its id can't be given to another process: its children are its
        stop_children(process.pid)
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)  # the group keeps the command's id after the command is gone
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(timeout=30)
    except Exception:  # noqa: BLE001 - it was killed; waiting is only to collect it
        pass


def container_load(proc="/proc", cgroup="/sys/fs/cgroup"):
    """How full the machine is in processes and threads, from Linux's own counts. -> {"processes", "threads",
    "limit"} (limit: the most threads and processes the container may hold, None when unlimited or unknown), or None
    where /proc doesn't exist (Windows, macOS)."""
    from pathlib import Path

    root = Path(proc)
    if not root.is_dir():
        return None
    processes = threads = 0
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            threads += sum(1 for _ in (entry / "task").iterdir())
        except OSError:
            continue  # it ended while counting
        processes += 1
    limit = None
    for name in ("pids.max", "pids/pids.max"):
        try:
            text = (Path(cgroup) / name).read_text().strip()
        except OSError:
            continue
        limit = int(text) if text.isdigit() else None
        break
    return {"processes": processes, "threads": threads, "limit": limit}
