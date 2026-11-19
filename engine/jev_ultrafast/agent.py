"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text
from .questions import MAX_STEPS


class BudgetExceeded(ValueError):
    """The run used up its steps or model calls before the goal was reached (a result, not a crash)."""


class Agent:
    def __init__(
        self, url, goals, *, record_dir=None, screenshots=False,
        max_steps=MAX_STEPS, text_provider=None, before_execute=None, browser_options=None, after_action=None,
        stop_needs_fresh_page=True, fill_before_submit=False,
    ):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.max_steps = max_steps
        # text_provider(context, action, page) -> (text, helper). Default: the text LLM.
        self.text_provider = text_provider or (lambda context, _action, _page: field_text(context))
        # before_execute(action, page, text) raises to veto an action before any browser input.
        self.before_execute = before_execute
        # after_action(browser) runs after an action and before the next observation (e.g. wait for loading).
        self.after_action = after_action
        # DONE/BLOCKED make no browser input. Callers that judge the outcome themselves can accept them even
        # when live content (tickers) changed the page since the decision.
        self.stop_needs_fresh_page = stop_needs_fresh_page
        # P27: a submit while its form still has an empty field the form needs fills that field first (a sign-in:
        # the model pressed "Log in" with the email and password fields empty). Off by default: a test may mean to
        # send a form empty, to see the app's own error.
        self.fill_before_submit = fill_before_submit
        self.browser = Browser(url, **(browser_options or {}))
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            stale=[],  # decisions dropped because the page changed first: (reason, the chosen action)
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage as exc:
                chosen = (state.get("decision") or {}).get("choice")
                state["stale"].append({"reason": str(exc), "choice": chosen})
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= self.max_steps * 2:
                raise BudgetExceeded("Reached the model-call budget before the goal")
            state["decision"] = choose(state["page"], state["goal"], state["history"])
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if self.stop_needs_fresh_page and not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            chosen = action
            if self.fill_before_submit:
                action = self._missing_fill(action, page)
            if len(state["history"]) >= self.max_steps:
                state["status"] = "blocked"
                raise BudgetExceeded(f"Stopped at the {self.max_steps}-action budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page, action):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = self.text_provider(context, action, page)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            if self.before_execute:
                self.before_execute(action, page, text)
            # Browser.act checks freshness immediately before input, including after text generation.
            done = state["browser"].act(action, page, text=text)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                    # P26: where the input went on the screen (the replay draws the cursor and focus ring there)
                    "point": done.get("point") if isinstance(done, dict) else None,
                    # P27: the model chose to send the form; this field was empty, so it was filled first
                    **({"instead_of": chosen["label"]} if action is not chosen else {}),
                }
            )
            if self.after_action:
                try:
                    self.after_action(state["browser"])
                except Exception:
                    pass  # waiting is best effort; the observation below is what counts
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if (len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated))
                or self._in_circles(action)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def _missing_fill(self, action, page):
        """P27: the fill to do before `action` sends its form: its first empty field the form needs, when that field
        is offered on the page; else `action` itself."""
        for node in self.state["browser"].empty_before_submit(action):
            fill = next((a for a in page["actions"] if a["kind"] == "fill" and a.get("node") == node), None)
            if fill is not None:
                return fill
        return action

    CIRCLES = 2  # scrolls back to a spot already reached twice before it counts as going in circles

    def _in_circles(self, action):
        """P20: True once scrolling keeps returning to the same spots of the same page (down, up, down, up...): every
        part of it was already seen, so more scrolling finds nothing new. Failing tests did this until their 40 or
        80 step budget ran out, one model call per step (automationexercise.com, 2026-11-17)."""
        page = self.state["page"]
        # a spot is a band of half a screen: scrolling back lands a few pixels off where it was (a carousel, lazy
        # images), and that is still the same part of the page
        band = max(1, (page.get("h") or 0) // 2)
        spot = (page.get("url"), int((page.get("scroll") or {}).get("y") or 0) // band)
        seen = self.state.setdefault("spots", {})
        seen[spot] = seen.get(spot, 0) + 1
        if action["kind"] != "scroll" or seen[spot] <= self.CIRCLES:
            return False
        self.state["circles"] = self.state.get("circles", 0) + 1
        return self.state["circles"] >= self.CIRCLES

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
