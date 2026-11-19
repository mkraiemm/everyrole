"""`everyrole ci --app <slug>`: run an app's EveryRole tests in your own CI, with logins that never leave it.

The hosted app keeps the tests; your CI keeps the test logins. This command, in a CI job:

  1. asks EveryRole for the run's plan (POST /api/v1/apps/<slug>/ci/runs): the app's kept tests and their config,
     and for each kind of user the two CI variables its login is read from (EVERYROLE_LOGIN_<KIND>_USERNAME and
     _PASSWORD, ci_logins.py). The plan has no secret in it: EveryRole has none for this app.
  2. reads each login from those variables (missing ones are named, and nothing runs), and runs the tests with
     `everyrole run` in this job's own browser. The runner types each login itself; AI models (EveryRole's, through
     its API, so you need no model key) only ever see the value's name: every request body is filtered before it
     leaves this job. Every line this command prints is filtered too.
  3. uploads the report (verdicts, failing steps, pictures, replays: never a login, never the variables) so the run
     shows in EveryRole like any other, "started by the GitLab pipeline"; keeps it in --output (results.json,
     summary.md, junit.xml for the CI's test report); and, with --comment, comments on the merge request.

Exit code: 0 every test passed, 1 a test is broken (the pipeline fails), 2 nothing could be checked or the run
couldn't start (a missing variable, a refused key...).

Environment: EVERYROLE_API_KEY (a workspace API key, as a masked CI variable), EVERYROLE_URL (default
https://everyrole.dev), the logins above, and BASE_URL / EVERYROLE_ENVIRONMENT to test a preview or staging deploy.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import httpx

from .redact import Redactor

DEFAULT_URL = "https://everyrole.dev"
HEARTBEAT_S = 30
SECRET_REF = re.compile(r"\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# what the report keeps (the same kinds of file EveryRole accepts); the account book (new accounts' passwords) never
REPORT_FILES = {".json", ".md", ".xml", ".txt", ".jpg", ".jpeg", ".png", ".webp", ".mp4"}
NEVER_SENT = {"accounts.json"}
TEXT_FILES = {".json", ".md", ".xml", ".txt"}


class CiFailed(Exception):
    """The run couldn't start or finish; the message says why, in plain words."""


def add_parser(sub):
    ci = sub.add_parser("ci", help="Run an app's tests in your own CI: its test logins never leave the CI")
    ci.add_argument("--app", required=True, help="The app's slug in EveryRole (its address: /p/<slug>)")
    ci.add_argument("--url", default=None, help=f"EveryRole's address (default: EVERYROLE_URL or {DEFAULT_URL})")
    ci.add_argument("--base-url", default=None, help="The deploy to test (default: BASE_URL, else the app's own)")
    ci.add_argument("--env", dest="environment", default=None,
                    help="Its environment: production, staging, preview... (default: EVERYROLE_ENVIRONMENT)")
    ci.add_argument("--output", default="everyrole-report", help="Where the report is written (junit.xml...)")
    ci.add_argument("--parallel", type=int, help="Tests at the same time (default: the app's setting)")
    ci.add_argument("--comment", action="store_true",
                    help="Comment the results on the merge request (GitLab: EVERYROLE_GITLAB_TOKEN) or pull request "
                         "(GitHub: GITHUB_TOKEN)")
    return ci


def pipeline(environ):
    """What this CI job is: {"source", "commit", "ref", "pipeline_url"} from GitLab's or GitHub's own variables."""
    if environ.get("GITLAB_CI"):
        return {"source": "gitlab", "commit": environ.get("CI_COMMIT_SHA", ""),
                "ref": environ.get("CI_COMMIT_REF_NAME", ""), "pipeline_url": environ.get("CI_PIPELINE_URL", "")}
    if environ.get("GITHUB_ACTIONS"):
        run = (f"{environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{environ.get('GITHUB_REPOSITORY', '')}"
               f"/actions/runs/{environ.get('GITHUB_RUN_ID', '')}") if environ.get("GITHUB_RUN_ID") else ""
        return {"source": "github", "commit": environ.get("GITHUB_SHA", ""), "ref": environ.get("GITHUB_REF_NAME", ""),
                "pipeline_url": run}
    return {"source": "", "commit": "", "ref": "", "pipeline_url": ""}


def logins(plan, environ):
    """({secret name: value} the run's config reads, [missing CI variables]) for every kind of user, and for any other
    {{secrets.NAME}} a test uses (read from the CI variable NAME itself)."""
    values, missing = {}, []
    for login in plan.get("logins") or []:
        for part in ("username", "password"):
            variable, secret = login[part]["variable"], login[part]["secret"]
            if environ.get(variable):
                values[secret] = environ[variable]
            else:
                missing.append(variable)
    text = json.dumps([plan.get("config"), [f["content"] for f in plan.get("flows") or []]])
    for name in sorted(set(SECRET_REF.findall(text)) - set(values)):
        if name in {s[p]["secret"] for s in plan.get("logins") or [] for p in ("username", "password")}:
            continue
        if environ.get(name):
            values[name] = environ[name]
        else:
            missing.append(name)
    return values, missing


