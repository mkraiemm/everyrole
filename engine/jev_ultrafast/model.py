"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import re
import time
from functools import lru_cache

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)

# Callables str -> str applied to every serialized request body before it leaves the process.
# The embedding runner registers secret redaction here; it is the single outbound choke point.
OUTBOUND_FILTERS = []

# A file the embedding app reads after the process ends: one JSON line per model answer worth knowing (every error,
# and each model's first success in this process), so a model the provider no longer offers can be told to whoever
# runs the app. Unset: nothing is written.
MODEL_LOG_ENV = "JEV_MODEL_LOG"
_ANSWERED = set()  # models already noted as answering in this process


def typesafe_url():
    """Jev endpoint: TypeSafe directly by default, or any compatible gateway via TYPESAFE_URL."""
    return os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")


def typesafe_model():
    return os.environ.get("TYPESAFE_MODEL", "jev-latest")


class ModelRefused(RuntimeError):
    """The provider answered with an error status; nothing was executed."""

    def __init__(self, status):
        super().__init__(f"Model provider returned HTTP {status}; no action executed.")
        self.status = status


def post_json(url, key, body, timeout=None):
    if OUTBOUND_FILTERS:
        payload = json.dumps(body, ensure_ascii=False)
        for outbound_filter in OUTBOUND_FILTERS:
            payload = outbound_filter(payload)
        body = json.loads(payload)
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"},
                                   **({} if timeout is None else {"timeout": timeout}))
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
            if attempt < 2:  # a dropped or reset connection (many calls share one HTTP/2 connection): try again
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError("Model connection failed; no action executed.") from None
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        _note(body, response)
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise ModelRefused(response.status_code)
        return response.json()
    raise RuntimeError("Model unavailable")


def _note(body, response):
    """One line in the model log (MODEL_LOG_ENV): which model, the HTTP status, and the provider's own words when it
    refused. A failure to write never touches the call."""
    path = os.environ.get(MODEL_LOG_ENV)
    model = body.get("model") if isinstance(body, dict) else None
    if not path or not isinstance(model, str) or not model:
        return
    if not response.is_error and model in _ANSWERED:
        return
    line = {"model": model, "status": response.status_code, "at": time.time(),
            "said": response.text[:500] if response.is_error else ""}
    if response.is_error:  # how big the refused request was: a request over the model's window says so (P24)
        line["request_chars"] = len(json.dumps(body, ensure_ascii=False))
    try:
        with open(path, "a", encoding="utf-8") as out:
            out.write(json.dumps(line) + "\n")
    except OSError:
        return
    if not response.is_error:
        _ANSWERED.add(model)


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(INVALID_ANSWER)
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        if action.get("covered_by"):  # a click would hit the overlay: listed in the page note, never a target
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded", "pressed") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def _scroll_note(state):
    """Tell the model what it cannot act on yet: parts of the page off screen, controls under an overlay."""
    note = {}
    ids = {a["id"] for a in state.get("actions", []) if a.get("kind") == "scroll"}
    if ids:
        where = " and ".join(w for w, i in (("below", "scroll_down"), ("above", "scroll_up")) if i in ids)
        note["off_screen"] = f"More of this page is {where}; its elements are not listed until scrolled into view."
    covered = {}
    for a in state.get("actions", []):
        if a.get("covered_by"):
            covered.setdefault(a["covered_by"], []).append(a["label"])
    if covered:
        note["covered"] = [f"{', '.join(dict.fromkeys(labels))[:120]} (hidden under: {overlay})"
                           for overlay, labels in covered.items()]
        note["covered_rule"] = ("Covered controls cannot be clicked until the overlay is closed or dismissed; "
                                "for a cookie or consent banner choose the most privacy-preserving option.")
    return note


INVALID_ANSWER = "Invalid TypeSafe response; no action executed."
CHOOSE_TRIES = 2  # an invalid answer is asked again once (nothing was executed yet)


def choose(state, goal, history):
    """One decision. An answer that fails validation (malformed probabilities) is asked once more before giving up:
    nothing has been executed, and a single bad answer must not end a whole test."""
    for attempt in range(CHOOSE_TRIES):
        try:
            return _choose_once(state, goal, history)
        except ValueError as exc:
            if str(exc) != INVALID_ANSWER or attempt == CHOOSE_TRIES - 1:
                raise


SHRINK_TRIES = 3  # the request as built, then two smaller ones


