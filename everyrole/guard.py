"""Code-enforced safety: `forbid` and `allowed_hosts` are checked by the runner, never delegated to a prompt."""

import re
from urllib.parse import unquote, urlparse


class GuardViolation(Exception):
    status = "error"


class ForbiddenAction(GuardViolation):
    status = "forbidden_action"


class LeftAllowedHosts(GuardViolation):
    status = "left_allowed_hosts"


def address_words(url):
    """The words of an address's path: "/delete_account", "/account/delete-account" and "/deleteAccount" all read
    "delete account". Only the path: a query or a fragment names a value, not what opening the address does."""
    path = unquote(urlparse(url or "").path or "")
    path = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", path)  # camelCase
    return " ".join(w for w in re.split(r"[/_\-.\s]+", path) if w)


class Guard:
    def __init__(self, forbid=(), allowed_hosts=()):
        # Whole-word, case-insensitive: "Pay" blocks "Pay now" but not "Display" or "Payment history".
        self._forbid = [(p, re.compile(rf"(?<!\w){re.escape(p)}(?!\w)", re.IGNORECASE)) for p in forbid]
        self._hosts = tuple(h.lower().lstrip(".") for h in allowed_hosts if h)

    def forbidden(self, *texts):
        """The first never-click word in these words (a label, an address's words), or None."""
        for text in texts:
            for pattern, regex in self._forbid:
                if text and regex.search(text):
                    return pattern
        return None

    def forbidden_link(self, *names, href=""):
        """The never-click word a link carries in its words (its text, its aria-label) or in its address's path
        ("/delete_account"), or None. Opening a plain link can change data as surely as a button (a GET that deletes
        the account, signs out, unsubscribes): the same words refuse it."""
        return self.forbidden(*names, address_words(href) if href else "")

    def check_action(self, action, _page=None, _text=None):
        """Engine `before_execute` hook: raise before any browser input happens. A link is refused by its words or
        by the words of the address it opens."""
        label, href = action.get("label", ""), action.get("href") or ""
        pattern = self.forbidden(label)
        if pattern:
            raise ForbiddenAction(f"Refused to {action.get('kind', 'act')} on {label!r} (forbid: {pattern!r})")
        pattern = self.forbidden_link(href=href) if href else None
        if pattern:
            raise ForbiddenAction(f"Refused to {action.get('kind', 'act')} on {label!r}, a link to {href!r} "
                                  f"(forbid: {pattern!r})")

    def check_url(self, url):
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not self._hosts or "*" in self._hosts:
            return  # "*": any address (a purchase journey goes to the payment provider's page, customer_journey.py)
        host = (parsed.hostname or "").lower()
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self._hosts):
            raise LeftAllowedHosts(f"Navigated to {host!r}, outside allowed hosts {list(self._hosts)}")
