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
