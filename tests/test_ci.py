"""`everyrole ci`: an app's tests run in your own CI, with test logins that never leave it.

The pieces on their own (no browser), the command against a stand-in EveryRole API (a refused key, a missing login
variable), and one run end to end as a GitLab job runs it: a stand-in EveryRole (the plan, heartbeats, the AI model
calls answered by simple rules, the report upload), a real app with a sign-in form and a real headless Chrome. The
admin's login lives only in the job's environment (EVERYROLE_LOGIN_ADMIN_*): the run signs in with it, the report is
uploaded, and the password is nowhere but in the app's own sign-in form: not in the job's output, the report, the
upload, nor any model request."""

import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx

from everyrole import ci
from everyrole.redact import Redactor

PASSWORD = "Gl-ci-only-Passw0rd!"
EMAIL = "admin@ci-shop.test"
KEY = "er_live_test_key"


# ---------------------------------------------------------------------------------------------- the pieces

PLAN = {"logins": [{"kind": "admin", "username": {"secret": "SW_ADMIN_EMAIL",
                                                   "variable": "EVERYROLE_LOGIN_ADMIN_USERNAME"},
                    "password": {"secret": "SW_ADMIN_PASSWORD", "variable": "EVERYROLE_LOGIN_ADMIN_PASSWORD"}}],
        "config": {"personas": {"admin": {"values": {"password": "{{secrets.SW_ADMIN_PASSWORD}}"}}}},
        "flows": [{"file": "pay.json", "content": {"values": {"code": "{{secrets.EVERYROLE_PAY_CODE}}"}}}],
        "models": {"decide_url": "https://er/decide", "decide_model": "jev-latest", "text_base_url": "https://er/text",
                   "text_models": ["small/typing"]}}


def test_each_login_is_read_from_its_ci_variables_and_a_missing_one_is_named():
    values, missing = ci.logins(PLAN, {"EVERYROLE_LOGIN_ADMIN_USERNAME": EMAIL, "EVERYROLE_PAY_CODE": "FREE100"})
    assert values == {"SW_ADMIN_EMAIL": EMAIL, "EVERYROLE_PAY_CODE": "FREE100"}
    assert missing == ["EVERYROLE_LOGIN_ADMIN_PASSWORD"]


def test_the_test_run_gets_the_logins_by_the_configs_names_and_everyroles_models_never_the_ci_variables():
    environ = {"EVERYROLE_LOGIN_ADMIN_PASSWORD": PASSWORD, "EVERYROLE_API_KEY": KEY, "PATH": "/bin",
               "TEXT_MODEL": "someone/elses", "TEXT_MODEL_BACKUP": "old/one"}
    env = ci.child_env(PLAN, KEY, {"SW_ADMIN_PASSWORD": PASSWORD}, environ)
    assert "EVERYROLE_LOGIN_ADMIN_PASSWORD" not in env and "EVERYROLE_API_KEY" not in env
    assert env["SW_ADMIN_PASSWORD"] == PASSWORD and env["PATH"] == "/bin"
    assert env["TYPESAFE_URL"] == "https://er/decide" and env["TYPESAFE_API_KEY"] == KEY
    assert env["TEXT_MODEL"] == "small/typing" and "TEXT_MODEL_BACKUP" not in env


def test_the_report_never_sends_the_account_book_and_drops_replays_first_when_too_large(tmp_path):
    (tmp_path / "results.json").write_text("{}", encoding="utf-8")
    (tmp_path / "accounts.json").write_text('{"password": "x"}', encoding="utf-8")
    (tmp_path / "home.jpg").write_bytes(b"jpg")
    (tmp_path / "videos").mkdir()
    (tmp_path / "videos" / "home.mp4").write_bytes(os.urandom(4000))
    (tmp_path / "frames" / "deep").mkdir(parents=True)
    (tmp_path / "frames" / "deep" / "0001.jpg").write_bytes(b"x")
    data, light = ci.report_zip(tmp_path)
    assert not light and sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == [
        "home.jpg", "results.json", "videos/home.mp4"]
    data, light = ci.report_zip(tmp_path, limit_bytes=1000)
    assert light and sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == ["home.jpg", "results.json"]


def test_a_login_that_somehow_reached_the_report_is_replaced_by_its_name(tmp_path):
    (tmp_path / "summary.md").write_text(f"typed {PASSWORD}", encoding="utf-8")
    assert ci.scrub_report(tmp_path, Redactor({"SW_ADMIN_PASSWORD": PASSWORD})) == ["summary.md"]
    assert (tmp_path / "summary.md").read_text(encoding="utf-8") == "typed [secret:SW_ADMIN_PASSWORD]"


