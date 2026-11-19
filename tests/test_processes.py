"""stop_tree on Linux and macOS: a process's own group is stopped, never the caller's."""

import signal

from everyrole import processes


def _unix(monkeypatch, group, own):
    """stop_tree as on Linux: `pid`'s process group is `group`, this process's own is `own`. -> what it signalled."""
    sent = []
    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(processes.os, "getpgid", lambda pid: group, raising=False)
    monkeypatch.setattr(processes.os, "getpgrp", lambda: own, raising=False)
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: sent.append(("kill", pid, sig)))
    monkeypatch.setattr(processes.os, "killpg", lambda pgid, sig: sent.append(("killpg", pgid, sig)), raising=False)
    return sent, signal.SIGTERM


def test_stopping_a_tree_stops_its_own_group(monkeypatch):
    sent, term = _unix(monkeypatch, group=500, own=100)
    processes.stop_tree(501)
    assert sent == [("killpg", 500, term)]


def test_stopping_a_process_in_the_callers_group_never_stops_the_caller(monkeypatch):
    """A daemon (or a reused pid) in the caller's own group: stopping that group took the caller, its parent and the
    CI job running them. Only the process itself is stopped."""
    sent, term = _unix(monkeypatch, group=100, own=100)
    processes.stop_tree(123)
    assert sent == [("kill", 123, term)]