def child_env(plan, key, values, environ):
    """The test run's environment: this job's, without the CI login variables nor EveryRole's key under its own
    name, plus the logins under the names the config reads and EveryRole's models (reached with the API key)."""
    env = {k: v for k, v in environ.items() if not k.startswith("EVERYROLE_LOGIN_") and k != "EVERYROLE_API_KEY"}
    models = plan["models"]
    env.update(values)
    env.update(TYPESAFE_URL=models["decide_url"], TYPESAFE_API_KEY=key, TYPESAFE_MODEL=models["decide_model"],
               TEXT_MODEL_BASE_URL=models["text_base_url"], TEXT_MODEL_API_KEY=key, PYTHONUTF8="1",
               PYTHONUNBUFFERED="1")
    for name in ("TEXT_MODEL", "TEXT_MODEL_BACKUP", "TEXT_MODEL_BACKUP_2", "TEXT_MODEL_REASONING"):
        env.pop(name, None)
    for name, model in zip(("TEXT_MODEL", "TEXT_MODEL_BACKUP", "TEXT_MODEL_BACKUP_2"), models.get("text_models") or []):
        env[name] = model
    if models.get("text_reasoning"):
        env["TEXT_MODEL_REASONING"] = models["text_reasoning"]
    return env


def write_plan(plan, folder):
    """The app's config and kept tests as files the runner reads. -> (config path, flows folder)"""
    folder = Path(folder)
    flows = folder / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    config = folder / plan.get("config_file", "everyrole.config.json")
    config.write_text(json.dumps(plan["config"], indent=2, ensure_ascii=False), encoding="utf-8")
    for flow in plan.get("flows") or []:
        name = Path(flow["file"]).name
        if not name.endswith(".json"):
            continue
        (flows / name).write_text(json.dumps(flow["content"], indent=2, ensure_ascii=False), encoding="utf-8")
    return config, flows


def scrub_report(folder, redactor):
    """Every text file of the report, with any login replaced by its name (the runner already writes none: this is
    the last check before anything leaves the job). -> files changed"""
    changed = []
    for path in Path(folder).rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_FILES:
            text = path.read_text(encoding="utf-8", errors="replace")
            clean = redactor.apply(text)
            if clean != text:
                path.write_text(clean, encoding="utf-8")
                changed.append(path.name)
    return changed


def report_zip(folder, limit_bytes=None):
    """The report folder as one zip (report files only, never the account book). Over the limit, the replays are
    left out first (the verdicts and pictures always go). -> (bytes, left out replays?)"""
    folder = Path(folder)

    def build(videos):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(folder.rglob("*")):
                relative = path.relative_to(folder)
                if (not path.is_file() or path.is_symlink() or len(relative.parts) > 2 or path.name in NEVER_SENT
                        or path.suffix.lower() not in REPORT_FILES or (not videos and path.suffix.lower() == ".mp4")):
                    continue
                archive.write(path, relative.as_posix())
        return buffer.getvalue()

    data = build(videos=True)
    if limit_bytes and len(data) > limit_bytes:
        return build(videos=False), True
    return data, False


class Client:
    def __init__(self, url, key, http=None):
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=120)
        self.headers = {"Authorization": f"Bearer {key}", "User-Agent": "everyrole-ci"}

    def call(self, method, url, **kwargs):
        url = url if url.startswith("http") else self.url + url
        try:
            response = self.http.request(method, url, headers={**self.headers, **kwargs.pop("headers", {})},
                                         **kwargs)
        except httpx.HTTPError as exc:
            raise CiFailed(f"EveryRole at {self.url} didn't answer ({type(exc).__name__}).") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise CiFailed(body.get("message") or f"EveryRole answered HTTP {response.status_code}.")
        return body


def _heartbeat(client, url, stop, stopped, child):
    while not stop.wait(HEARTBEAT_S):
        try:
            if client.call("POST", url).get("stop"):
                stopped.set()
                if child[0] is not None:
                    child[0].terminate()
                return
        except CiFailed:
            continue  # a missed heartbeat is retried; the run's own outcome decides the exit code