def _decision_body(state, elements, questions, history, smaller=0):
    """The decision request. A page too big for the model's context window (automationexercise.com's sign-up form:
    HTTP 400) is sent smaller on each try: the page's text halved, then the dropdown options left out of the element
    list (each option is still a choice in select_target), then fewer past actions. What the model may choose never
    shrinks: every target stays."""
    page = {k: state[k] for k in ("url", "title", "text")}
    if smaller:
        page["text"] = page["text"][: len(page["text"]) // (2 ** smaller)]
        elements = [{k: v for k, v in e.items() if k != "options"} for e in elements]
    return {
        "model": typesafe_model(),
        "state": {
            "page": {**page, **_scroll_note(state)},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")}
                for h in history[-(10 // (smaller + 1)):]
            ],
        },
        "questions": questions,
    }


def _choose_once(state, goal, history):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "pressed") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    started = time.perf_counter()
    body, result = None, None
    for smaller in range(SHRINK_TRIES):  # P24: a request over the model's window is refused (400): ask it smaller
        body = _decision_body(state, elements, questions, history, smaller)
        try:
            result = post_json(typesafe_url(), os.environ["TYPESAFE_API_KEY"], body)
            break
        except ModelRefused as exc:
            if exc.status != 400 or smaller == SHRINK_TRIES - 1:
                raise
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value", "secret") if k in action},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


class SecretField(ValueError):
    """A password field: the text model never writes into one (only a named value may be typed there)."""


# the typing job's slots, tried in this order (everyrole.model_check.JOB_SETTINGS["text"]); an empty one is skipped
TEXT_MODEL_SLOTS = (("primary", "TEXT_MODEL"), ("backup", "TEXT_MODEL_BACKUP"), ("backup_2", "TEXT_MODEL_BACKUP_2"))


def text_models(environ):
    """[(slot, model)]: the main typing model, then backup 1 and backup 2; empty slots and repeats left out."""
    out, seen = [], set()
    for slot, name in TEXT_MODEL_SLOTS:
        model = (environ.get(name) or "").strip()
        if model and model not in seen:
            seen.add(model)
            out.append((slot, model))
    return out


def field_text(context):
    if context.get("field", {}).get("secret"):
        raise SecretField("This is a password field: EveryRole types only a named value (a test account's password) "
                          "there, never text from a model. Give this kind of user a value for it.")
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = (os.environ.get("TEXT_MODEL_BASE_URL") or "").rstrip("/")
    models = text_models(os.environ)
    if not base or not models:  # no model or gateway is assumed: the deployment names them
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_BASE_URL and TEXT_MODEL; no model is assumed by the executor.")
    messages = [{"role": "system", "content": TEXT_VALUE}, {"role": "user", "content": json.dumps(context)}]
    failures = []
    for slot, model in models:  # backup 1, then backup 2, takes over when the one before fails: a test never stops
        started = time.perf_counter()
        try:
            result = post_json(base + "/chat/completions", key,
                               {"model": model, "max_tokens": 1024, "messages": messages,
                                **request_options(base, model)})
            value = _text_value(result)
        except (RuntimeError, ValueError) as exc:
            failures.append(f"{model}: {exc}")
            continue
        return value, {
            "model": model,
            "slot": slot,
            "backup": slot != "primary",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "usage": result.get("usage", {}),
        }
    label = str(context.get("field", {}).get("label") or "").strip()
    raise ValueError("Text helper returned no valid field value" + (f" for “{label}”" if label else "")
                     + "; nothing typed. " + " | ".join(failures))


def _text_value(result):
    content = result["choices"][0]["message"]["content"] or ""
    try:
        output = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        match = re.search(r"\{.*\}", content, re.S)  # a model without JSON mode may wrap its answer in prose
        output = json.loads(match.group(0)) if match else None
    value = output.get("text") if isinstance(output, dict) else None
    if output == {"text": None}:  # the answer the prompt asks for when the goal gives no value for this field
        raise ValueError("no value for this field in the test's goal or values")
    if not isinstance(output, dict) or set(output) != {"text"} or not isinstance(value, str) or not value.strip() \
            or len(value) > 2000:
        raise ValueError("no valid field value")
    return value


# ---------------------------------------------------------------------------------------------- model capabilities

@lru_cache(maxsize=4)
def _gateway_models(base):
    """{model id: set of tags} from the gateway's own model list ({} when it can't say)."""
    try:
        response = CLIENT.get(base.rstrip("/") + "/models", timeout=15)
        response.raise_for_status()
        return {m["id"]: set(m.get("tags") or []) for m in response.json().get("data", [])}
    except (httpx.HTTPError, ValueError, KeyError):
        return {}


def request_options(base, model, environ=None):
    """The optional request fields this model accepts, from the gateway's list of its abilities: JSON mode only for
    models that support structured output, the reasoning switch only for reasoning models. A model the list doesn't
    describe gets the request as before."""
    env = os.environ if environ is None else environ
    tags = _gateway_models(base).get(model)
    options = {}
    if tags is None or "structured-output" in tags:
        options["response_format"] = {"type": "json_object"}
    if "api.deepseek.com/" in base:
        options["thinking"] = {"type": "disabled"}
    elif tags is None or "reasoning" in tags:
        options["reasoning"] = ({"enabled": False} if env.get("TEXT_MODEL_REASONING") == "none"
                                else {"effort": "low"})
    return options
