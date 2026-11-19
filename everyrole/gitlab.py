"""Post the report as ONE merge-request comment (a note), updated in place on every run: `everyrole ci --comment`
in a GitLab CI job.

The merge request is the pipeline's own (CI_MERGE_REQUEST_IID, in merge-request pipelines) or, in a branch pipeline,
the open merge request of the commit. The token is a project (or personal) access token with the `api` scope, set as
the masked CI variable EVERYROLE_GITLAB_TOKEN: GitLab's own job token can't write comments.
"""

from urllib.parse import quote

import httpx

from .report import COMMENT_MARKER


class GitLabError(RuntimeError):
    pass


class GitLab:
    def __init__(self, token, api_url, project, *, client=None):
        self.api = api_url.rstrip("/")
        self.project = quote(str(project), safe="")
        self.client = client or httpx.Client(timeout=20)
        self.headers = {"PRIVATE-TOKEN": token}

    def _request(self, method, path, **kwargs):
        response = self.client.request(method, f"{self.api}/projects/{self.project}{path}", headers=self.headers,
                                       **kwargs)
        if response.status_code >= 400:
            raise GitLabError(f"GitLab {method} {path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else None

    def merge_request(self, iid, sha):
        if iid:
            return int(iid)
        if not sha:
            return None
        found = self._request("GET", f"/repository/commits/{sha}/merge_requests") or []
        opened = [m for m in found if m.get("state") == "opened"]
        return (opened or found or [{}])[0].get("iid")

    def upsert_note(self, iid, body):
        page = 1
        while True:
            notes = self._request("GET", f"/merge_requests/{iid}/notes",
                                  params={"per_page": 100, "page": page, "sort": "asc"}) or []
            mine = next((n for n in notes if COMMENT_MARKER in (n.get("body") or "")), None)
            if mine:
                self._request("PUT", f"/merge_requests/{iid}/notes/{mine['id']}", json={"body": body})
                return "updated"
            if len(notes) < 100:
                break
            page += 1
        self._request("POST", f"/merge_requests/{iid}/notes", json={"body": body})
        return "created"


def post_report(summary_path, *, environ, client=None):
    token = environ.get("EVERYROLE_GITLAB_TOKEN")
    api, project = environ.get("CI_API_V4_URL"), environ.get("CI_PROJECT_ID")
    if not token:
        return "skipped: set EVERYROLE_GITLAB_TOKEN (a project access token with the api scope) to comment"
    if not api or not project:
        raise GitLabError("CI_API_V4_URL and CI_PROJECT_ID are required to comment (a GitLab CI job sets them)")
    gitlab = GitLab(token, api, project, client=client)
    iid = gitlab.merge_request(environ.get("CI_MERGE_REQUEST_IID"), environ.get("CI_COMMIT_SHA"))
    if not iid:
        return "skipped: no merge request for this commit"
    with open(summary_path, encoding="utf-8") as handle:
        body = handle.read()
    return f"{gitlab.upsert_note(iid, body)} comment on !{iid}"
