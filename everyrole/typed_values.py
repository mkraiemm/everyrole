"""Typing values, measured: each value the TEXT model typed into a form field during a test, and whether the page
accepted it. The "text" models' quality is measured from these records.

Only what the page itself says counts, never a guess:

- a value is the text model's when the engine's text helper wrote it (no named value of the test was chosen:
  values.ValueChooser answered WRITE, or the test has no named values); a named value is the test's, not the model's.
- it was REJECTED when its field said it was wrong: the browser's own check of the field (its type, pattern, length,
  range: the field's `validity`) failed, or the app marked the field invalid (`aria-invalid`), right after it was
  typed or when the test ended (a field that is still on the page then, and wasn't typed into again since).
- every other typed value was ACCEPTED.

Each test's result keeps its records (results.json "typed_values": [{field, model, accepted, cause, message}]);
`count` sums them per model.
"""

import json
from collections import Counter, defaultdict

from .values import WRITE

# the browser's own checks (ValidityState), in plain words; the first one that failed names the cause
BROWSER_CHECKS = (("typeMismatch", "not the kind of value the field takes"),
                  ("patternMismatch", "not in the form the field asks for"),
                  ("tooLong", "too long for the field"), ("tooShort", "too short for the field"),
                  ("rangeOverflow", "above the field's highest value"),
                  ("rangeUnderflow", "below the field's lowest value"),
                  ("stepMismatch", "not one of the field's allowed steps"),
                  ("badInput", "the browser couldn't read it as the field's kind of value"),
                  ("valueMissing", "the field was still empty after typing"),
                  ("customError", "the app's own check of the field refused it"))

# the field's state, by the engine's own id for it (the element it typed into, never a selector)
FIELD_STATE = """(node => {
  const CHECKS = (CHECK_KEYS);
  const e = window.__jevFast?.nodes.get(node);
  if (!e || !e.isConnected) return {connected: false};
  const failed = e.validity && e.willValidate && !e.validity.valid
    ? CHECKS.filter(k => e.validity[k]) : [];
  const aria = (e.getAttribute('aria-invalid') || '').trim().toLowerCase();
  const marked = aria !== '' && aria !== 'false';
  const words = ids => (ids || '').split(/\\s+/).filter(Boolean)
    .map(id => (e.ownerDocument.getElementById(id)?.innerText || '').trim()).filter(Boolean).join(' ');
  const message = failed.length ? e.validationMessage
    : marked ? words(e.getAttribute('aria-errormessage')) || words(e.getAttribute('aria-describedby')) : '';
  return {connected: true, failed, marked, message: (message || '').slice(0, 200)};
})""".replace("(CHECK_KEYS)", json.dumps([key for key, _ in BROWSER_CHECKS]))

APP_MARKED = "the app marked the field invalid"


def written_by_model(helper):
    """The text model wrote this value (not a named value of the test). -> its model id, or "" when it didn't."""
    if not isinstance(helper, dict) or not helper.get("model"):
        return ""
    return str(helper["model"]) if helper.get("value_choice") in (None, WRITE) else ""


def cause_of(state):
    """Why the field said the value is wrong, in plain words; "" when it didn't (or isn't on the page)."""
    if not isinstance(state, dict) or not state.get("connected"):
        return ""
    failed = set(state.get("failed") or ())
    for key, words in BROWSER_CHECKS:
        if key in failed:
            return words
    return APP_MARKED if state.get("marked") else ""


class TypedValues:
    """One test's typed values. The runner calls: answered (the text provider's answer), about_to_type (the engine
    is about to type it into a field), typed (after the action, the page settled), settle (the test ended)."""

    def __init__(self):
        self.entries = []  # [{"node", "field", "model", "cause", "message"}] in the order typed
        self._answer = None  # (text, model) of the text provider's last answer
        self._pending = None  # the entry being typed now

    def answered(self, text, helper):
        self._answer = (text, written_by_model(helper))

    def about_to_type(self, action, text):
        """A fill is about to run. Only a value the text model wrote is tracked (the provider's answer is reused when
        the engine types it again after a stale page)."""
        self._pending = None
        if not isinstance(action, dict) or action.get("kind") != "fill" or self._answer is None:
            return
        answer_text, model = self._answer
        if model and text == answer_text and type(action.get("node")) is int:
            self._pending = {"node": action["node"], "field": str(action.get("label") or "")[:120], "model": model,
                             "cause": "", "message": ""}

    def typed(self, browser):
        """After the fill ran: the value is counted, and its field read once now."""
        entry, self._pending = self._pending, None
        if entry is None:
            return
        self.entries.append(entry)
        self._judge(browser, entry)

    def settle(self, browser):
        """The test ended: the last value typed into each field still on the page is read again (an app that checks
        on submit marks the field then)."""
        last = {}
        for entry in self.entries:
            last[entry["node"]] = entry
        for entry in last.values():
            if not entry["cause"]:
                self._judge(browser, entry)

    def records(self):
        return [{"field": e["field"], "model": e["model"], "accepted": not e["cause"], "cause": e["cause"],
                 "message": e["message"]} for e in self.entries]

    @staticmethod
    def _judge(browser, entry):
        try:
            state = browser.evaluate(f"{FIELD_STATE}({entry['node']})")
        except Exception:
            return  # the page couldn't be read (navigating): nothing said the value was wrong
        cause = cause_of(state)
        if cause:
            entry["cause"], entry["message"] = cause, str(state.get("message") or "")[:200]


def count(results, model=""):
    """A run's results.json -> {model: {"typed", "accepted", "rejected", "causes": {cause: n}}} for every model that
    typed values (only `model` when given). None when the run didn't record typed values (it ran before they were)."""
    tests = (results or {}).get("results") or []
    if not any("typed_values" in test for test in tests):
        return None
    out = defaultdict(lambda: {"typed": 0, "accepted": 0, "rejected": 0, "causes": Counter()})
    for test in tests:
        for value in test.get("typed_values") or ():
            if model and value.get("model") != model:
                continue
            row = out[value.get("model") or ""]
            row["typed"] += 1
            if value.get("accepted"):
                row["accepted"] += 1
            else:
                row["rejected"] += 1
                row["causes"][value.get("cause") or "no reason given"] += 1
    return {name: {**row, "causes": dict(row["causes"].most_common())} for name, row in out.items()}