def test_the_pipeline_says_where_it_runs():
    assert ci.pipeline({"GITLAB_CI": "true", "CI_COMMIT_SHA": "abc", "CI_COMMIT_REF_NAME": "main",
                        "CI_PIPELINE_URL": "https://gitlab.example/p/-/pipelines/1"})["source"] == "gitlab"
    github = ci.pipeline({"GITHUB_ACTIONS": "true", "GITHUB_SHA": "abc", "GITHUB_REPOSITORY": "o/r",
                          "GITHUB_RUN_ID": "9"})
    assert github["source"] == "github" and github["pipeline_url"] == "https://github.com/o/r/actions/runs/9"


def test_the_merge_request_gets_one_comment_updated_in_place(tmp_path):
    from everyrole.gitlab import post_report
    from everyrole.report import COMMENT_MARKER

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("private-token")))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 5, "body": f"{COMMENT_MARKER} old"}])
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    summary = tmp_path / "summary.md"
    summary.write_text(f"{COMMENT_MARKER}\nAll passed", encoding="utf-8")
    said = post_report(summary, environ={"EVERYROLE_GITLAB_TOKEN": "glpat", "CI_API_V4_URL": "https://gl/api/v4",
                                         "CI_PROJECT_ID": "42", "CI_MERGE_REQUEST_IID": "7"}, client=client)
    assert said == "updated comment on !7"
    assert seen[-1] == ("PUT", "/api/v4/projects/42/merge_requests/7/notes/5", "glpat")
    assert post_report(summary, environ={}).startswith("skipped")


def test_the_gitlab_job_starts_a_pipeline_on_its_own():
    """GitLab creates no pipeline whose only jobs are in .pre/.post ("would have been empty"): the job, often the
    only one in a repository that deploys elsewhere, sits in a default stage, and says how to run it after a deploy
    job instead."""
    import re

    text = (Path(ci.__file__).parent / "ci_files" / "everyrole.gitlab-ci.yml").read_text(encoding="utf-8")
    assert re.findall(r"^  stage: *(\S+)", text, re.M) == ["test"]  # each job's own stage (comments start with #)
    assert "stage: .post" in text  # the hint for a pipeline with a deploy job


def test_the_gitlab_job_installs_this_runner_and_reports_junit():
    job = (Path(ci.__file__).parent / "ci_files" / "everyrole.gitlab-ci.yml").read_text(encoding="utf-8")
    assert "git+https://github.com/mkraiemm/everyrole.git@" in job
    assert "everyrole ci --app" in job and "junit: everyrole-report/junit.xml" in job


# ---------------------------------------------------------------------------------------------- the command

def _args(**overrides):
    return SimpleNamespace(**{"app": "shop", "url": "https://everyrole.test", "base_url": None, "environment": None,
                              "output": "everyrole-report", "parallel": None, "comment": False, **overrides})


def test_without_an_api_key_nothing_is_asked_and_it_says_so_plainly():
    err = io.StringIO()
    assert ci.cmd_ci(_args(), environ={}, err=err) == 2
    assert "set EVERYROLE_API_KEY" in err.getvalue()


def test_a_refused_key_ends_the_job_with_everyroles_own_words():
    def handler(request):
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(401, json={"message": "This API key was revoked."})

    err = io.StringIO()
    code = ci.cmd_ci(_args(), environ={"EVERYROLE_API_KEY": KEY}, http=httpx.Client(transport=httpx.MockTransport(
        handler)), out=io.StringIO(), err=err)
    assert code == 2 and "This API key was revoked." in err.getvalue()


def test_a_missing_login_variable_runs_nothing_names_it_and_ends_the_run():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.url.path.endswith("/ci/runs"):
            return httpx.Response(201, json={**PLAN, "run": 7, "name": "Shop", "base_url": "https://shop.example.com",
                                             "environment": "production", "end_url": "/api/v1/ci/runs/7/end"})
        return httpx.Response(200, json={"run": 7, "status": "failed"})

    out, err = io.StringIO(), io.StringIO()
    code = ci.cmd_ci(_args(), environ={"EVERYROLE_API_KEY": KEY, "EVERYROLE_LOGIN_ADMIN_USERNAME": EMAIL,
                                       "GITLAB_CI": "true", "CI_COMMIT_SHA": "abc"},
                     http=httpx.Client(transport=httpx.MockTransport(handler)), out=out, err=err)
    assert code == 2
    assert "EVERYROLE_LOGIN_ADMIN_PASSWORD" in err.getvalue() and EMAIL not in out.getvalue() + err.getvalue()
    started, ended = calls
    assert started[1] == "/api/v1/apps/shop/ci/runs" and started[2] == {"source": "gitlab", "commit": "abc"}
    assert ended[1] == "/api/v1/ci/runs/7/end" and "EVERYROLE_LOGIN_ADMIN_PASSWORD" in ended[2]["error"]


