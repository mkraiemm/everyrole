"""Before testing: does the site answer at all?

A site that is down is an app failure and must be reported loudly, never hidden as "not checked". But if
this machine has no network, the site is not to blame: the model provider's address (which every run needs
anyway) is used as the reference. Site unreachable + provider reachable = the site is down.

What people read says what really happened (it answered with a server error, it didn't answer in time, it couldn't
be reached), never "it did not answer" for a site that answered (a new customer's trial, 2026-11-17). A connection
that fails is tried once more first: one dropped connection is not a site that is down.
"""

import time
from urllib.parse import urlparse

import httpx

from .chrome import egress_proxy

NAME = "site responds"
AGAIN_AFTER_S = 3


def check(base_url, reference_url, *, get=None, timeout_s=30, sleep=time.sleep):
    """-> None when the site answers, else a failing result (cause app or machine), with `said`: what happened, as
    people read it ("www.shop.com didn’t answer within 30 seconds")."""
    get = get or (lambda url: httpx.get(url, timeout=timeout_s, follow_redirects=True, proxy=egress_proxy()))
    host = urlparse(base_url).hostname or base_url
    for attempt in (1, 2):
        try:
            response = get(base_url)
        except httpx.TimeoutException as exc:
            problem, said = (f"{base_url} did not answer: {type(exc).__name__}: {exc}",
                             f"{host} didn’t answer within {timeout_s} seconds")
        except httpx.HTTPError as exc:
            problem, said = (f"{base_url} did not answer: {type(exc).__name__}: {exc}",
                             f"{host} couldn’t be reached")
        else:
            if response.status_code < 500:
                return None
            return _down(base_url, reference_url, get, f"{base_url} answered HTTP {response.status_code}",
                         f"{host} answered with a server error ({response.status_code})")
        if attempt == 1:
            sleep(AGAIN_AFTER_S)
    return _down(base_url, reference_url, get, problem, said)


def _down(base_url, reference_url, get, problem, said):
    cause = "app" if _reachable(reference_url, get) else "machine"
    if cause == "machine":
        problem += " (this machine cannot reach the model provider either: no network here)"
    return {"name": NAME, "status": "failed_expect" if cause == "app" else "error", "passed": False,
            "cause": cause, "error": problem, "said": said, "duration_ms": 0, "expectations": [], "steps": [],
            "warnings": []}


def _reachable(url, get):
    try:
        get(url)  # any HTTP answer (even 401/404) proves the network works
        return True
    except httpx.HTTPError:
        return False
