"""Run a suite: sign in once per persona, then all flows and access checks in parallel.

Phase 0  (optional) the site answers at all; if it is down, that is the only failure reported
Phase 1  login@persona for every persona that signs in -> sessions in a private temp dir
Phase 2  every flow (restoring its persona's session) + every persona's access rules
One worker process (and harness daemon) per unit, all against one Chrome.

Every unit that does not pass is tried once more. Before retrying a machine problem the browser is healed
(see chrome.py). Passing on the second try is reported as a pass with a warning, so flakiness stays visible
without turning a pull request red (a test that keeps failing and passing is set aside: flaky.py).

A session can expire during the run: a signed-in test (or page check) that ends on the app's sign-in page or
refusal signal says so (`signed_out`, see signin.SignedOut). Its user type is then signed in again, once per run
however many tests noticed, and each of those tests runs once more with the new session. If signing in again
fails, they are "couldn't check" (a sign-in problem), never app failures. Nothing is ever retried in a loop.
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

from . import account_book, daemons, heartbeat, importance, mailbox, password_change, usage, verdict, video
from .access import expected_rules
from .chrome import close_tabs
from .limits import active as active_limits
from .progress import Progress
from .session import private_dir
from .spec import schema, site_of
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
            suite = self._run(flows, config, started, progress, access_batches, signing_in)
        finally:
            progress.done()
        stamp_importance(suite["results"], flows)
        return suite

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
        # an app whose kinds of user all sign in alone (a login link, a sign-in by hand) has no `login` recipe
        login_timeout_s = (config.login.get("timeout_s") or schema("config.schema.json")["properties"]["login"]
                           ["properties"]["timeout_s"]["default"]) if signing_in else 0
        self._again, self._again_locks, self._again_lock = {}, {}, threading.Lock()
        with private_dir() as sessions, ThreadPoolExecutor(max_workers=self.parallel) as pool:
            session_of = {p: sessions / f"{p}.json" for p in signing_in}
            logins = list(pool.map(lambda p: progress.track(
                f"login@{p}", lambda: self._login(p, session_of[p], login_timeout_s)), signing_in))
            signed_in = {p for p, r in zip(signing_in, logins) if r.get("passed")}
            self._logins = dict(zip(signing_in, logins))

            def session_for(persona):
                again = self._again.get(persona)
                if again and again[0]:  # signed in again during this run: that session is the one that works
                    return again[0]
                return str(session_of[persona]) if persona in signed_in else None

            def sign_in_again(persona):
                return self._sign_in_again(persona, sessions / f"{persona}-again.json", login_timeout_s)

            def blocked_by_login(persona):
                return persona in session_of and persona not in signed_in

            def one(f):
                if f.not_run:
                    return self._skipped(f.name, f.persona, f.source, f.not_run, cause=verdict.HELD)
                if blocked_by_login(f.persona):
                    return self._skipped(f.name, f.persona, f.source, self._login_reason(f.persona))
                session = session_for(f.persona)
                result = self._flow(f, session)
                if result.get("signed_out") and f.persona in session_of:
                    return self._flow_signed_out(f, result, session, sign_in_again(f.persona))
                return result

            # a test that changes its user type's password runs after the others, one at a time (below)
            changing = [f for f in flows if f.changes_password and not f.not_run]
            flow_jobs = [(lambda f=f: progress.track(f.name, lambda: one(f))) for f in flows if f not in changing]
            access_jobs = [
                (lambda key=key, p=p, pages=pages: progress.track(key, lambda: (
                    self._access_skipped(config, p, self._login_reason(p), pages) if blocked_by_login(p)
                    else self._access_signed_in(config, p, session_for(p), pages, sign_in_again))))
                for key, p, pages in access_batches
            ]
            outcomes = list(pool.map(lambda job: job(), flow_jobs + access_jobs))
            by_name = {r.get("name"): r for r in outcomes[: len(flow_jobs)]}
            for f in changing:
                by_name[f.name] = progress.track(f.name, lambda f=f: self._change_password(
                    f, one(f), config, sessions, login_timeout_s))
            flow_results = [by_name[f.name] for f in flows if f.name in by_name]
            access_results = self._recheck_quietly(
                config, [row for rows in outcomes[len(flow_jobs):] for row in rows], session_for, blocked_by_login)
        again = [signin for _, signin in self._again.values()]  # signing in again used the models too
        return {
            "results": logins + flow_results,
            "access": access_results,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "run_id": self.run_id,
            "usage": {**usage.merge(*(r.get("usage") for r in logins + flow_results + again)),
                      "machine": dict(self.machine)},
        }

    # ------------------------------------------------------------------ units

    def _record(self, name):
        if not self.videos:
            return []
        return ["--record-dir", str(Path(self.output_dir).resolve() / "videos" / name.replace("@", "-"))]

    def _login(self, persona, session_path, timeout_s, env_extra=None):
        """Sign in once per persona. A single flaky sign-in must not turn every test of that user type into
        "not run", so it is tried twice like every other unit."""
        args = ["login", "--persona", persona, "--save-session", str(session_path), *self._record(f"login@{persona}")]
        extra = {"env_extra": env_extra} if env_extra else {}
        attempt = lambda: self._spawn(args, name=f"login@{persona}", timeout_s=timeout_s, **extra)  # noqa: E731
        result = self._twice(attempt)
        if verdict.cause_of(result) == verdict.MACHINE:  # every test of this user type depends on it: once more
            self._heal()
            third = attempt()
            third["attempts"] = 3
            third.setdefault("warnings", []).extend(result.get("warnings", []))
            third["warnings"].append(f"second try could not be checked ({(result.get('error') or '')[:120]}); "
                                     "tried a third time")
            return third
        return result

    def _change_password(self, flow, result, config, sessions, timeout_s):
        """After a test that changed its user type's password: sign in with the new password, else with the old
        one, and say which works (`signin_after`, see password_change.py). The test passes only if the new one
        does; whatever happened, the host keeps the password that works."""
        if result.get("status") == "skipped":
            return result
        persona = flow.changes_password
        secret = password_change.password_secret(config.persona(persona, "run").values)
        new = os.environ.get(password_change.env_name(persona), "")
        after, unsure = password_change.NEITHER, False
        for which, extra in ((password_change.NEW, {secret: new}), (password_change.OLD, {})):
            signin = self._login(persona, sessions / f"{persona}-after-{which}.json", timeout_s, env_extra=extra)
            if signin.get("passed"):
                after = which
                break
            unsure = unsure or verdict.cause_of(signin) in (verdict.MACHINE, verdict.SIGNIN, verdict.UNAVAILABLE)
        result = {**result, "signin_after": after if (after != password_change.NEITHER or not unsure) else "unknown"}
        if after == password_change.NEW and os.environ.get(password_change.CHANGE_BACK_ENV):
            return self._change_back(flow, result, secret, new, sessions, timeout_s)
        if after == password_change.NEW:
            return {**result, "passed": True, "status": "passed", "cause": "", "error": ""}
        if verdict.cause_of(result) == verdict.MACHINE and after == password_change.OLD:
            return result  # it stopped before saving (a machine problem): nothing changed, nothing to judge
        if after == password_change.OLD:
            error = "The password didn't change: signing in with the new password failed, and the old one still works."
        elif unsure:
            return {**result, "passed": False, "status": "error", "cause": verdict.MACHINE,
                    "error": "Couldn't check which password works now (a problem on our side). EveryRole kept both: "
                             "see this user type in the app's settings."}
        else:
            error = ("Neither the new nor the old password signs in any more. EveryRole kept both: see this user type "
                     "in the app's settings.")
        return {**result, "passed": False, "status": "failed_expect", "cause": verdict.APP, "error": error}

    def _change_back(self, flow, result, secret, new, sessions, timeout_s):
        """The app has other environments sharing this test account: once the new password is proven, the same
        test changes it back (the new one typed as the current one), then the old one must sign in again. If that
        fails, the new one is the password now (the host keeps it and says so)."""
        persona = flow.changes_password
        old = os.environ.get(secret, "")
        signed_in = sessions / f"{persona}-after-{password_change.NEW}.json"
        self._flow(flow, str(signed_in) if signed_in.is_file() else None,
                   env_extra={secret: new, password_change.env_name(persona): old})
        back = self._login(persona, sessions / f"{persona}-after-back.json", timeout_s)  # the old password
        passed = {**result, "passed": True, "status": "passed", "cause": "", "error": ""}
        if back.get("passed"):
            return {**passed, "signin_after": password_change.OLD, "changed_back": True}
        warning = ("the new password works, but changing it back failed: the new one is the password now, in every "
                   "environment EveryRole signs in to (sign in to the others with it, or save their password)")
        return {**passed, "signin_after": password_change.NEW, "warnings": [*result.get("warnings", []), warning]}

    def _login_reason(self, persona):
        login = getattr(self, "_logins", {}).get(persona, {})
        if verdict.cause_of(login) == verdict.SIGNIN:
            return f"not run: {persona!r} needs you to sign in yourself (security check or expired sign-in)"
        if verdict.of(login) == verdict.NOT_CHECKED:
            return f"not run: sign-in as {persona!r} could not be checked (machine problem)"
        return f"not run: sign-in as {persona!r} failed"

    def _flow(self, flow, session, env_extra=None, once=False):
        args = ["flow", "--flow-file", flow.source, "--flow-name", flow.name, *self._record(flow.name)]
        if flow.persona:
            args += ["--persona", flow.persona]
        if session:
            args += ["--session", session]

        budget = flow.budget_s  # each step's time plus the waits its steps are allowed

        tries = []

        def attempt():
            # each try knows which one it is (mailbox.py); with an account book, every try shares the role's one
            # account: once a try reached the sign-up, the next one logs in with it (account_book.py)
            tries.append(1)
            env = {**(env_extra or {}), mailbox.ATTEMPT_ENV: str(len(tries))}
            # a journey frozen on one call is stopped once it showed no sign of life for longer than a step may take
            result = self._spawn(args, name=flow.name, timeout_s=budget, source=flow.source, env_extra=env,
                                 quiet_s=max(flow.timeout_s, video.ENCODE_TIMEOUT_S) + self.grace_s)
            reached_sign_up(flow, result)
            return result

        return attempt() if once else self._twice(attempt)

    # ------------------------------------------------------------------ signed out mid-run

    def _sign_in_again(self, persona, session_path, timeout_s):
        """Sign this user type in again, at most once per run, however many tests found it signed out (the others
        wait for that one sign-in). -> (session path, or None when it failed; the sign-in's result)."""
        with self._again_lock:
            lock = self._again_locks.setdefault(persona, threading.Lock())
        with lock:
            if persona not in self._again:
                signin = self._login(persona, session_path, timeout_s)
                self._again[persona] = (str(session_path) if signin.get("passed") else None, signin)
            return self._again[persona]

    def _flow_signed_out(self, flow, result, used, again):
        """A test that ended signed out: run it once more with the session of signing in again. If that sign-in
        failed it couldn't be checked; if it already ran with that session, it is not run again (never a loop)."""
        session, signin = again
        why = result["signed_out"]
        if session is None:
            return {**result, "passed": False, "status": "error", "cause": _signin_cause(signin),
                    "error": f"couldn't check: signed out mid-run ({why}), and signing in again failed "
                             f"({_signin_error(signin)})"}
        if session == used:
            return {**result, "warnings": [*result.get("warnings", []),
                                           f"signed out mid-run ({why}) with a session just signed in again: not "
                                           "run again"]}
        rerun = self._flow(flow, session, once=True)
        rerun["attempts"] = result.get("attempts", 1) + 1
        rerun.setdefault("warnings", []).append(f"signed out mid-run ({why}); signed in again and ran it once more")
        return rerun

    def _access_signed_in(self, config, persona, session, pages, sign_in_again):
        """A batch of pages. Those that sent a signed-in user to the sign-in page (its session expired mid-run) are
        checked once more after signing in again, or are "couldn't check" when that sign-in fails."""
        rows = self._access(config, persona, session, pages)
        out = {row["path"]: row for row in rows if row.get("signed_out")}
        if not out or session is None:
            return rows
        fresh, signin = sign_in_again(persona)
        if fresh is None:
            failed = {path: {**row, "passed": False, "observed": "error", "cause": _signin_cause(signin),
                             "detail": f"couldn't check: signed out mid-run ({row['signed_out']}), and signing in "
                                       f"again failed ({_signin_error(signin)})"} for path, row in out.items()}
            return [failed.get(row["path"], row) for row in rows]
        if fresh == session:
            return rows  # it already used the new session: never a loop
        later = {row["path"]: row for row in self._access(config, persona, fresh, list(out))}
        for path, row in later.items():
            row["detail"] = f"{row.get('detail', '')} (checked again after signing in again: {out[path]['signed_out']})"
        return [later.get(row["path"], row) for row in rows]

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

    QUIET_RECHECKS = 2  # pages checked again at once after the rest: few enough that the machine is quiet

    def _recheck_quietly(self, config, rows, session_for, blocked_by_login):
        """Pages that couldn't be judged while every batch ran at once (a busy machine: heavy pages don't answer
        in time) are checked again after all the rest, QUIET_RECHECKS at a time. A page that answers now takes
        that answer; one that still can't be judged stays "couldn't check", with both tries in its detail."""
        def doubtful(row):
            unsure = not row.get("passed") and row.get("observed") in ("unsure", "error")
            return unsure and not blocked_by_login(row.get("persona")) and not row.get("signed_out")

        def again(row):
            later = next((r for r in self._access(config, row["persona"], session_for(row["persona"]), [row["path"]])
                          if r.get("path") == row["path"]), None)
            if later is None:
                return row
            first = row.get("detail", "")
            later["detail"] = (f"{later.get('detail', '')} (also while the others ran: {first})"
                               if later.get("observed") in ("unsure", "error") else
                               f"{later.get('detail', '')} (checked again once the others were done; first: {first})")
            return later

        doubts = [i for i, row in enumerate(rows) if doubtful(row)]
        if not doubts:
            return rows
        with ThreadPoolExecutor(max_workers=self.QUIET_RECHECKS) as pool:
            answers = dict(zip(doubts, pool.map(lambda i: again(rows[i]), doubts)))
        return [answers.get(i, row) for i, row in enumerate(rows)]

    # ------------------------------------------------------------------ retries

    def _twice(self, attempt):
        first = attempt()
        cause = verdict.cause_of(first)
        if not cause or cause in (verdict.SETUP, verdict.SKIPPED, verdict.SIGNIN) or first.get("signed_out"):
            # retrying can't help: the setup is wrong, a person has to sign in, or the session expired (the same
            # session would be signed out again: see _flow_signed_out)
            return first
        if cause == verdict.MACHINE:
            self._heal()
        second = attempt()
        second["attempts"] = 2
        second["first_cause"] = cause  # a pass after an app failure is a sign of a flaky test (flaky.py)
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

    def _spawn(self, args, *, name, timeout_s, source="", env_extra=None, quiet_s=None):
        with private_dir() as tabs_dir:
            tabs = Path(tabs_dir) / "tabs"
            try:
                return self._spawn_worker(args, name=name, timeout_s=timeout_s, source=source, tabs=tabs,
                                          env_extra=env_extra, quiet_s=quiet_s)
            finally:  # a worker that hung or crashed must not leave its tabs open in the shared browser
                if tabs.is_file():
                    close_tabs(self.cdp_url, tabs.read_text(encoding="utf-8").split())

    def _spawn_worker(self, args, *, name, timeout_s, source, tabs, env_extra=None, quiet_s=None):
        worker = worker_name(self.run_id, name)
        env = {**os.environ, "BU_CDP_URL": self.cdp_url, "BU_NAME": worker, **daemons.for_worker(worker),
               TABS_FILE_ENV: str(tabs), **(env_extra or {})}  # e.g. the new password to sign in with
        alive = tabs.with_name("heartbeat")  # the worker touches it at every action and wait (heartbeat.py)
        if quiet_s:
            env[heartbeat.ENV] = str(alive)
        command = [
            self.python, "-m", "everyrole.worker", *args,
            *self.site.as_args(), "--run-id", self.run_id,
            "--output", str(self.output_dir), "--env-file", str(self.env_file),
        ] + (["--config", str(self.config_path)] if self.config_path else [])
        with self.limits.slot():
            started = time.monotonic()  # the worker's own time starts once it has room to run
            if quiet_s:
                alive.touch()  # its quiet time counts from its start
            try:
                completed = self.limits.run(command, env=env, text=True, encoding="utf-8",
                                            timeout=timeout_s + self.grace_s,
                                            quiet=(alive, quiet_s) if quiet_s else None)
            except subprocess.TimeoutExpired:
                completed = None
            with self._machine_lock:
                self.machine["workers"] += 1
                self.machine["browser_seconds"] += time.monotonic() - started
        if completed is None or not any(line.startswith(RESULT_PREFIX) for line in completed.stdout.splitlines()):
            # stopped or crashed before it could stop its own browser-control daemon: stop it here
            daemons.stop(daemons.claim(), name=worker)
        if completed is None:
            # past its whole budget, or frozen: no sign of life for longer than a step may take
            ran = round(time.monotonic() - started)
            said = (f"worker hung (no answer after {timeout_s + self.grace_s}s)" if ran >= timeout_s + self.grace_s
                    else f"worker hung (nothing moved for {quiet_s}s; stopped after {ran}s)")
            return self._failure(name, "error", said, started, source)
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
    def _skipped(name, persona, source, reason, cause=verdict.SKIPPED):
        return {
            "name": name, "status": "skipped", "passed": False, "persona": persona, "source": source,
            "cause": cause, "error": reason, "duration_ms": 0,
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
        # what really happened when EveryRole first checked the site (reachability.said), never a guess
        said = down.get("said") or down.get("error", "")
        reason = (f"not run: {said} when EveryRole checked it before testing" if down["cause"] == verdict.APP
                  else "not run: no network on this machine")
        personas = [p for p in config.personas if expected_rules(config, p)]
        return {
            "results": [down] + [self._skipped(f.name, f.persona, f.source, reason) for f in flows],
            "access": [row for p in personas for row in self._access_skipped(config, p, reason)],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "run_id": self.run_id,
        }


def reached_sign_up(flow, result):
    """A try that got to its sign-up step may have made the account: the book says so (account_book.py), so the next
    tries and walks log in with it and the cleanup deletes it."""
    book = account_book.from_env()
    email = flow.values.get("new_account_email") if flow.inbox else None
    if book is None or not email:
        return
    first = next((n for n, s in enumerate(flow.steps, 1) if s.get("sign_up")), None)
    judged = {s.get("step"): s.get("passed") for s in (result or {}).get("steps_judged") or []}
    # judged at or past its sign-up, or stopped (a crash) right after passing the step before it: the cleanup's
    # log-in check tells whether an account was really made
    if first and (any(n >= first for n in judged) or judged.get(first - 1) or first == 1 and result):
        book.reached(email)


def stamp_importance(results, flows):
    """Each result carries its test's level (importance.py), so the report and the alerts can tell a
    broken sign-up from a broken blog page. A user type's sign-in (login@...) is access: critical, as the levels
    define it (a failed one stops every test of that user type)."""
    levels = {f.name: f.importance for f in flows}
    for result in results:
        name = result.get("name", "")
        if name in levels:
            result["importance"] = levels[name]
        elif name.startswith("login@"):
            result["importance"] = importance.CRITICAL
    return results


def _signin_cause(signin):
    """Signing in again failed: on our side (machine), or a sign-in a person must do. Never the test failing."""
    return verdict.MACHINE if verdict.cause_of(signin) == verdict.MACHINE else verdict.SIGNIN


def _signin_error(signin):
    return (signin.get("error") or signin.get("status") or "no reason given")[:200]


def _first_failed_check(result):
    failed = [e for e in result.get("expectations", []) if not e.get("passed")]
    return f"{failed[0]['kind']} “{failed[0]['expected']}”: {failed[0]['detail']}" if failed else ""


WORKER_NAME_MAX = 64  # the browser daemon's limit (browser_harness: [A-Za-z0-9_-]{1,64})


def worker_name(run_id, name):
    """The browser worker's name for a run and a test: its characters and length as the daemon accepts them. A long
    test name (a change's journey) is cut and keeps a short hash, so two names never share a worker."""
    import hashlib
    import re

    full = re.sub(r"[^A-Za-z0-9_-]", "-", f"sw-{run_id}-{name}")
    if len(full) <= WORKER_NAME_MAX:
        return full
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    return f"{full[:WORKER_NAME_MAX - 9]}-{digest}"
