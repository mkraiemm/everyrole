"""Code-enforced safety: `forbid` and `allowed_hosts` are checked by the runner, never delegated to a prompt."""

import re
from urllib.parse import urlparse


class GuardViolation(Exception):
    status = "error"


class ForbiddenAction(GuardViolation):
    status = "forbidden_action"


class LeftAllowedHosts(GuardViolation):
    status = "left_allowed_hosts"


class Guard:
    def __init__(self, forbid=(), allowed_hosts=()):
        # Whole-word, case-insensitive: "Pay" blocks "Pay now" but not "Display" or "Payment history".
        self._forbid = [(p, re.compile(rf"(?<!\w){re.escape(p)}(?!\w)", re.IGNORECASE)) for p in forbid]
        self._hosts = tuple(h.lower().lstrip(".") for h in allowed_hosts if h)

    def check_action(self, action, _page=None, _text=None):
        """Engine `before_execute` hook: raise before any browser input happens."""
        label = action.get("label", "")
        for pattern, regex in self._forbid:
            if regex.search(label):
                raise ForbiddenAction(f"Refused to {action.get('kind', 'act')} on {label!r} (forbid: {pattern!r})")

    def check_url(self, url):
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not self._hosts:
            return
        host = (parsed.hostname or "").lower()
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self._hosts):
            raise LeftAllowedHosts(f"Navigated to {host!r}, outside allowed hosts {list(self._hosts)}")
