"""Access matrix: can this persona open this page? Deterministic, zero model calls.

A page counts as DENIED when the browser ends up on a different page than the one requested (the app
redirected the user away: to the home page, a login page, an upgrade page...), when the app answered the page
itself with a refusal (HTTP 401, 403, 404 or 410: "not yours", or "no such page" to hide it), or when any of the
app's optional `denied_when` checks passes on it ("no access" text shown in place). It is ALLOWED only when
`access_settle_s` has passed without a denial AND the page has finished loading (no request in flight, nothing
changing): a page still waiting for the app's answer may yet send the user away, so it is not judged ("unsure",
shown as couldn't check) rather than called open. All pages for one persona are opened in parallel tabs and
watched together. A page that could not be read, or whose answer disagrees with the rule, is checked again
alone once: parallel tabs on a busy machine are slow, and a slow page must not look like an open door. (A run
also checks the pages still not judged once more after everything else, one at a time: suite.py.)
"""

import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from . import assertions, inject, verdict
from .pagewait import Steadiness, chain, describe_pending, read_pending, read_state, track_network
from .runner import read_facts
from .session import restorer
from .spec import site_of

# the app's own answer for the page says no: not signed in, forbidden, or hidden as missing
DENIED_STATUSES = (401, 403, 404, 410)
DOCUMENT_STATUS = "(performance.getEntriesByType('navigation')[0] || {}).responseStatus || 0"


@dataclass
class AccessResult:
    path: str
    persona: str
    expected: str  # "allow" | "deny"
    observed: str  # "allowed" | "denied" | "unsure" (never finished loading) | "error"
    passed: bool
    url: str = ""
    detail: str = ""
    cause: str = ""  # why it did not pass: app | machine (see verdict.py)

    def __post_init__(self):
        if not self.passed and not self.cause:
            self.cause = verdict.MACHINE if self.observed in ("error", "unsure") else verdict.APP

    def as_dict(self):
        return asdict(self)


def _browser_factory(url, **options):
    from jev_ultrafast.browser import Browser

    return Browser(url, **options)


def expected_rules(config, persona):
    """(path, expected) pairs this persona has a rule for."""
    rules = []
    for path, rule in config.access.items():
        if persona in rule.get("allow", []):
            rules.append((path, "allow"))
        elif persona in rule.get("deny", []):
            rules.append((path, "deny"))
    return rules


