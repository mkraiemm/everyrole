"""Run a suite: sign in once per persona, then all flows and access checks in parallel.

Phase 0  (optional) the site answers at all; if it is down, that is the only failure reported
Phase 1  login@persona for every persona that signs in -> sessions in a private temp dir
Phase 2  every flow (restoring its persona's session) + every persona's access rules
One worker process (and harness daemon) per unit, all against one Chrome.

Every unit that does not pass is tried once more. Before retrying a machine problem the browser is healed
(see chrome.py). Passing on the second try is reported as a pass with a warning, so flakiness stays visible
without turning a pull request red.
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import daemons, usage, verdict
from .access import expected_rules
from .chrome import close_tabs
from .limits import active as active_limits
from .progress import Progress
from .session import private_dir
from .spec import site_of
from .worker import RESULT_PREFIX

# the engine writes every tab a worker opens to this file (jev_ultrafast.browser.TABS_FILE_ENV); named here, not
# imported, so this process never loads the browser package
TABS_FILE_ENV = "JEV_TABS_FILE"


def site_checker():
    """The real pre-check: the site must answer; the model provider's address tells "down" from "no network"."""
    from jev_ultrafast.model import typesafe_url

    from .reachability import check

    return lambda base_url: check(base_url, typesafe_url())


class SuiteRunner:
    def __init__(self, *, parallel, cdp_url, output_dir, env_file, base_url=None, site=None, config_path=None,
                 python=sys.executable, grace_s=30, run_id=None, videos=False, chrome=None, site_check=None,
                 limits=None):
        self.parallel = max(1, parallel)
        # machine time: how long workers held a browser (each worker's own time, once it had room to run)
        self.machine = {"workers": 0, "browser_seconds": 0.0}
        self._machine_lock = threading.Lock()
        self.cdp_url = cdp_url
        self.site = site_of(base_url, site)  # every address of the app in the environment being tested
        self.base_url = self.site.main_url
        self.output_dir = output_dir
        self.env_file = env_file
        self.config_path = config_path
        self.python = python
        self.grace_s = grace_s
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.videos = videos
        self.chrome = chrome  # optional: heals the browser before a machine problem is retried
        self.site_check = site_check  # optional: (base_url) -> failing result, or None when the site answers
        self.limits = limits or active_limits()  # a worker starts only when it fits under the memory and CPU caps

    # ------------------------------------------------------------------ orchestration

    def run(self, flows, config):
        started = time.monotonic()
        access_personas = [p for p in config.personas if expected_rules(config, p)]
        needed = {f.persona for f in flows if f.persona} | set(access_personas)
        signing_in = sorted(p for p in needed if config.personas[p].login)
        # the run's live state for people watching it (progress.json next to the report)
        progress = Progress(Path(self.output_dir) / "progress.json" if self.output_dir else None)
        access_batches = self._access_batches(config, access_personas)
        progress.plan([*([{"key": "site", "kind": "site", "persona": ""}] if self.site_check else []),
                       *({"key": f"login@{p}", "kind": "signin", "persona": p} for p in signing_in),
                       *({"key": f.name, "kind": "test", "persona": f.persona} for f in flows),
                       *({"key": key, "kind": "access", "persona": p} for key, p, _ in access_batches)])
        try:
            return self._run(flows, config, started, progress, access_batches, signing_in)
        finally:
            progress.done()

    @staticmethod
    def _access_batches(config, personas):
        """[(unit key, persona, pages)]: each kind of user's rules in batches of `run.access_batch` pages, so a
        kind of user with many pages is checked by several workers at once instead of one opening every page."""
        size = config.run["access_batch"]
        batches = []
        for persona in personas:
            pages = [path for path, _ in expected_rules(config, persona)]
            chunks = [pages[i:i + size] for i in range(0, len(pages), size)]
            for n, chunk in enumerate(chunks, 1):
                key = f"access@{persona}" + (f" {n}/{len(chunks)}" if len(chunks) > 1 else "")
                batches.append((key, persona, chunk))
        return batches

    def _run(self, flows, config, started, progress, access_batches, signing_in):
        down = progress.track("site", lambda: self.site_check(self.base_url)) if self.site_check else None
        if down:
            return self._site_down(down, flows, config, started)
        login_timeout_s = config.login["timeout_s"] if signing_in else 0
        with private_dir() as sessions, ThreadPoolExecutor(max_workers=self.parallel) as pool:
            session_of = {p: sessions / f"{p}.json" for p in signing_in}
            logins = list(pool.map(lambda p: progress.track(
                f"login@{p}", lambda: self._login(p, session_of[p], login_timeout_s)), signing_in))
            signed_in = {p for p, r in zip(signing_in, logins) if r.get("passed")}
            self._logins = dict(zip(signing_in, logins))

            def session_for(persona):
                return str(session_of[persona]) if persona in signed_in else None

            def blocked_by_login(persona):
                return persona in session_of and persona not in signed_in

            flow_jobs = [
                (lambda f=f: progress.track(f.name, lambda: (
                    self._skipped(f.name, f.persona, f.source, self._login_reason(f.persona))
                    if blocked_by_login(f.persona) else self._flow(f, session_for(f.persona)))))
                for f in flows
            ]
            access_jobs = [
                (lambda key=key, p=p, pages=pages: progress.track(key, lambda: (
                    self._access_skipped(config, p, self._login_reason(p), pages) if blocked_by_login(p)
                    else self._access(config, p, session_for(p), pages))))
                for key, p, pages in access_batches
            ]
            outcomes = list(pool.map(lambda job: job(), flow_jobs + access_jobs))
        flow_results = outcomes[: len(flow_jobs)]
        access_results = [row for rows in outcomes[len(flow_jobs):] for row in rows]
        return {
            "results": logins + flow_results,
            "access": access_results,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "run_id": self.run_id,
            "usage": {**usage.merge(*(r.get("usage") for r in logins + flow_results)), "machine": dict(self.machine)},
        }

    # ------------------------------------------------------------------ units

    def _record(self, name):
        if not self.videos:
            return []
        return ["--record-dir", str(Path(self.output_dir).resolve() / "videos" / name.replace("@", "-"))]

    def _login(self, persona, session_path, timeout_s):
        """Sign in once per persona. A single flaky sign-in must not turn every test of that user type into
        "not run", so it is tried twice like every other unit."""
        args = ["login", "--persona", persona, "--save-session", str(session_path), *self._record(f"login@{persona}")]
        return self._twice(lambda: self._spawn(args, name=f"login@{persona}", timeout_s=timeout_s))

    def _login_reason(self, persona):
        login = getattr(self, "_logins", {}).get(persona, {})
        if verdict.cause_of(login) == verdict.SIGNIN:
            return f"not run: {persona!r} needs you to sign in yourself (security check or expired sign-in)"
        if verdict.of(login) == verdict.NOT_CHECKED:
            return f"not run: sign-in as {persona!r} could not be checked (machine problem)"
        return f"not run: sign-in as {persona!r} failed"

    def _flow(self, flow, session):
        args = ["flow", "--flow-file", flow.source, "--flow-name", flow.name, *self._record(flow.name)]
        if flow.persona:
            args += ["--persona", flow.persona]
        if session:
            args += ["--session", session]
        return self._twice(lambda: self._spawn(args, name=flow.name, timeout_s=flow.timeout_s, source=flow.source))

    def _access(self, config, persona, session, pages):
        """One batch of a kind of user's pages. Rows are re-checked one by one inside the worker; here only a
        worker that died is retried."""
        args = ["access", "--persona", persona, *(a for page in pages for a in ("--path", page))]
        args += ["--session", session] if session else []
        # opening each page (the engine waits up to its load timeout) + the settle window
        timeout_s = config.access_settle_s + 20 * max(1, len(pages))
        rows = self._spawn(args, name=f"access@{persona}", timeout_s=timeout_s)
        if not isinstance(rows, list):
            self._heal()
            rows = self._spawn(args, name=f"access@{persona}", timeout_s=timeout_s)
        if isinstance(rows, list):
            return rows
        problem = rows.get("error") or rows.get("status", "worker failed")
        return [{"path": path, "persona": persona, "expected": expected, "observed": "error", "passed": False,
                 "url": "", "detail": problem, "cause": verdict.cause_of(rows)}
                for path, expected in expected_rules(config, persona) if path in pages]

    # ------------------------------------------------------------------ retries

    def _twice(self, attempt):
        first = attempt()
        cause = verdict.cause_of(first)
        if not cause or cause in (verdict.SETUP, verdict.SKIPPED, verdict.SIGNIN):
            return first  # retrying can't help: the setup is wrong, or a person has to sign in
        if cause == verdict.MACHINE:
            self._heal()
        second = attempt()
        second["attempts"] = 2
        why = (first.get("error") or _first_failed_check(first) or first.get("status", "failed"))[:160]
        tried = "could not be checked" if cause == verdict.MACHINE else "failed"
        second.setdefault("warnings", []).append(f"first try {tried} ({why}); tried again")
        return second

    def _heal(self):
        if self.chrome is None:
            return
        try:
            self.chrome.ensure()
        except Exception as exc:  # the retry will then report a machine problem, never an app failure
            print(f"warning: browser could not be restored: {exc}", file=sys.stderr)

    def _spawn(self, args, *, name, timeout_s, source=""):
        with private_dir() as tabs_dir:
            tabs = Path(tabs_dir) / "tabs"
            try:
                return self._spawn_worker(args, name=name, timeout_s=timeout_s, source=source, tabs=tabs)
            finally:  # a worker that hung or crashed must not leave its tabs open in the shared browser
                if tabs.is_file():
                    close_tabs(self.cdp_url, tabs.read_text(encoding="utf-8").split())

    def _spawn_worker(self, args, *, name, timeout_s, source, tabs):
        worker = f"sw-{self.run_id}-{name}".replace("@", "-")
        env = {**os.environ, "BU_CDP_URL": self.cdp_url, "BU_NAME": worker, **daemons.for_worker(worker),
               TABS_FILE_ENV: str(tabs)}
        command = [
            self.python, "-m", "everyrole.worker", *args,
            *self.site.as_args(), "--run-id", self.run_id,
            "--output", str(self.output_dir), "--env-file", str(self.env_file),
        ] + (["--config", str(self.config_path)] if self.config_path else [])
        with self.limits.slot():
            started = time.monotonic()  # the worker's own time starts once it has room to run
            try:
                completed = self.limits.run(command, env=env, text=True, encoding="utf-8",
                                            timeout=timeout_s + self.grace_s)
            except subprocess.TimeoutExpired:
                completed = None
            with self._machine_lock:
                self.machine["workers"] += 1
                self.machine["browser_seconds"] += time.monotonic() - started
        if completed is None or not any(line.startswith(RESULT_PREFIX) for line in completed.stdout.splitlines()):
            # stopped or crashed before it could stop its own browser-control daemon: stop it here
            daemons.stop(daemons.claim(), name=worker)
        if completed is None:
            # the run has its own budget; a worker past it has hung
            return self._failure(name, "error", f"worker hung (no answer after {timeout_s + self.grace_s}s)",
                                 started, source)
        for line in reversed(completed.stdout.splitlines()):
            if line.startswith(RESULT_PREFIX):
                return json.loads(line[len(RESULT_PREFIX):])
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-5:]
        return self._failure(name, "error", "worker crashed: " + " | ".join(tail), started, source)

    # ------------------------------------------------------------------ placeholders

    @staticmethod
    def _failure(name, status, error, started, source=""):
        return {
            "name": name, "status": status, "passed": False, "error": error, "source": source,
            "cause": verdict.MACHINE, "duration_ms": round((time.monotonic() - started) * 1000),
            "expectations": [], "steps": [], "warnings": [],
        }

    @staticmethod
    def _skipped(name, persona, source, reason):
        return {
            "name": name, "status": "skipped", "passed": False, "persona": persona, "source": source,
            "cause": verdict.SKIPPED, "error": reason, "duration_ms": 0,
            "expectations": [], "steps": [], "warnings": [],
        }

    @staticmethod
    def _access_skipped(config, persona, reason, pages=None):
        return [
            {"path": path, "persona": persona, "expected": expected, "observed": "skipped", "passed": False,
             "url": "", "detail": reason, "cause": verdict.SKIPPED}
            for path, expected in expected_rules(config, persona) if pages is None or path in pages
        ]

    def _site_down(self, down, flows, config, started):
        reason = ("not run: the site did not answer" if down["cause"] == verdict.APP
                  else "not run: no network on this machine")
        personas = [p for p in config.personas if expected_rules(config, p)]
        return {
            "results": [down] + [self._skipped(f.name, f.persona, f.source, reason) for f in flows],
            "access": [row for p in personas for row in self._access_skipped(config, p, reason)],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "run_id": self.run_id,
        }


def _first_failed_check(result):
    failed = [e for e in result.get("expectations", []) if not e.get("passed")]
    return f"{failed[0]['kind']} “{failed[0]['expected']}”: {failed[0]['detail']}" if failed else ""
