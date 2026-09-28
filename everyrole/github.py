"""Post the report as ONE pull-request comment, updated in place on every run.

The PR comes from the event payload (pull_request events) or, for deployment events such as Vercel's
`deployment_status`, from the pull requests associated with the deployed commit.
"""

import json
import os

import httpx

from .report import COMMENT_MARKER


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token, repository, *, api_url=None, client=None):
        self.repository = repository
        self.api = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.client = client or httpx.Client(timeout=20)
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def _request(self, method, path, **kwargs):
        response = self.client.request(method, f"{self.api}{path}", headers=self.headers, **kwargs)
        if response.status_code >= 400:
            raise GitHubError(f"GitHub {method} {path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else None

    def pull_number(self, event, sha):
        if event.get("pull_request", {}).get("number"):
            return event["pull_request"]["number"]
        sha = (event.get("deployment") or {}).get("sha") or sha
        if not sha:
            return None
        pulls = self._request("GET", f"/repos/{self.repository}/commits/{sha}/pulls") or []
        open_pulls = [p for p in pulls if p.get("state") == "open"]
        return (open_pulls or pulls or [{}])[0].get("number")

    def upsert_comment(self, number, body):
        page = 1
        while True:
            comments = self._request(
                "GET", f"/repos/{self.repository}/issues/{number}/comments", params={"per_page": 100, "page": page}
            ) or []
            mine = next((c for c in comments if COMMENT_MARKER in (c.get("body") or "")), None)
            if mine:
                self._request("PATCH", f"/repos/{self.repository}/issues/comments/{mine['id']}", json={"body": body})
                return "updated"
            if len(comments) < 100:
                break
            page += 1
        self._request("POST", f"/repos/{self.repository}/issues/{number}/comments", json={"body": body})
        return "created"


def post_report(summary_path, *, environ=None, client=None):
    environ = os.environ if environ is None else environ
    token, repository = environ.get("GITHUB_TOKEN"), environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        raise GitHubError("GITHUB_TOKEN and GITHUB_REPOSITORY are required to comment")
    event = {}
    if environ.get("GITHUB_EVENT_PATH") and os.path.isfile(environ["GITHUB_EVENT_PATH"]):
        with open(environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
            event = json.load(handle)
    github = GitHub(token, repository, client=client)
    number = github.pull_number(event, environ.get("GITHUB_SHA"))
    if not number:
        return "skipped: no pull request for this commit"
    with open(summary_path, encoding="utf-8") as handle:
        body = handle.read()
    return f"{github.upsert_comment(number, body)} comment on #{number}"
