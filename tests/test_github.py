import json

import httpx
import pytest

from everyrole.github import GitHubError, post_report
from everyrole.report import COMMENT_MARKER


def fake_github(existing_comments=(), pulls=()):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx.Response(200, json=list(pulls))
        if path.endswith("/comments") and request.method == "GET":
            return httpx.Response(200, json=list(existing_comments))
        return httpx.Response(201, json={"id": 9})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def env(tmp_path, event):
    (tmp_path / "event.json").write_text(json.dumps(event))
    (tmp_path / "summary.md").write_text(COMMENT_MARKER + "\nresults")
    return {
        "GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "me/app", "GITHUB_SHA": "abc",
        "GITHUB_EVENT_PATH": str(tmp_path / "event.json"), "GITHUB_API_URL": "https://api.github.test",
    }


def test_creates_comment_for_pull_request_event(tmp_path):
    client, calls = fake_github()
    out = post_report(tmp_path / "summary.md", environ=env(tmp_path, {"pull_request": {"number": 7}}), client=client)
    assert out == "created comment on #7"
    assert calls[-1][:2] == ("POST", "/repos/me/app/issues/7/comments")


def test_updates_existing_marked_comment(tmp_path):
    client, calls = fake_github(existing_comments=[{"id": 1, "body": "hi"}, {"id": 5, "body": COMMENT_MARKER + " old"}])
    out = post_report(tmp_path / "summary.md", environ=env(tmp_path, {"pull_request": {"number": 7}}), client=client)
    assert out == "updated comment on #7"
    assert calls[-1][:2] == ("PATCH", "/repos/me/app/issues/comments/5")


def test_deployment_event_finds_pr_from_commit(tmp_path):
    client, calls = fake_github(pulls=[{"number": 3, "state": "closed"}, {"number": 4, "state": "open"}])
    event = {"deployment": {"sha": "def"}, "deployment_status": {"state": "success"}}
    out = post_report(tmp_path / "summary.md", environ=env(tmp_path, event), client=client)
    assert out == "created comment on #4"
    assert calls[0][1] == "/repos/me/app/commits/def/pulls"


def test_no_pull_request_is_skipped(tmp_path):
    client, _ = fake_github(pulls=[])
    out = post_report(tmp_path / "summary.md", environ=env(tmp_path, {"deployment": {"sha": "x"}}), client=client)
    assert out.startswith("skipped")


def test_missing_token_raises(tmp_path):
    with pytest.raises(GitHubError):
        post_report(tmp_path / "summary.md", environ={})
