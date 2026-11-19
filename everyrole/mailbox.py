"""EveryRole's inboxes, as the engine reads them: the signed addresses, the emails kept for each, and what to do
with one (the link to open, the code to type).

An address is <name>.<project id>.<signature>@<inbox domain>. The signature is an HMAC over the project and the name:
it can't be guessed and needs nothing stored. EveryRole's mail server receives the mail and keeps each
email as a JSON file in <inbox folder>/<name>/; the engine only reads those files, so a run needs no server code.

A journey that signs up gets its own name (`new-<run>...`), so its emails are its own. A run learns where the
inboxes are from its environment (set where EveryRole's inboxes are); without it, signing up can't be tested here.
"""

import email
import email.policy
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

DOMAIN_ENV, PROJECT_ENV, KEY_ENV, DIR_ENV = ("EVERYROLE_INBOX_DOMAIN", "EVERYROLE_INBOX_PROJECT", "EVERYROLE_INBOX_KEY",
                                             "EVERYROLE_INBOX_DIR")
ATTEMPT_ENV = "EVERYROLE_ATTEMPT"  # which try of a test this is (suite.py): a retry signs up as someone new
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
URL = re.compile(r"https?://[^\s\"'<>()\]]+", re.I)
# a one-time code: 4 to 8 digits (or letters and digits, 6 to 8) standing alone, near a word that says it's a code
CODE_WORDS = re.compile(r"\b(code|otp|one[- ]time|verification|verify|pin|passcode)\b", re.I)
CODE = re.compile(r"(?<![\w/.-])(\d{4,8}|(?=[A-Z0-9]*\d)[A-Z0-9]{6,8})(?![\w/-]|\.\w)")  # "is 482913." counts
# words that mark the link to follow in an invitation or a sign-in email (not the logo, the help page or unsubscribe)
ACTION_WORDS = re.compile(r"invit|accept|join|sign[-_]?up|signup|register|verif|confirm|magic|token|activate|onboard|"
                          r"auth|login|sign[-_]?in|callback|set[-_]?password|reset", re.I)


def signing_key(server_secret):
    """The key every inbox signature is made with, from the server's secret (the server never hands out its secret:
    a run gets this key only)."""
    return hashlib.sha256(f"inbox:{server_secret}".encode()).hexdigest()


def signature(key, project_id, name):
    return hmac.new(bytes.fromhex(key), f"{int(project_id)}:{name}".encode(), hashlib.sha256).hexdigest()[:10]


def address(domain, key, project_id, name):
    """The address of one inbox, or None when the name can't be one."""
    name = (name or "").lower()
    if not (domain and key and NAME.match(name)):
        return None
    return f"{name}.{int(project_id)}.{signature(key, project_id, name)}@{domain}"


def owner(domain, key, addr):
    """-> (project_id, name) for one of our addresses, else None (unknown, forged or another domain)."""
    local, _, at = (addr or "").strip().lower().rpartition("@")
    if not domain or at != domain.lower():
        return None
    match = re.match(r"^([a-z0-9][a-z0-9_-]{0,39})\.(\d+)\.([0-9a-f]{10})$", local.split("+")[0])
    if not match:
        return None
    name, project_id, signed = match.group(1), int(match.group(2)), match.group(3)
    return (project_id, name) if hmac.compare_digest(signed, signature(key, project_id, name)) else None


# ---------------------------------------------------------------- a new account for one journey


def new_account(env, run_id, flow_name):
    """A brand-new identity for one run of one journey: {email, password, name, inbox} (inbox: the folder its emails
    land in), or None when this run has no inbox to sign up with. Its own inbox name per run and journey, so the
    emails a journey waits for are its own."""
    domain, key, project, folder = (env.get(n, "") for n in (DOMAIN_ENV, KEY_ENV, PROJECT_ENV, DIR_ENV))
    if not (domain and key and project and folder):
        return None
    tag = hashlib.sha256(f"{run_id}:{flow_name}:{env.get(ATTEMPT_ENV, '1')}".encode()).hexdigest()[:6]
    name = f"new-{re.sub(r'[^a-z0-9]', '', (run_id or '').lower())[:16]}{tag}"
    addr = address(domain, key, project, name)
    if not addr:
        return None
    return {"email": addr, "password": "Er-" + secrets.token_urlsafe(12) + "-7a", "name": "EveryRole Test",
            "inbox": str(Path(folder) / name)}


# ---------------------------------------------------------------- reading emails


def parse_message(raw):
    """Raw RFC 822 bytes -> {from, subject, received, text, links, codes} (text is the readable body)."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    plain, rich = [], []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            continue
        (rich if part.get_content_subtype() == "html" else plain).append(content)
    hrefs = [html.unescape(h) for body in rich for h in re.findall(r'href\s*=\s*["\']([^"\']+)', body, re.I)]
    text = "\n".join(plain) or _html_text("\n".join(rich))
    links = []
    for link in hrefs + URL.findall(text):
        link = link.rstrip(".,;:!?")
        if link.lower().startswith(("http://", "https://")) and link not in links:
            links.append(link)
    codes = []
    for line in text.splitlines():
        if CODE_WORDS.search(line) or (codes == [] and len(line.split()) <= 2):
            codes += [c for c in CODE.findall(line) if c not in codes]
    try:
        received = parsedate_to_datetime(message["date"]).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        received = datetime.now(timezone.utc).isoformat()
    return {"from": parseaddr(str(message["from"] or ""))[1], "subject": str(message["subject"] or "").strip(),
            "received": received, "text": text.strip()[:20000], "links": links[:50], "codes": codes[:5]}


def action_link(message, app_hosts):
    """The link to follow in an invitation or a confirmation: on one of the app's own addresses (or a subdomain of
    them) and with words of an action in it; else any link with those words (a sign-in service's page)."""
    def on_app(link):
        host = (urlparse(link).hostname or "").lower()
        return any(host == h or host.endswith("." + h) or h.endswith("." + host) for h in app_hosts if h)

    links = [link for link in message.get("links") or [] if ACTION_WORDS.search(urlparse(link).path + "?" +
                                                                                 urlparse(link).query)]
    return next((link for link in links if on_app(link)), None) or next(iter(links), None)


def messages(folder):
    """The emails kept in one inbox folder, oldest first, each with its file name as `id`."""
    folder = Path(folder) if folder else None
    found = []
    for path in sorted(folder.glob("*.json")) if folder and folder.is_dir() else []:
        try:
            found.append({**json.loads(path.read_text(encoding="utf-8")), "id": path.stem})
        except (OSError, ValueError):
            continue
    return found


def wait_for_new(folder, seen, *, wait_s, clock=time.monotonic, sleep=time.sleep, poll_s=3.0):
    """The first email in `folder` whose id is not in `seen`, waiting up to `wait_s`; None if none came."""
    deadline = clock() + max(wait_s, 0)
    while True:
        fresh = [m for m in messages(folder) if m["id"] not in seen]
        if fresh:
            return fresh[0]
        if clock() >= deadline:
            return None
        sleep(poll_s)


def _html_text(markup):
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))
