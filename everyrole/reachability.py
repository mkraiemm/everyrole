"""Before testing: does the site answer at all?

A site that is down is an app failure and must be reported loudly, never hidden as "not checked". But if
this machine has no network, the site is not to blame: the model provider's address (which every run needs
anyway) is used as the reference. Site unreachable + provider reachable = the site is down.
"""

import httpx

NAME = "site responds"


def check(base_url, reference_url, *, get=None, timeout_s=30):
    """-> None when the site answers, else a failing result (cause app or machine)."""
    get = get or (lambda url: httpx.get(url, timeout=timeout_s, follow_redirects=True))
    try:
        response = get(base_url)
    except httpx.HTTPError as exc:
        problem = f"{base_url} did not answer: {type(exc).__name__}: {exc}"
    else:
        if response.status_code < 500:
            return None
        problem = f"{base_url} answered HTTP {response.status_code}"
    cause = "app" if _reachable(reference_url, get) else "machine"
    if cause == "machine":
        problem += " (this machine cannot reach the model provider either: no network here)"
    return {"name": NAME, "status": "failed_expect" if cause == "app" else "error", "passed": False,
            "cause": cause, "error": problem, "duration_ms": 0, "expectations": [], "steps": [], "warnings": []}


def _reachable(url, get):
    try:
        get(url)  # any HTTP answer (even 401/404) proves the network works
        return True
    except httpx.HTTPError:
        return False
