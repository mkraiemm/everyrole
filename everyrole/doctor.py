"""`everyrole doctor`: is this machine ready to test? One line per check, with the fix when it is not.

The provider check only asks whether the address answers. The model check makes a tiny call to each configured
model (a few tokens), so a model the gateway no longer offers is found before a run fails on it.
"""

import os
import shutil
import sys
from dataclasses import dataclass

import httpx

from . import model_check
from .chrome import ChromeError, find_chrome

REQUIRED_KEYS = ("TYPESAFE_API_KEY",)  # every run
AUTHOR_KEYS = ("TEXT_MODEL_API_KEY", "TEXT_MODEL_BASE_URL", "TEXT_MODEL")  # exploring + writing tests


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def line(self):
        mark = "ok  " if self.ok else ("FAIL" if self.required else "warn")
        return f"{mark}  {self.name}: {self.detail}"


def python_version(version=None):
    version = version or sys.version_info
    ok = tuple(version[:2]) >= (3, 12)
    return Check("Python", ok, f"{version[0]}.{version[1]}" + ("" if ok else " (needs 3.12 or newer)"))


def chrome(environ=None):
    try:
        return Check("Chrome", True, find_chrome(environ))
    except ChromeError as exc:
        return Check("Chrome", False, str(exc))


def ffmpeg(which=shutil.which):
    found = which("ffmpeg")
    return Check("ffmpeg (replay videos)", bool(found), found or "not found: install ffmpeg to get replay videos",
                 required=False)


def keys(environ=None):
    env = os.environ if environ is None else environ
    checks = []
    for names, what, required in ((REQUIRED_KEYS, "running tests", True),
                                  (AUTHOR_KEYS, "exploring and writing tests", False)):
        missing = [k for k in names if not env.get(k)]
        detail = "set" if not missing else f"missing {', '.join(missing)} (put them in your .env file)"
        checks.append(Check(f"Model keys for {what}", not missing, detail, required=required))
    return checks


def provider(url, get=None):
    get = get or (lambda address: httpx.get(address, timeout=10))
    try:
        get(url)  # any HTTP answer proves the address is reachable; nothing is spent
        return Check("Model provider reachable", True, url)
    except httpx.HTTPError as exc:
        return Check("Model provider reachable", False, f"{url}: {type(exc).__name__}: {exc}")


def models(environ=None, probe=model_check.probe):
    """One line per configured model (each job's main model and its two backups, model_check.SETTINGS): does it
    answer?"""
    env = os.environ if environ is None else environ
    base, key = env.get("TEXT_MODEL_BASE_URL"), env.get("TEXT_MODEL_API_KEY")
    if not base or not key:
        return []  # keys() already says what is missing
    checks = []
    for model, used_for in model_check.slots(env).items():
        answer = probe(base, key, model)
        detail = answer.detail + ("; the gateway doesn't offer it (any more): choose another model" if answer.missing
                                  else "")
        checks.append(Check(f"Model {model} ({'; '.join(used_for)})", answer.ok, detail, required=False))
    return checks


def run(environ=None, *, provider_url=None, get=None, which=shutil.which, version=None, probe=None):
    """-> list of Checks. With `probe` (model_check.probe), each configured model gets a tiny call too."""
    checks = [python_version(version), chrome(environ), ffmpeg(which), *keys(environ)]
    if provider_url:
        checks.append(provider(provider_url, get))
    if probe:
        checks.extend(models(environ, probe))
    return checks


def report(checks):
    lines = [c.line() for c in checks]
    blocking = [c for c in checks if c.required and not c.ok]
    lines.append("")
    lines.append("Ready to test." if not blocking else f"Not ready: fix the {len(blocking)} FAIL line(s) above.")
    return "\n".join(lines), (0 if not blocking else 1)
