"""Login links: the customer's own app signs EveryRole in, so EveryRole never holds a password.

The customer adds one endpoint to their app (e.g. https://staging.example.com/api/everyrole-login). When a run,
an exploration or "Check sign-in" needs a kind of user, EveryRole POSTs a small signed request to it:

    POST <endpoint>
    Content-Type: application/json
    EveryRole-Signature: <Ed25519 signature of the exact body bytes, base64url>

    {"audience": "<endpoint>", "kind": "admin", "nonce": "<32 hex>", "run": "<run id>", "timestamp": 1763550000,
     "v": 1}

The endpoint checks the signature with the app's PUBLIC key, refuses a request older than 60 seconds, one whose
nonce it has seen, or one meant for another address (`audience`), then makes a one-time sign-in link for the test
user it keeps for that kind (Supabase generateLink, a Clerk sign-in token...) and answers {"url": "<link>"}. The
test browser opens the link and is signed in; its session is used like any other sign-in.

Who holds what:
  EveryRole  the app's PRIVATE signing key, encrypted at rest with the project's other secrets (never shown, never
             sent anywhere, never given to a model).
  The app    the PUBLIC key only: nothing secret to leak, store or rotate on the customer's side.
A copy of the customer's code or settings signs nobody in. A leak of EveryRole's database alone neither: the key is
in the encrypted secrets, under a server key that is not in the database. What any leak can reach at most is the
test users the customer's endpoint chose, on the environments where they turned the endpoint on, and only until
they remove it or EveryRole makes a new key ("New key": the old one stops working at once).

The link the app answers with is a credential: it is never logged, printed, saved, put in a result, a screenshot or
a replay, or shown to a model. It lives in this process's memory between the answer and the browser opening it.
"""

import base64
import json
import os
import secrets as _random
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from . import verdict
from .site import HASH_ROUTE

KEY_SECRET = "EVERYROLE_LOGIN_LINK_KEY"  # the app's private signing key, as the job's environment gives it
SIGNATURE_HEADER = "EveryRole-Signature"
VERSION = 1
WINDOW_S = 60  # a request older (or newer) than this is refused by the customer's endpoint
TIMEOUT_S = 20  # an endpoint that makes a link answers well within this
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
FAILED = "Your login link"  # every failure said to people starts so (exploring's messages keep it: plain.py)


class LoginLinkError(Exception):
    """A login link that didn't sign in, said for people. `cause`: verdict.SETUP (the endpoint or its key needs
    fixing) or verdict.UNAVAILABLE (the app didn't answer: tried again later)."""

    def __init__(self, message, cause=verdict.SETUP):
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------------------------------- keys


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text):
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_key():
    """A new signing key pair -> (private, public), each base64url without padding (the private one is the 32-byte
    Ed25519 seed, the public one the 32-byte raw public key the customer's endpoint checks with)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return _b64(_private_bytes(private)), _b64(_public_bytes(private.public_key()))


def public_of(private):
    """The public key of a private key, as the customer pastes it ("" for none or a damaged one)."""
    try:
        return _b64(_public_bytes(_private_key(private).public_key()))
    except ValueError:
        return ""


def _private_key(private):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        return Ed25519PrivateKey.from_private_bytes(_unb64(private))
    except Exception:
        raise ValueError("not a login-link key") from None


def _private_bytes(key):
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def _public_bytes(key):
    from cryptography.hazmat.primitives import serialization

    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ---------------------------------------------------------------------------------------------------- requests


def body(kind, *, audience, run_id="", now=None, nonce=None):
    """The exact bytes EveryRole signs and sends (compact JSON, keys sorted)."""
    data = {"audience": audience, "kind": kind, "nonce": nonce or _random.token_hex(16), "run": run_id or "",
            "timestamp": int(time.time() if now is None else now), "v": VERSION}
    return json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign(private, data):
    """The signature header's value for `data`."""
    return _b64(_private_key(private).sign(data))


@dataclass
class Verdict:
    ok: bool
    status: int = 200
    reason: str = ""
    kind: str = ""


