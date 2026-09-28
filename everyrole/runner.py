"""Run one flow: agent + guard + secret-safe values + budgets + deterministic expectations.

Status is decided ONLY by `expect` checks on a fresh read of the page, never by the agent's DONE:
  passed | failed_expect | forbidden_action | left_allowed_hosts | budget_exceeded | blocked | error

Each flow runs in its own isolated browser context. A persona's saved session is restored before the
first page load; a passing login flow saves its session for the others.
`expect_denied` flows never stop early: the agent must really try, and only the checks decide.
Checks are judged on a SETTLED page: when they pass, the runner waits for the page to stop changing and
re-checks, so text that only exists while loading ("Opening…", skeletons) never passes a flow.
"""

import base64
import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from jev_ultrafast.agent import BudgetExceeded

from . import assertions, inject, usage, verdict, writeguard
from .guard import Guard, GuardViolation
from .pagewait import chain, track_network, wait_until_stable
from .redact import Redactor
from .session import capture, restorer, save
from .values import ValueChooser

STEP_KEYS = ("step", "action", "kind", "text", "url", "probability", "confidence", "latency_ms", "executed_ms")


@dataclass
class FlowResult:
    name: str
    status: str
    passed: bool
    duration_ms: int
    expectations: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    decisions: int = 0
    input_tokens: int = 0
    usage: dict = field(default_factory=dict)  # {"engine": {...}, "text": {...}} as the gateway reported it
    final_url: str = ""
    error: str = ""
    warnings: list = field(default_factory=list)
    screenshot: str = ""
    source: str = ""
    persona: str = ""
    expect_denied: bool = False
    session_saved: bool = False
    video: str = ""  # replay MP4, when recording was requested and ffmpeg is available
    timeline: list = field(default_factory=list)  # [{ms, label}] captions for the replay
    cause: str = ""  # why it did not pass: app | machine (see verdict.py)
    poster: str = ""  # where the test ended (the replay's last frame), for passing tests too
    challenge: str = ""  # a security check (CAPTCHA) found on the page where it stopped, e.g. "Cloudflare Turnstile"

    def as_dict(self):
        return asdict(self)


NO_PASSWORD_FORM = ("This sign-in never showed a password field: it goes through another site (like Google or "
                    "GitHub), an emailed link or code, or a form EveryRole can't use. Sign in yourself once and "
                    "save that session as the user type's `saved_signin`: EveryRole reuses it.")

PASSWORD_FIELDS = r"""(() => [document, ...[...document.querySelectorAll('iframe')]
  .map(f => { try { return f.contentDocument; } catch (e) { return null; } }).filter(Boolean)]
  .flatMap(d => [...d.querySelectorAll('input[type="password"]')])
  .filter(e => e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true)
  .length)()"""

PAGE_CONTROLS = r"""(() => {
  const visible = e => e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true;
  const text = e => (e.getAttribute('aria-label') || e.innerText || e.value || e.getAttribute('placeholder') ||
    e.getAttribute('title') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const labelOf = e => {
    const id = e.getAttribute('id');
    const label = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
    return label ? label.innerText.replace(/\s+/g, ' ').trim() : '';
  };
  const found = [...document.querySelectorAll('a[href],button,[role="button"],[role="link"],[role="tab"],' +
    'input:not([type="hidden"]),textarea,select')].filter(visible).flatMap(e => [text(e), labelOf(e)]);
  return JSON.stringify([...new Set(found.filter(Boolean))]);
})()"""


def read_facts(browser, page):
    """Fresh URL and full rendered text from the browser; labels of every visible control on the whole page
    (not only the part in view), plus the latest observation's labels."""
    url = browser.evaluate("location.href") or page.get("url", "")
    text = browser.evaluate("document.body ? document.body.innerText : ''") or ""
    try:
        controls = json.loads(browser.evaluate(PAGE_CONTROLS) or "[]")
    except (TypeError, ValueError):
        controls = []
    labels = tuple(dict.fromkeys([*(a.get("label", "") for a in page.get("actions", [])), *controls]))
    passwords = browser.evaluate(PASSWORD_FIELDS) or 0
    return assertions.PageFacts(url=url, text=text, labels=labels, blocked=tuple(writeguard.blocked(browser)),
                                passwords=passwords)