# ---------------------------------------------------------------------------------------------- end to end

APP_HOME = b"<!doctype html><html><body><h1>Hi</h1><p>Welcome to the shop</p></body></html>"
LOGIN_FORM = (b"<!doctype html><html><body><h1>Sign in</h1><form method='post' action='/login'>"
              b"<label>Email <input name='email' type='email'></label>"
              b"<label>Password <input name='password' type='password'></label>"
              b"<button type='submit'>Sign in</button></form></body></html>")
DASHBOARD = b"<!doctype html><html><body><h1>Admin dashboard</h1><p>3 orders today</p></body></html>"


class ShopApp(BaseHTTPRequestHandler):
    """The team's app: a home page, a sign-in form, and a dashboard only the admin reaches."""

    logins = []

    def log_message(self, *args):
        pass

    def _send(self, body, status=200, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/login"):
            return self._send(LOGIN_FORM)
        if self.path.startswith("/dashboard"):
            if "session=admin" in (self.headers.get("Cookie") or ""):
                return self._send(DASHBOARD)
            return self._send(b"", 303, [("Location", "/login")])
        return self._send(APP_HOME)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
        ShopApp.logins.append(form)
        if form.get("email") == [EMAIL] and form.get("password") == [PASSWORD]:
            return self._send(b"", 303, [("Location", "/dashboard"), ("Set-Cookie", "session=admin; Path=/")])
        return self._send(LOGIN_FORM.replace(b"<h1>Sign in</h1>", b"<h1>Sign in</h1><p>Wrong password</p>"))


def _choice(keys, pick):
    keys = list(keys)
    rest = (1 - 0.9) / (len(keys) - 1) if len(keys) > 1 else 0
    return {"choice": pick, "confidence": 0.9,
            "probabilities": {k: (0.9 if len(keys) > 1 else 1.0) if k == pick else rest for k in keys}}


def decide(body):
    """A model's answer, by simple rules: type into an empty field (the value it names), else press Sign in on a
    form, else done."""
    questions, state = body["questions"], body["state"]
    if "value" in questions:  # which named value goes in this field
        names = list(questions["value"]["criteria"])
        field = state.get("field") or {}
        wants_password = bool(field.get("secret")) or "pass" in str(field.get("label", "")).lower()
        pick = next((n for n in names if ("password" in n) == wants_password and n != "WRITE"), names[0])
        return {"value": _choice(names, pick)}
    operations = questions["operation"]["criteria"]
    typing = (questions.get("type_text_target") or {}).get("criteria") or {}
    empty = [i for i, c in typing.items() if not c.get("current_value")]
    clicks = (questions.get("click_target") or {}).get("criteria") or {}
    submit = next((i for i, c in clicks.items() if "Sign in" in c["element"] and "button" in str(c.get("role", ""))
                   or c["element"].endswith("] Sign in")), None)
    if empty and "TYPE_TEXT" in operations:
        return {"operation": _choice(operations, "TYPE_TEXT"), "type_text_target": _choice(typing, empty[0])}
    if typing and submit and "CLICK" in operations:
        return {"operation": _choice(operations, "CLICK"), "click_target": _choice(clicks, submit)}
    return {"operation": _choice(operations, "DONE")}


class FakeEveryRole(BaseHTTPRequestHandler):
    """EveryRole's CI API, as the runner sees it: the plan, heartbeats, the model calls, the report upload."""

    shop_url = ""
    requests = []  # every request: (method, path, authorization, raw body)
    uploads = []

    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def do_GET(self):
        FakeEveryRole.requests.append(("GET", self.path, self.headers.get("Authorization"), b""))
        if self.path.endswith("/model/text/models"):
            return self._json({"data": []})
        return self._json({"message": "not here"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeEveryRole.requests.append(("POST", self.path, self.headers.get("Authorization"), raw))
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._json({"message": "Unknown API key."}, 401)
        run = f"{self._base()}/api/v1/ci/runs/1"
        if self.path == "/api/v1/apps/shop/ci/runs":
            return self._json(self.plan(run), 201)
        if self.path.endswith("/heartbeat"):
            return self._json({"stop": False})
        if self.path.endswith("/model/decide"):
            return self._json({"answers": decide(json.loads(raw)), "model": "jev-latest", "usage": {}})
        if self.path.endswith("/model/text/chat/completions"):
            return self._json({"choices": [{"message": {"content": '{"text": null}'}}], "usage": {}})
        if self.path.endswith("/results"):
            FakeEveryRole.uploads.append(raw)
            return self._json({"app_url": f"{self._base()}/p/shop/runs/1"})
        if self.path.endswith("/end"):
            return self._json({"run": 1, "status": "failed"})
        return self._json({"message": "not here"}, 404)

    def plan(self, run):
        shop = FakeEveryRole.shop_url
        config = {"login": {"start": "{{BASE_URL}}/login", "goal": "Sign in with the account's email and password.",
                            "expect": [{"url_contains": "/dashboard"}]},
                  "personas": {"admin": {"values": {"email": "{{secrets.SW_ADMIN_EMAIL}}",
                                                    "password": "{{secrets.SW_ADMIN_PASSWORD}}"}}}}
        flows = [{"name": "home", "title": "The home page opens", "start": "{{BASE_URL}}/", "goal": "See the home page",
                  "expect": [{"text_visible": "Welcome to the shop"}]},
                 {"name": "dashboard", "title": "The admin sees the dashboard", "as": "admin",
                  "start": "{{BASE_URL}}/dashboard", "goal": "See today's orders on the dashboard",
                  "expect": [{"text_visible": "Admin dashboard"}]}]
        return {"run": 1, "app": "shop", "name": "Shop", "environment": "production", "base_url": shop,
                "options": ["--env", "production", "--skip-sign-up"], "config_file": "everyrole.config.json",
                "config": config, "flows": [{"file": f"{f['name']}.json", "content": f} for f in flows],
                "logins": PLAN["logins"],
                "models": {"decide_url": f"{run}/model/decide", "decide_model": "jev-latest",
                           "text_base_url": f"{run}/model/text", "text_models": ["small/typing"]},
                "heartbeat_url": f"{run}/heartbeat", "results_url": f"{run}/results", "end_url": f"{run}/end",
                "app_url": f"{self._base()}/p/shop/runs/1", "max_upload_mb": 50}


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_a_gitlab_job_runs_the_tests_with_its_own_login_and_the_password_never_leaves_it(tmp_path):
    from jev_ultrafast import model

    import everyrole
    from everyrole import daemons

    shop, shop_url = _serve(ShopApp)
    api, api_url = _serve(FakeEveryRole)
    FakeEveryRole.shop_url = shop_url
    output = tmp_path / "everyrole-report"
    code = os.pathsep.join(str(p) for p in (Path(everyrole.__file__).parents[1], Path(model.__file__).parents[1]))
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "CHROME_PATH",
            "FFMPEG_PATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMSPEC", "WINDIR")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}  # no model key: the job has none
    env.update(PYTHONPATH=code, EVERYROLE_API_KEY=KEY, EVERYROLE_URL=api_url, GITLAB_CI="true",
               CI_COMMIT_SHA="0123456789abcdef", CI_COMMIT_REF_NAME="main", PYTHONUTF8="1",
               EVERYROLE_LOGIN_ADMIN_USERNAME=EMAIL, EVERYROLE_LOGIN_ADMIN_PASSWORD=PASSWORD)
    env[daemons.ENV] = str(tmp_path / "daemons")
    try:
        done = subprocess.run([sys.executable, "-m", "everyrole.cli", "ci", "--app", "shop", "--output", str(output)],
                              env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    finally:
        daemons.stop(tmp_path / "daemons")
        shop.shutdown()
        api.shutdown()
    said = done.stdout + done.stderr
    assert done.returncode == 0, said[-3000:]

    # it signed in with the CI's login (the app got it from the form), and every test passed
    assert {"email": [EMAIL], "password": [PASSWORD]} in ShopApp.logins
    results = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert {r["name"]: r["passed"] for r in results["results"]} == {
        "home": True, "dashboard@admin": True, "login@admin": True}
    assert (output / "junit.xml").is_file()  # GitLab's test report

    # the report reached EveryRole: one zip with the verdicts, never the account book
    assert len(FakeEveryRole.uploads) == 1
    names = zipfile.ZipFile(io.BytesIO(FakeEveryRole.uploads[0])).namelist()
    assert "results.json" in names and "accounts.json" not in names

    # the models: asked through EveryRole with the API key, the password field handled by its name only
    models = [r for r in FakeEveryRole.requests if "/model/" in r[1]]
    assert models and {r[2] for r in models} == {f"Bearer {KEY}"}
    assert any(b"secret value named 'password'" in r[3] for r in models)

    # the password: nowhere but in the app's own form
    secret = PASSWORD.encode()
    assert not any(secret in r[3] for r in FakeEveryRole.requests)
    assert secret not in FakeEveryRole.uploads[0]
    assert PASSWORD not in said and KEY not in said
    for path in output.rglob("*"):
        assert path.is_dir() or secret not in path.read_bytes(), path