class Verifier:
    """What the customer's endpoint does, in Python: the reference the snippets follow, and the fake app's check in
    tests. `seen`: nonces already used inside the window (one process; an app with several should keep them in its
    database or Redis, see the docs)."""

    def __init__(self, public, audience, *, window_s=WINDOW_S, clock=time.time):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        self._key = Ed25519PublicKey.from_public_bytes(_unb64(public))
        self._audience = audience
        self._window_s = window_s
        self._clock = clock
        self._seen = {}

    def check(self, raw, signature):
        from cryptography.exceptions import InvalidSignature

        try:
            self._key.verify(_unb64(signature), raw)
        except (InvalidSignature, ValueError):
            return Verdict(False, 401, "bad signature")
        try:
            data = json.loads(raw)
        except ValueError:
            return Verdict(False, 400, "not JSON")
        now = self._clock()
        if data.get("v") != VERSION or abs(now - float(data.get("timestamp", 0))) > self._window_s:
            return Verdict(False, 401, "too old")
        if data.get("audience") != self._audience:
            return Verdict(False, 401, "meant for another address")
        self._seen = {n: t for n, t in self._seen.items() if now - t <= self._window_s}
        if data.get("nonce") in self._seen:
            return Verdict(False, 401, "replayed")
        self._seen[data.get("nonce")] = now
        return Verdict(True, kind=str(data.get("kind", "")))


# ---------------------------------------------------------------------------------------------------- the endpoint