def _agent_factory(url, goal, **hooks):
    from jev_ultrafast import Agent

    return Agent(url, goal, **hooks)


def _outbound_filters():
    from jev_ultrafast import model

    return model.OUTBOUND_FILTERS


def _release_all(pace, tickets):
    while tickets:
        pace.release(tickets.pop())


class FlowRunner:
    def __init__(
        self,
        *,
        agent_factory=_agent_factory,
        facts_reader=read_facts,
        outbound_filters=_outbound_filters,
        value_chooser=ValueChooser,
        clock=time.monotonic,
        sleep=time.sleep,
        poll_s=0.25,
        wait_stable=wait_until_stable,
        pace=None,
    ):
        self._agent_factory = agent_factory
        self._pace = pace  # the job's network pace (see pace.py): the start page waits for room to load
        self._facts = facts_reader
        self._filters = outbound_filters
        self._value_chooser = value_chooser
        self._clock = clock
        self._sleep = sleep
        self._poll_s = poll_s
        self._wait_stable = wait_stable

    def _release(self, tickets):
        if self._pace:
            _release_all(self._pace, tickets)

    def run(self, flow, output_dir=None, *, session_state=None, save_session_to=None, record_dir=None, site=None):
        """`site`: every address of the app, so a saved session keeps what each part stored."""
        started = self._clock()
        self._passwords_seen = 0  # the most password fields any page of this run showed
        self._origins = tuple(f"{urlparse(u).scheme}://{urlparse(u).netloc}" for _, u in site.parts) if site else ()
        self._ignore_saves = flow.ignore_saves
        if flow.block_writes and flow.submit_settle_s > flow.settle_s:
            flow = replace(flow, settle_s=flow.submit_settle_s)  # submits trigger slow work before saving
        redactor = Redactor(flow.secrets)
        guard = Guard(flow.forbid, flow.allowed_hosts)
        checks = assertions.build(flow.expect)
        filters = self._filters()
        filters.append(redactor.apply)
        result = FlowResult(
            name=flow.name, status="error", passed=False, duration_ms=0, source=flow.source,
            persona=flow.persona, expect_denied=flow.expect_denied,
        )
        browser_options = {"isolated": True}
        tickets = []  # the start page's place in the job's network pace, held until it has loaded
        # restore the session (if any), then track in-flight requests so "loaded" means really loaded
        browser_options["prepare"] = chain(
            (lambda browser: tickets.append(self._pace.acquire(flow.start))) if self._pace else None,
            restorer(session_state) if session_state else None,
            track_network,
            writeguard.block_writes if flow.block_writes else None,  # form-only: nothing can be saved
            inject.from_env(),  # only with `everyrole run --inject`: a planted bug
        )
        # the first decision is made on the loaded start page, not on a spinner or skeleton
        def settle(browser):
            try:
                self._wait_stable(browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                                  sleep=self._sleep)
            finally:
                self._release(tickets)

        browser_options["settle"] = settle
        recording = {"record_dir": str(record_dir)} if record_dir else {}
        agent = None
        outcomes = []
        try:
            agent = self._agent_factory(
                flow.start,
                flow.goal,
                max_steps=flow.max_steps,
                text_provider=self._value_chooser(flow.values, tuple(flow.secrets.values())),
                before_execute=guard.check_action,
                browser_options=browser_options,
                stop_needs_fresh_page=False,  # EveryRole's own checks decide; "done"/"blocked" make no input
                after_action=lambda browser: self._wait_stable(
                    browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock, sleep=self._sleep),
                **recording,
            )
            result.status, outcomes = self._drive(agent, flow, guard, checks, started)
        except GuardViolation as exc:
            result.status, result.error = exc.status, str(exc)
        except BudgetExceeded as exc:  # the goal was not reached within the test's budget: a result, not a crash
            result.status, result.error = "budget_exceeded", str(exc) + _stale_note(agent)
        except Exception as exc:  # provider/browser failures; expectations below may still prove success
            result.status, result.error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if agent is not None:
                    outcomes = self._final_outcomes(agent, checks, outcomes)
                    self._finish(agent, result, outcomes, output_dir, flow)
                    if result.passed and save_session_to:
                        self._save_session(agent, result, save_session_to)
                    if record_dir:
                        self._final_frame(agent, record_dir)
            finally:
                self._release(tickets)  # a start page that never loaded gives its place back
                filters.remove(redactor.apply)
                if agent is not None:
                    try:
                        agent.close()
                    except Exception as exc:
                        result.warnings.append(f"close failed: {type(exc).__name__}: {exc}")
        result.duration_ms = round((self._clock() - started) * 1000)
        # An exception inside the run (browser, model provider) says nothing about the app.
        result.cause = "" if result.passed else (verdict.MACHINE if result.status == "error" else verdict.APP)
        if result.challenge and not result.passed:
            from .challenge import message

            result.cause, result.error = verdict.SIGNIN, message(result.challenge)
        elif flow.is_login and result.cause == verdict.APP and not self._passwords_seen:
            # no password field ever appeared: the sign-in goes through another site (Google, GitHub...), an
            # emailed link or code, or a form EveryRole cannot reach. A person signs in once; never an app failure.
            result.cause, result.error = verdict.SIGNIN, NO_PASSWORD_FORM
        if record_dir and agent is not None:
            self._make_video(result, record_dir)
        return self._redacted(result, redactor)

    PAGE_POSITION = "[scrollY, innerHeight, document.documentElement.scrollHeight]"

    def _scroll_down(self, browser):
        """Scroll one screen with a real mouse wheel (smooth-scroll libraries ignore window.scrollBy).
        -> True if the page actually moved."""
        before, height, total = browser.evaluate(self.PAGE_POSITION)
        if before + height >= total - 4:
            return False
        browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=height // 2, y=height // 2, deltaX=0,
                     deltaY=round(height * 0.8))
        self._wait_stable(browser, max_s=2, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
        return browser.evaluate(self.PAGE_POSITION)[0] > before

    def _scroll_to_top(self, browser):
        """Back to the top with a real mouse wheel (headers, menus and cart icons live there).
        -> True if the page actually moved."""
        before, height, _total = browser.evaluate(self.PAGE_POSITION)
        if before <= 4:
            return False
        browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=height // 2, y=height // 2, deltaX=0,
                     deltaY=-round(before + height))
        self._wait_stable(browser, max_s=2, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
        return browser.evaluate(self.PAGE_POSITION)[0] < before

    def _resume_after_scroll(self, agent, flow, scrolls, may_look=True, may_return=True):
        """The agent gave up. If the page has changed since it decided (a slow navigation landed), let it look
        again; otherwise, if the page continues below the fold (it only sees what is on screen), scroll; once it
        can't (or may not) scroll further down, go back to the top once: what it needs may be up there.
        -> "look" | "scroll" | "top" | None (stop). Pages with live content (tickers, rotating headlines) change
        all the time, so a second give-up in a row never just looks again: "the page changed" must not use up
        the scrolls."""
        if agent.snapshot()["status"] != "blocked" or (scrolls >= flow.scroll_retries and not may_return):
            return None
        try:
            self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                              sleep=self._sleep)
            seen = agent.state.get("page", {}).get("fingerprint")
            fresh = agent.browser.observe(screenshot=agent.screenshots)
            if may_look and seen and fresh.get("fingerprint") and fresh["fingerprint"] != seen:
                agent.state["page"], agent.state["status"] = fresh, "ready"
                return "look"
            if scrolls < flow.scroll_retries and self._scroll_down(agent.browser):
                how = "scroll"
            elif may_return and self._scroll_to_top(agent.browser):
                how = "top"
            else:
                return None
            self._wait_stable(agent.browser, max_s=max(flow.settle_s, 1), poll_s=self._poll_s, clock=self._clock,
                              sleep=self._sleep)
            agent.state["page"] = agent.browser.observe(screenshot=agent.screenshots)
            agent.state["status"] = "ready"
            return how
        except Exception:
            return None

    def _drive(self, agent, flow, guard, checks, started):
        scrolls, looked, returned = 0, False, False
        while True:
            result = self._drive_once(agent, flow, guard, checks, started)
            if result is not None:
                return result
            how = self._resume_after_scroll(agent, flow, scrolls, may_look=not looked, may_return=not returned)
            if how is None:
                break
            looked = how == "look"
            scrolls += how == "scroll"
            returned = returned or how == "top"
        return self._conclude(agent, flow, checks)

    def _drive_once(self, agent, flow, guard, checks, started):
        """Run the agent until it stops. -> (status, outcomes) if the flow is decided, else None."""
        outcomes = []
        for state in agent.run():
            guard.check_url(state["page"]["url"])
            outcomes = assertions.evaluate(checks, self._read(agent.browser, state["page"]))
            if all(o.passed for o in outcomes) and not flow.expect_denied:
                settled = self._settled_outcomes(agent, checks, flow.settle_s)
                if settled and all(o.passed for o in settled):
                    return "passed", settled  # early stop: the success condition holds on the loaded page
                outcomes = settled or outcomes  # it only held while loading; keep going
            if self._clock() - started > flow.timeout_s:
                return "budget_exceeded", outcomes
        return None

    def _conclude(self, agent, flow, checks):
        outcomes = []
        final = agent.snapshot()
        if final["status"] == "blocked" and len(final["history"]) >= flow.max_steps and not flow.expect_denied:
            return "budget_exceeded", outcomes
        # The agent stopped; the page may still be redirecting or rendering. Re-check for settle_s.
        outcomes = self._settle(agent, checks, flow.settle_s) or outcomes
        if outcomes and all(o.passed for o in outcomes):
            return "passed", outcomes
        return ("blocked" if final["status"] == "blocked" else "failed_expect"), outcomes

    def _settled_outcomes(self, agent, checks, settle_s):
        """Wait for the page to finish loading (up to settle_s), then evaluate the checks on it."""
        self._wait_stable(agent.browser, max_s=settle_s, poll_s=self._poll_s, clock=self._clock, sleep=self._sleep)
        try:
            page = agent.browser.observe(screenshot=False)
            return assertions.evaluate(checks, self._read(agent.browser, page))
        except Exception:
            return []

    def _settle(self, agent, checks, settle_s):
        deadline = self._clock() + settle_s
        outcomes = []
        while True:
            try:
                page = agent.browser.observe(screenshot=False)
                outcomes = assertions.evaluate(checks, self._read(agent.browser, page))
            except Exception:
                pass  # mid-navigation reads can fail; keep polling until the deadline
            if (outcomes and all(o.passed for o in outcomes)) or self._clock() >= deadline:
                return outcomes
            self._sleep(self._poll_s)

    def _final_outcomes(self, agent, checks, outcomes):
        """Re-check on a fresh read so the reported state is the page as it is now."""
        try:
            page = agent.browser.observe(screenshot=False)
            return assertions.evaluate(checks, self._read(agent.browser, page))
        except Exception:
            return outcomes

    @staticmethod
    def _final_frame(agent, record_dir):
        """The page the checks were judged on, as the replay's last frame."""
        started = agent.snapshot().get("started_at")
        existing = [int(p.stem) for p in Path(record_dir).glob("*.jpg") if p.stem.isdigit()]
        at = round((time.perf_counter() - started) * 1000) if started else max(existing, default=0) + 500
        try:
            data = agent.browser.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
        except Exception:
            return
        Path(record_dir).mkdir(parents=True, exist_ok=True)
        (Path(record_dir) / f"{max(at, max(existing, default=0) + 1):06d}.jpg").write_bytes(base64.b64decode(data))

    @staticmethod
    def _make_video(result, record_dir):
        from .video import make_video, timeline

        record_dir = Path(record_dir)
        # Read the final frame's time before encoding: encoding removes the frames.
        existing = [int(p.stem) for p in record_dir.glob("*.jpg") if p.stem.isdigit()] if record_dir.is_dir() else []
        last_step = max((s.get("executed_ms") or 0 for s in result.steps), default=0)
        result.timeline = timeline(result.steps, max([last_step, *existing]) or result.duration_ms)
        if existing:  # the page the checks were judged on, kept as a picture before the frames are encoded
            poster = record_dir.with_suffix(".jpg")
            last = max((p for p in record_dir.glob("*.jpg") if p.stem.isdigit()), key=lambda p: int(p.stem))
            poster.write_bytes(last.read_bytes())
            result.poster = str(poster)
        video = make_video(record_dir, record_dir.with_suffix(".mp4"))
        if video:
            result.video = video

    def _read(self, browser, page):
        """Page facts, without the flow's known background writes (analytics/metrics are not save attempts)."""
        facts = self._facts(browser, page)
        self._passwords_seen = max(getattr(self, "_passwords_seen", 0), facts.passwords)
        ignore = getattr(self, "_ignore_saves", ())
        if not ignore or not facts.blocked:
            return facts
        kept = tuple(b for b in facts.blocked if not any(fragment in b[1] for fragment in ignore))
        return replace(facts, blocked=kept)

    def _save_session(self, agent, result, path):
        try:
            save(capture(agent.browser, getattr(self, "_origins", ())), path)
            result.session_saved = True
        except Exception as exc:
            result.passed, result.status = False, "error"
            result.error = f"signed in, but the session could not be saved: {type(exc).__name__}: {exc}"

    def _finish(self, agent, result, outcomes, output_dir, flow):
        snapshot = agent.snapshot()
        all_passed = bool(outcomes) and all(o.passed for o in outcomes)
        # Denial flows: the agent giving up or running out of steps is the expected outcome.
        recoverable = {"error", "failed_expect", "blocked"} | ({"budget_exceeded"} if flow.expect_denied else set())
        if result.status in recoverable and all_passed:
            # The page satisfies every expectation even though the agent errored or gave up.
            if result.error:
                result.warnings.append(f"agent error after success: {result.error}")
            result.status, result.error = "passed", ""
        elif result.status == "passed" and not all_passed:
            result.status = "failed_expect"
            result.warnings.append("expectations passed during the run but not on the final page")
        result.passed = result.status == "passed"
        if not result.passed:  # only a person can pass a security check: say so instead of blaming the app
            from .challenge import detect

            result.challenge = detect(agent.browser)
        result.expectations = [o.as_dict() for o in outcomes]
        result.steps = [{k: h.get(k) for k in STEP_KEYS} for h in snapshot["history"]]
        result.decisions = len(snapshot["decisions"])
        result.usage = {"engine": usage.of_calls(snapshot["decisions"]),
                        "text": usage.of_calls(snapshot.get("text_calls"))}
        result.input_tokens = sum(d.get("usage", {}).get("input_tokens", 0) for d in snapshot["decisions"]) + sum(
            c.get("usage", {}).get("input_tokens", 0) for c in snapshot.get("text_calls", [])
        )
        result.final_url = snapshot["page"].get("url", "")
        if not result.passed and output_dir:
            result.screenshot = self._screenshot(agent, Path(output_dir), result)

    def _screenshot(self, agent, output_dir, result):
        try:
            data = agent.browser.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
        except Exception as exc:
            result.warnings.append(f"screenshot failed: {type(exc).__name__}: {exc}")
            return ""
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{result.name}.jpg"
        path.write_bytes(base64.b64decode(data))
        return str(path)

    @staticmethod
    def _redacted(result, redactor):
        return FlowResult(**redactor.scrub(result.as_dict()))


def _stale_note(agent):
    """Why a budget ran out without progress: decisions dropped because the page changed before acting."""
    stale = (agent.snapshot().get("stale") if agent is not None else None) or []
    if not stale:
        return ""
    reasons = {}
    for item in stale:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    top = ", ".join(f"{reason} ({n}x)" for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:2])
    return f"; {len(stale)} decisions dropped because the page changed first: {top}"