def run_tests(plan, env, folder, output, parallel, redactor, client, out=sys.stdout):
    """`everyrole run` on the plan's tests, its output filtered line by line. -> (exit code, stopped by a person?)"""
    from .video import find_ffmpeg

    config, flows = write_plan(plan, folder)
    args = [sys.executable, "-m", "everyrole.cli", "run", str(flows), "--config", str(config), "--base-url",
            plan["base_url"], "--output", str(output), "--env-file", str(Path(folder) / ".env.none"),
            *plan.get("options", []), *(["--videos"] if find_ffmpeg(env) else []),
            *(["--parallel", str(parallel)] if parallel else [])]
    stop, stopped, child = threading.Event(), threading.Event(), [None]
    beat = threading.Thread(target=_heartbeat, args=(client, plan["heartbeat_url"], stop, stopped, child),
                            daemon=True)
    beat.start()
    try:
        process = subprocess.Popen(args, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        child[0] = process
        for line in process.stdout:
            out.write(redactor.apply(line))
            out.flush()
        code = process.wait()
    finally:
        stop.set()
    return code, stopped.is_set()


def comment(summary_path, environ):
    """Comment the report on the merge request (GitLab) or pull request (GitHub). -> what happened, in words"""
    if environ.get("GITLAB_CI"):
        from .gitlab import post_report as gitlab_post

        return gitlab_post(summary_path, environ=environ)
    from .github import post_report as github_post

    return github_post(summary_path, environ=environ)


def cmd_ci(args, environ=None, http=None, out=sys.stdout, err=sys.stderr):
    environ = os.environ if environ is None else environ
    key = (environ.get("EVERYROLE_API_KEY") or "").strip()
    if not key:
        print("error: set EVERYROLE_API_KEY (a workspace API key from EveryRole, Settings → API keys) as a masked CI "
              "variable", file=err)
        return 2
    client = Client(args.url or environ.get("EVERYROLE_URL") or environ.get("EVERYROLE_API_URL") or DEFAULT_URL, key,
                    http=http)
    payload = {**pipeline(environ),
               "base_url": args.base_url or environ.get("BASE_URL") or "",
               "environment": args.environment or environ.get("EVERYROLE_ENVIRONMENT") or ""}
    try:
        plan = client.call("POST", f"/api/v1/apps/{args.app}/ci/runs", json={k: v for k, v in payload.items() if v})
    except CiFailed as exc:
        print(f"error: {exc}", file=err)
        return 2
    print(f"· EveryRole run {plan['run']} of {plan['name']} on {plan['base_url']} ({plan['environment']}): "
          f"{len(plan.get('flows') or [])} tests", file=out, flush=True)
    values, missing = logins(plan, environ)
    if missing:
        reason = ("These CI variables aren't set, so nothing ran: " + ", ".join(missing) + ". Add them as masked "
                  "CI/CD variables (GitLab: Settings → CI/CD → Variables; GitHub: Settings → Secrets).")
        print(f"error: {reason}", file=err)
        _end(client, plan, reason, err)
        return 2
    redactor = Redactor({**values, "EVERYROLE_API_KEY": key})  # never printed, never in the report
    output = Path(args.output).resolve()
    with tempfile.TemporaryDirectory(prefix="everyrole-ci-") as folder:
        try:
            code, stopped = run_tests(plan, child_env(plan, key, values, environ), folder, output, args.parallel,
                                      redactor, client, out=out)
        except OSError as exc:
            _end(client, plan, f"the test run couldn't start ({exc})", err)
            print(f"error: the test run couldn't start ({exc})", file=err)
            return 2
    if stopped:
        print("· A person stopped this run in EveryRole.", file=out)
        return 2
    if not (output / "results.json").is_file():
        _end(client, plan, "the test run ended without a report", err)
        print("error: the test run ended without a report (see the lines above)", file=err)
        return 2
    scrub_report(output, redactor)
    data, light = report_zip(output, plan.get("max_upload_mb", 0) * 1024 * 1024)
    try:
        sent = client.call("POST", plan["results_url"], content=data, headers={"Content-Type": "application/zip"})
        print(f"· In EveryRole: {sent.get('app_url') or plan['app_url']}"
              + (" (replays left out: the report was too large)" if light else ""), file=out, flush=True)
        summary = output / "summary.md"
        if summary.is_file():
            summary.write_text(summary.read_text(encoding="utf-8").rstrip("\n")
                               + f"\n\nReplays and pictures: {sent.get('app_url') or plan['app_url']}\n",
                               encoding="utf-8")
    except CiFailed as exc:  # the verdict stands: the report is still in --output for the CI
        print(f"warning: the report didn't reach EveryRole: {exc}", file=err)
    if args.comment and (output / "summary.md").is_file():
        try:
            print(f"· {comment(output / 'summary.md', environ)}", file=out)
        except Exception as exc:  # a failed comment never hides the test result
            print(f"warning: no comment: {exc}", file=err)
    return code


def _end(client, plan, reason, err):
    try:
        client.call("POST", plan["end_url"], json={"error": reason})
    except CiFailed as exc:
        print(f"warning: {exc}", file=err)
