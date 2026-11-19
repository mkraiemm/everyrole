"""A disposable headless Chrome with a DevTools endpoint, or an existing one given by URL.

`Chrome.ensure()` heals the browser before a retry: it waits for DevTools to answer and, if EveryRole launched
this Chrome itself and it stopped answering, restarts it on the same port. A Chrome given by URL belongs to
someone else and is never restarted.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from . import limits

EXECUTABLES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge")


class ChromeError(RuntimeError):
    pass


def find_chrome(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("CHROME_PATH"):
        return environ["CHROME_PATH"]
    for name in EXECUTABLES:
        if found := shutil.which(name):
            return found
    # Standard install locations, derived from the OS's own environment variables.
    roots = [environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if environ.get(k)]
    for root in roots:
        candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if candidate.is_file():
            return str(candidate)
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.is_file():
        return str(mac)
    raise ChromeError("Chrome not found. Install Chrome/Chromium or set CHROME_PATH.")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_devtools(url, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/json/version", timeout=2) as response:
                return json.load(response)
        except OSError:
            time.sleep(0.1)
    raise ChromeError(f"Chrome DevTools did not answer at {url} within {timeout_s}s")


class Chrome:
    """A Chrome that EveryRole talks to. `url` is its DevTools HTTP address."""

    def __init__(self, cdp_url=None, *, startup_timeout_s=20, window="1400,900", heal_wait_s=30, headless=True):
        self.startup_timeout_s = startup_timeout_s
        self.heal_wait_s = heal_wait_s
        self.window = window
        self.headless = headless  # False: a normal window a person can use directly (signing in by hand)
        self.owned = not cdp_url
        self.url = cdp_url or f"http://127.0.0.1:{_free_port()}"
        self._process = None
        self._profile = None

    def start(self):
        if self.owned:
            self._launch()
        wait_for_devtools(self.url, self.startup_timeout_s)
        return self

    def ensure(self):
        """Make sure DevTools answers. Restart our own Chrome if it does not."""
        try:
            wait_for_devtools(self.url, self.heal_wait_s)
        except ChromeError:
            if not self.owned:
                raise
            self.stop()
            self._launch()
            wait_for_devtools(self.url, self.startup_timeout_s)

    def stop(self):
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None

    def _launch(self):
        port = self.url.rsplit(":", 1)[1]
        self._profile = tempfile.mkdtemp(prefix="everyrole-chrome-")
        args = [
            find_chrome(),
            *(["--headless=new"] if self.headless else []),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self._profile}",
            "--no-first-run",
            "--no-default-browser-check",
            # names are looked up through the system's resolver and its shared cache: Chrome's own DNS client sent
            # every lookup of every private session to the router as a separate TCP connection (500 in 2 minutes),
            # and a home router's DNS gave up
            "--disable-features=AsyncDns,DnsOverHttps",
            f"--window-size={self.window}",
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")  # containers commonly run as root, where the sandbox cannot start
        args += egress_args(os.environ)
        self._process = limits.active().launch(args + ["about:blank"], stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL)  # inside the job's caps, if any


EGRESS_PROXY_ENV = "EVERYROLE_EGRESS_PROXY"  # an egress proxy every request goes through, when set: host:port


def egress_proxy(environ=None):
    """The egress proxy as a URL ("http://127.0.0.1:port") for a job's own requests (httpx's `proxy=`), or None
    when none is set: then any address is reached, localhost included."""
    proxy = (os.environ if environ is None else environ).get(EGRESS_PROXY_ENV, "")
    return f"http://{proxy}" if proxy else None


def egress_args(environ):
    """With an egress proxy set, every request of the browser goes through it: loopback too (Chrome sends it around
    a proxy by default), and no WebRTC traffic around it. [] without one."""
    proxy = environ.get(EGRESS_PROXY_ENV, "")
    if not proxy:
        return []
    return [f"--proxy-server=http://{proxy}", "--proxy-bypass-list=<-loopback>",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]


def close_tabs(cdp_url, target_ids):
    """Closes these tabs if they are still open (a worker that hung or crashed leaves its tabs behind).
    -> how many were still open."""
    closed = 0
    for target in target_ids:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/close/{target}", timeout=5) as response:
                closed += response.status == 200
        except OSError:
            pass  # already closed: the usual case
    return closed


@contextmanager
def chrome_session(cdp_url=None, **options):
    """Yield a started `Chrome`: the one at `cdp_url`, otherwise a throwaway headless Chrome."""
    chrome = Chrome(cdp_url, **options)
    try:
        yield chrome.start()
    finally:
        chrome.stop()
