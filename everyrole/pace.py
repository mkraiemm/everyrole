"""How fast a job opens pages: one pace shared by every process of the job (workers, browsers), so a job never
floods the network it runs on, whatever its number of workers.

  - at most `run.network.pages_at_once` pages load at the same time, in the whole job;
  - new pages start no closer together than the network allows: the gap is the recent median load time divided by
    the pages allowed at once (so pages start as fast as they finish), kept between `min_gap_s` and `max_gap_s`.
    A slower network means slower loads, so the job slows down by itself;
  - when the app's address stops resolving (the network's DNS gave up), nothing new starts until it resolves
    again; the job says so instead of piling up requests.

The shared state is a small JSON file in a directory the job creates (its path is passed to workers in
EVERYROLE_PACE_DIR), guarded by a lock file, so it works the same on Windows and Linux.
"""

import json
import os
import socket
import statistics
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .processes import process_alive
from .spec import _defaults, schema

ENV = "EVERYROLE_PACE_DIR"
WINDOW = 20  # recent page loads the gap is measured from
WAIT_S = 0.25  # how often a waiting page looks again
LOCK_STALE_S = 10  # a lock held this long belongs to a process that died holding it
DNS_EVERY_S = 10  # how often the app's address is checked to still resolve
DNS_TIMEOUT_S = 5


def settings_of(run=None):
    """`run.network` with the schema's defaults filled in."""
    props = schema("config.schema.json")["properties"]["run"]["properties"]["network"]["properties"]
    return _defaults(((run or {}).get("network") or {}), props)


@dataclass(frozen=True)
class Settings:
    pages_at_once: int
    min_gap_s: float
    max_gap_s: float
    page_timeout_s: float

    @classmethod
    def of(cls, run=None):
        s = settings_of(run)
        return cls(pages_at_once=s["pages_at_once"], min_gap_s=s["min_gap_s"], max_gap_s=s["max_gap_s"],
                   page_timeout_s=s["page_timeout_s"])


def resolves(host, timeout_s=DNS_TIMEOUT_S):
    """Does `host` resolve within `timeout_s`? (getaddrinfo has no timeout of its own.)"""
    answer = []
    worker = threading.Thread(target=lambda: answer.append(_lookup(host)), daemon=True)
    worker.start()
    worker.join(timeout_s)
    return bool(answer and answer[0])


def _lookup(host):
    try:
        return bool(socket.getaddrinfo(host, 443))
    except OSError:
        return False


class Pace:
    def __init__(self, directory, settings, *, clock=time.time, sleep=time.sleep, resolve=resolves):
        self.dir = Path(directory)
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self._resolve = resolve

    # ------------------------------------------------------------------ pages

    @contextmanager
    def page(self, url):
        """Waits for the network to have room, then holds a place while the page loads."""
        ticket = self.acquire(url)
        try:
            yield
        finally:
            self.release(ticket)

    def acquire(self, url):
        host = urlparse(url).hostname
        while True:
            if host and not self._dns_fine(host):
                self._sleep(WAIT_S * 4)
                continue
            with self._state() as state:
                now = self._clock()
                self._drop_stale(state, now)
                room = len(state["open"]) < self.settings.pages_at_once
                if room and now - state["last_start"] >= self.gap(state):
                    ticket = uuid.uuid4().hex
                    state["open"][ticket] = {"pid": os.getpid(), "at": now, "url": url}
                    state["last_start"] = now
                    return ticket
            self._sleep(WAIT_S)

    def release(self, ticket):
        with self._state() as state:
            entry = state["open"].pop(ticket, None)
            if entry:
                state["loads"] = (state["loads"] + [self._clock() - entry["at"]])[-WINDOW:]

    def gap(self, state):
        if not state["loads"]:
            return self.settings.min_gap_s
        wanted = statistics.median(state["loads"]) / self.settings.pages_at_once
        return min(self.settings.max_gap_s, max(self.settings.min_gap_s, wanted))

    # ------------------------------------------------------------------ the network

    def _dns_fine(self, host):
        """Checks the app's address still resolves, at most every DNS_EVERY_S across the job. Records a pause."""
        with self._state() as state:
            checked = state["dns"].get(host)
            if checked and self._clock() - checked["at"] < DNS_EVERY_S:
                return checked["ok"]
            state["dns"][host] = {"at": self._clock(), "ok": checked["ok"] if checked else True}  # one checker
        ok = self._resolve(host)
        with self._state() as state:
            state["dns"][host] = {"at": self._clock(), "ok": ok}
            if not ok and not state["paused"]:
                state["paused"] = {"since": self._clock(), "why": f"{host} doesn't resolve (the network's DNS "
                                                                   f"stopped answering)"}
            elif ok and state["paused"]:
                state["paused"] = None
        return ok

    def status(self):
        """-> {"open": n, "gap_s": s, "paused": {since, why} | None} for people watching the job."""
        with self._state() as state:
            return {"open": len(state["open"]), "gap_s": round(self.gap(state), 2), "paused": state["paused"]}

    # ------------------------------------------------------------------ shared state

    def _drop_stale(self, state, now):
        """A page whose process died, or that never finished, gives its place back."""
        for ticket, entry in list(state["open"].items()):
            if now - entry["at"] > self.settings.page_timeout_s or not _alive(entry["pid"]):
                del state["open"][ticket]

    @contextmanager
    def _state(self):
        lock = self.dir / "lock"
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except PermissionError:  # Windows: another process is deleting the lock this instant
                time.sleep(0.01)
                continue
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.01)
        try:
            path = self.dir / "state.json"
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {"open": {}, "loads": [], "last_start": 0, "dns": {}, "paused": None}
            yield state
            tmp = self.dir / f"state.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)


def _alive(pid):
    return pid == os.getpid() or process_alive(pid)


# ---------------------------------------------------------------------------------------------------- the job

_shared = None


def shared(run=None):
    """The job's pace: made once by the job (before its first worker), found by its workers through
    EVERYROLE_PACE_DIR, so every process of a job shares one."""
    global _shared
    if _shared is None:
        directory = os.environ.get(ENV)
        if not directory:
            directory = tempfile.mkdtemp(prefix="everyrole-pace-")
            os.environ[ENV] = directory  # every worker started from here on shares it
        _shared = Pace(directory, Settings.of(run))
    return _shared


def watch(pace, log, every_s=2):
    """Says in the job's log when the network stops answering and when it is back (a background thread)."""
    def loop():
        was = None
        while True:
            paused = pace.status()["paused"]
            if paused and not was:
                log(f"· paused: {paused['why']}; waiting for the network, nothing new starts")
            elif was and not paused:
                log("· the network answers again: continuing")
            was = paused
            time.sleep(every_s)

    threading.Thread(target=loop, daemon=True).start()
