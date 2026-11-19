"""Are the configured AI models still offered? Which setting names which model, a tiny call to each, and how to read
the answers jobs noted (jev_ultrafast.model.MODEL_LOG_ENV).

A model the gateway no longer offers answers HTTP 404 or an error whose code is `model_not_found` (the OpenAI-style
error every compatible gateway returns). That is "missing": a configuration problem only a person can fix by choosing
another model, unlike a busy or failing provider (429, 5xx, a timeout), which passes by itself.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

# Each job (catalog.MODEL_JOBS) has a main model and two backups, tried in this order: when one fails the next takes
# over, an empty slot is skipped. ROLES are the slots' keys in the platform setting "models".
ROLES = ("primary", "backup", "backup_2")
JOB_SETTINGS = {"author": ("EVERYROLE_AUTHOR_MODEL", "EVERYROLE_AUTHOR_BACKUP", "EVERYROLE_AUTHOR_BACKUP_2"),
                "text": ("TEXT_MODEL", "TEXT_MODEL_BACKUP", "TEXT_MODEL_BACKUP_2")}
JOB_LABELS = {"author": "Writing tests", "text": "Typing values"}
ROLE_LABELS = {"primary": "main model", "backup": "backup 1", "backup_2": "backup 2"}
# The settings that name a model on the AI gateway, with what each does (in the admin's words).
SETTINGS = tuple((name, f"{JOB_LABELS[job]}, {ROLE_LABELS[role]}")
                 for job in ("author", "text") for role, name in zip(ROLES, JOB_SETTINGS[job]))
# writing tests uses the typing model of the same slot when it has none of its own (propose.author_models)
AUTHOR_FALLBACK = dict(zip(JOB_SETTINGS["author"], JOB_SETTINGS["text"]))
MISSING_CODE = "model_not_found"
TRANSIENT = {408, 425, 429}  # plus every 5xx: the provider is busy or failing, the model may well be there
PROBE_TIMEOUT_S = 30


def slots(environ):
    """{model id: [labels of the settings that use it]}, as a job with this environment uses them: writing tests
    falls back to the text model when it has none of its own (propose.author_models)."""
    used = {}
    for name, label in SETTINGS:
        model = environ.get(name) or environ.get(AUTHOR_FALLBACK.get(name, ""), "")
        if model:
            used.setdefault(model, []).append(f"{label} ({name})")
    return used


def chain(environ, job):
    """[(role, model)]: the models a job tries, in order (main, backup 1, backup 2). An empty slot is skipped, and so
    is a model already earlier in the chain; writing tests takes the typing model of a slot it leaves empty."""
    out, seen = [], set()
    for role, name in zip(ROLES, JOB_SETTINGS[job]):
        model = environ.get(name) or environ.get(AUTHOR_FALLBACK.get(name, ""), "")
        if model and model not in seen:
            seen.add(model)
            out.append((role, model))
    return out


def usage_group(job, role):
    """The usage record's group for calls a slot answered: "text", "text_backup", "text_backup_2", "author", ..."""
    return job if role == "primary" else f"{job}_{role}"


def answered_slot(call):
    """Which slot answered a model call (its usage says "slot"; one noted before backup 2 only says "backup")."""
    return call.get("slot") or ("backup" if call.get("backup") else "primary")


def error_code(said):
    """The `code` or `type` of an OpenAI-style error body ({"error": {"code": ..., "type": ...}}), else ""."""
    try:
        error = json.loads(said or "").get("error")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or error.get("type") or "")


def error_message(said):
    try:
        error = json.loads(said or "").get("error")
    except (ValueError, AttributeError):
        return (said or "").strip()[:300]
    message = error.get("message") if isinstance(error, dict) else error
    return str(message or said or "").strip()[:300]


def is_missing(status, said=""):
    return status == 404 or error_code(said) == MISSING_CODE


def is_transient(status):
    return status is None or status in TRANSIENT or status >= 500


@dataclass
class Answer:
    model: str
    status: int | None  # None: no HTTP answer (unreachable, timed out)
    said: str = ""  # the gateway's words when it refused

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 300

    @property
    def missing(self):
        return self.status is not None and is_missing(self.status, self.said)

    @property
    def detail(self):
        if self.ok:
            return "answers"
        if self.status is None:
            return f"no answer: {self.said}"
        message = error_message(self.said)
        return f"HTTP {self.status}" + (f": {message}" if message else "")


def probe(base, key, model, post=None):
    """A tiny call (a few tokens) to one model. -> Answer."""
    post = post or (lambda url, **kw: httpx.post(url, timeout=PROBE_TIMEOUT_S, **kw))
    body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with: ok"}]}
    try:
        response = post(base.rstrip("/") + "/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return Answer(model, None, f"{type(exc).__name__}: {exc}"[:300])
    return Answer(model, response.status_code, "" if response.is_success else response.text[:500])


def read_log(path):
    """The answers a job noted (one JSON line each), the last one per model. -> {model: Answer}."""
    answers = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return answers
    for line in lines:
        try:
            row = json.loads(line)
            answers[str(row["model"])] = Answer(str(row["model"]), int(row["status"]), str(row.get("said") or ""))
        except (ValueError, KeyError, TypeError):
            continue  # a line cut short when the process was stopped
    return answers