class AccessChecker:
    def __init__(self, *, browser_factory=_browser_factory, facts_reader=read_facts, clock=time.monotonic,
                 sleep=time.sleep, poll_s=0.25, pace=None, page_state=None, stable_reads=3, quiet_tail_s=2.0,
                 pending_reader=None, status_reader=None):
        self._browser = browser_factory
        self._status = status_reader or _document_status  # browser -> the HTTP status the page was answered with
        self._page_state = page_state or _where_and_quiet  # browser -> (state, idle) or None
        self._pending = pending_reader or read_pending  # browser -> what a page still waits for
        self._stable_reads = stable_reads
        # a page that keeps polling in the background is never quiet for long: it has answered when it was quiet
        # at some moment near the end of the window (a page still waiting for the app's answer never is)
        self._quiet_tail_s = quiet_tail_s
        self._open_waits_s = (1.0, 2.0)  # between tries of opening a page our browser helper dropped
        self._pace = pace  # the job's network pace (see pace.py): each page waits for room to load
        self._facts = facts_reader
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s

    def run(self, *, base_url=None, site=None, persona, rules, denied_when, settle_s, session_state=None,
            _retry=False):
        """`rules`: (page, expected) pairs; a page is "/path" or "part:/path" on another part of the app."""
        site = site_of(base_url, site)
        checks = assertions.build(denied_when)
        options = {"isolated": True,
                   "prepare": chain(restorer(session_state) if session_state else None, inject.from_env(),
                                    track_network)}
        tabs, results = [], {}
        try:
            for path, expected in rules:
                try:
                    tab = self._open(site.url(path), options)
                    tabs.append((path, expected, tab))
                    if getattr(tab, "context", None) and "context" not in options:
                        # the batch's other pages share this one's session: same cookies, fewer connections
                        options = {**{k: v for k, v in options.items() if k != "isolated"}, "context": tab.context}
                except Exception as exc:
                    results[path] = AccessResult(path, persona, expected, "error", False,
                                                 detail=f"could not open: {type(exc).__name__}: {exc}")
            deadline = self._clock() + settle_s
            pending = {path: (expected, browser) for path, expected, browser in tabs}
            last = {}
            steady = {path: Steadiness(self._stable_reads) for path in pending}
            settled, quiet_at = {}, {}
            while pending:
                for path, (expected, browser) in list(pending.items()):
                    try:
                        page = browser.observe(screenshot=False)
                        facts = self._facts(browser, page)
                    except Exception:
                        continue  # mid-redirect read; try again next poll
                    last[path] = facts
                    hits = [o for o in assertions.evaluate(checks, facts) if o.passed]
                    landed, wanted = _page(site, facts.url), _page(site, site.url(path))
                    status = self._status(browser) if landed == wanted else 0
                    if hits or landed != wanted or status in DENIED_STATUSES:
                        where = landed or urlparse(facts.url).hostname or facts.url  # outside the app: its host
                        why = (f"{hits[0].kind} “{hits[0].expected}”" if hits else f"redirected to {where}"
                               if landed != wanted else f"answered HTTP {status}")
                        results[path] = self._result(path, persona, expected, "denied", facts, why)
                        del pending[path]
                        continue
                    reading = self._page_state(browser)
                    settled[path] = steady[path].read(reading)
                    if reading is not None and reading[1]:
                        quiet_at[path] = self._clock()
                if not pending or self._clock() >= deadline:
                    break
                self._sleep(self._poll_s)
            for path, (expected, _browser) in pending.items():
                facts = last.get(path)
                if facts is None:
                    results[path] = AccessResult(path, persona, expected, "error", False,
                                                 detail="page never became readable")
                elif settled.get(path) or quiet_at.get(path, float("-inf")) >= deadline - self._quiet_tail_s:
                    results[path] = self._result(path, persona, expected, "allowed", facts,
                                                 f"no denial signal within {settle_s}s")
                else:
                    waiting = describe_pending(self._pending(_browser))
                    results[path] = AccessResult(path, persona, expected, "unsure", False, url=facts.url,
                                                 detail=f"still loading after {settle_s}s: not judged"
                                                        + (f" ({waiting})" if waiting else ""))
        finally:
            for _path, _expected, browser in reversed(tabs):  # the tab that owns the shared session goes last
                try:
                    browser.close()
                except Exception:
                    pass
        for path, expected in ([] if _retry else rules):
            if not results[path].passed:  # could not run, or disagrees: check again alone before reporting
                first = results[path]
                results[path] = self.run(site=site, persona=persona, rules=[(path, expected)],
                                         denied_when=denied_when, settle_s=settle_s, session_state=session_state,
                                         _retry=True)[0]
                if results[path].passed:
                    results[path].detail = f"passed when checked again alone (first: {first.detail})"
        return [results[path] for path, _ in rules]

    def _open(self, url, options):
        """Open one page. Our browser helper sometimes drops or refuses a connection while many tabs open at once:
        a short, growing wait and a new try (never a hammering loop, never the app's fault)."""
        for attempt, wait_s in enumerate(self._open_waits_s + (None,)):
            try:
                with self._pace.page(url) if self._pace else nullcontext():
                    return self._browser(url, **options)
            except (ConnectionError, FileNotFoundError, TimeoutError, OSError, RuntimeError):  # browser side
                if wait_s is None:
                    raise
                self._sleep(wait_s)

    @staticmethod
    def _result(path, persona, expected, observed, facts, detail):
        passed = (expected == "allow") == (observed == "allowed")
        return AccessResult(path, persona, expected, observed, passed, url=facts.url, detail=detail)


def _document_status(browser):
    """The HTTP status the page itself was answered with; 0 when unknown (mid-navigation, an old browser)."""
    try:
        return int(browser.evaluate(DOCUMENT_STATUS) or 0)
    except Exception:
        return 0


def _where_and_quiet(browser):
    """For access only the address and pending requests matter: a page that changes its text (a clock, a live
    counter) has still answered, while one waiting for the app's answer has a request in flight."""
    reading = read_state(browser)
    if reading is None:
        return None
    state, idle = reading
    return (state[0], state[3]) if isinstance(state, list) and len(state) > 3 else state, idle


def _page(site, url):
    """The page a URL is in the app, without a trailing slash; None outside the app."""
    page = site.page(url)
    if page is None:
        return None
    return page.rstrip("/") if page.rstrip("/") and not page.endswith(":/") else page