def check_endpoint(url):
    """The endpoint as saved -> its address, or ValueError with what to fix. https, or http on this computer only."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or " " in url:
        raise ValueError("Enter the full address of your login-link endpoint, like "
                         "https://staging.example.com/api/everyrole-login")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("Your login-link endpoint must use https (a sign-in link travels back on it).")
    if parsed.username or parsed.password:
        raise ValueError("Leave the user name and password out of the endpoint's address.")
    return url


def fetch_link(endpoint, kind, private, *, run_id="", post=None, now=None):
    """Ask the app's endpoint for a one-time sign-in link for `kind`. -> the link (keep it in memory only).
    Raises LoginLinkError with a sentence for people; never with the link or the endpoint's answer in it."""
    endpoint = check_endpoint(endpoint)
    if not private:
        raise LoginLinkError(f"{FAILED} has no key on EveryRole’s side: open this kind of user and turn the login "
                             "link on again.")
    data = body(kind, audience=endpoint, run_id=run_id, now=now)
    headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign(private, data),
               "User-Agent": "EveryRole-LoginLink/1"}
    post = post or _post
    try:
        status, text = post(endpoint, data, headers)
    except LoginLinkError:
        raise
    except Exception as exc:  # no answer at all: the app is down or the address is wrong
        raise LoginLinkError(f"{FAILED} didn’t answer ({_no_answer(exc)}). Check its address and that your app is "
                             "up.", verdict.UNAVAILABLE) from None
    if status in (401, 403):
        raise LoginLinkError(f"{FAILED} answered {status}: it refused EveryRole’s request. Check that "
                             "EVERYROLE_LOGIN_KEY in your app is the public key shown here, and that its clock is "
                             "right.")
    if status == 404:
        raise LoginLinkError(f"{FAILED} answered 404: the endpoint isn’t there, or is turned off on this environment "
                             "(EVERYROLE_LOGIN_KEY not set), or has no test user for this kind of user.")
    if status >= 500 or status == 429:
        raise LoginLinkError(f"{FAILED} answered {status}: your app had a problem making the link. Its own logs say "
                             "why.", verdict.UNAVAILABLE)
    if status != 200:
        raise LoginLinkError(f"{FAILED} answered {status}; it should answer 200 with {{\"url\": \"…\"}}.")
    try:
        link = json.loads(text).get("url")
    except (ValueError, AttributeError):
        link = None
    if not isinstance(link, str) or not link.strip():
        raise LoginLinkError(f"{FAILED} answered, but gave no url: it should answer {{\"url\": \"…\"}} with the "
                             "one-time sign-in link.")
    parsed = urlparse(link.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LoginLinkError(f"{FAILED} gave something that isn’t a web address: it should be the full sign-in link "
                             "(https://…).")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise LoginLinkError(f"{FAILED} gave an http link: a sign-in link must use https.")
    return link.strip()


def _no_answer(exc):
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return f"nothing within {TIMEOUT_S} seconds"
    if "connect" in name:
        return "it couldn’t be reached"
    return "the connection failed"


def _post(url, data, headers):
    """One POST, through the egress proxy when there is one (chrome.EGRESS_PROXY_ENV), like everything the test
    browser opens."""
    import httpx

    from .chrome import EGRESS_PROXY_ENV

    proxy = os.environ.get(EGRESS_PROXY_ENV, "")
    response = httpx.post(url, content=data, headers=headers, timeout=TIMEOUT_S, follow_redirects=False,
                          **({"proxy": f"http://{proxy}"} if proxy else {}))
    return response.status_code, response.text


def bare(url):
    """An address without its query and fragment: what may be said about where a link landed (a magic link's
    tokens ride in both). A single-page app's route after `#` stays, without its own query."""
    parsed = urlparse(url or "")
    route = parsed.fragment.split("?")[0] if HASH_ROUTE.match(parsed.fragment) else ""
    return urlunparse(parsed._replace(query="", fragment=route, params=""))


# ---------------------------------------------------------------------------------------------------- signing in

SETTLE_S = 15  # the link's page has this long to finish signing in (a redirect, the app storing its session)


def sign_in(config, persona, *, site, run_id="", env=None, fetch=None, opener=None, checker=None,
            clock=time.monotonic):
    """Sign `persona` in with its login link, without any AI: ask the app for a link, open it in a fresh browser
    context, keep the session, then prove it by opening the check page with that session (the page the link landed
    on, unless the config names one). -> (result dict like a FlowResult, session state or None)."""
    from .signin import _result, session_holds

    started, name = clock(), f"login@{persona}"
    spec = config.personas[persona].login_link
    env = os.environ if env is None else env

    def failed(message, cause):
        result = _result(name, persona, False, "error" if cause == verdict.MACHINE else "failed_expect", message,
                         cause, started, clock)
        return {**result, "saved_signin": False, "signed_in_by": "login_link"}, None

    try:
        link = (fetch or fetch_link)(spec["url"], persona, env.get(KEY_SECRET, ""), run_id=run_id)
    except LoginLinkError as exc:
        return failed(str(exc), exc.cause)
    except ValueError as exc:  # the saved endpoint itself (check_endpoint)
        return failed(f"{FAILED}: {exc}", verdict.SETUP)
    try:
        state, landed, passwords = (opener or open_link)(link, site)
    except Exception as exc:  # never its message: it could quote the link
        return failed(f"The test browser couldn’t open the sign-in link ({type(exc).__name__}).", verdict.MACHINE)
    finally:
        link = None  # noqa: F841 (the credential is dropped as soon as the browser has it)
    landed = bare(landed)
    page = site.page(landed)
    if page is None:
        where = urlparse(landed).hostname or "another site"
        return failed(f"{FAILED} ended outside your app, on {where}: its link should lead back to your app once "
                      "signed in (Supabase: redirectTo; Clerk: your sign-in page).", verdict.SETUP)
    if passwords:
        return failed(f"{FAILED} opened, but {site.label(page)} still asks for a password: the link didn’t sign in "
                      "(already used, expired, or for another environment).", verdict.SETUP)
    check = spec.get("check") or page
    row = session_holds(config, persona, state, check, site=site, checker=checker)
    if row.cause == verdict.MACHINE:
        return failed(row.detail, verdict.MACHINE)
    if not row.passed:
        return failed(f"{FAILED} opened, but {site.label(check)} then said signed out: the session it made didn’t "
                      "hold.", verdict.SETUP)
    result = _result(name, persona, True, "passed", "", "", started, clock)
    return {**result, "saved_signin": False, "signed_in_by": "login_link", "final_url": site.url(check)}, state


def open_link(link, site, *, browser_factory=None, settle_s=SETTLE_S):
    """Open the sign-in link in a fresh browser context, let it finish, keep its session.
    -> (session state, the address it landed on, visible password fields there). No screenshot, no recording."""
    from .access import _browser_factory
    from .pagewait import chain, track_network, wait_until_stable
    from .runner import PASSWORD_FIELDS
    from .session import capture

    browser = (browser_factory or _browser_factory)(link, isolated=True, prepare=chain(track_network))
    try:
        wait_until_stable(browser, max_s=settle_s)
        landed = browser.evaluate("location.href") or ""
        passwords = browser.evaluate(PASSWORD_FIELDS) or 0
        origins = tuple(f"{urlparse(u).scheme}://{urlparse(u).netloc}" for _, u in site.parts)
        state = capture(browser, origins)
    finally:
        browser.close()
    return state, landed, passwords
