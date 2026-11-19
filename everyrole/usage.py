"""What a job used of the AI models, exactly as the gateway reported it.

Every call's `usage` object is summed field by field, whatever fields the provider returns (tokens in and out,
cached tokens, and the gateway's own dollar `cost` when it gives one), grouped by what made the call:

  engine   the Jev engine choosing each step of a test or a sign-in
  text     the text helper choosing values to type
  author   the model drafting tests while exploring

A job writes its total to `usage.json` next to its results, so what people see is always what was really used.
"""

import json
import os
import time
from functools import lru_cache
from pathlib import Path

USAGE_FILE = "usage.json"
COST_FIELDS = ("cost", "market_cost", "gateway_cost")  # the gateway's own dollar figures, most specific first


def add(total, usage):
    """Sums the number fields of `usage` into `total` (nested objects too). Returns `total`."""
    for key, value in (usage or {}).items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
        elif isinstance(value, dict):
            add(total.setdefault(key, {}), value)
    return total


def of_calls(calls):
    """Summed usage of a list of engine/model call records ({"usage": {...}}), with the number of calls."""
    total = {"calls": 0}
    for call in calls or []:
        total["calls"] += 1
        add(total, call.get("usage") or {})
    return total


def merge(*groups):
    """{"engine": {...}, "text": {...}} objects summed per group."""
    out = {}
    for group in groups:
        for name, usage in (group or {}).items():
            add(out.setdefault(name, {}), usage)
    return out


def cost(usage):
    """Dollars the gateway reported for this usage, or None when it reported no cost."""
    for field in COST_FIELDS:
        if isinstance(usage.get(field), (int, float)):
            return usage[field]
    return None


def summary(groups):
    """-> {"groups": {...}, "cost_usd": total of the reported costs, "cost_known": every group reported one}.
    Groups without model calls (machine time) are not priced here."""
    costs = {name: cost(usage) for name, usage in groups.items() if usage.get("calls")}
    return {"groups": groups, "cost_usd": round(sum(c for c in costs.values() if c is not None), 6),
            "cost_known": all(c is not None for c in costs.values()),
            "unpriced": sorted(name for name, c in costs.items() if c is None)}


def models(environ=None):
    """The model behind each kind of call (usage group), from the environment the job ran with: the engine's, then
    each job's main model and backups (model_check.usage_group: text, text_backup, text_backup_2, author, ...)."""
    from .model_check import JOB_SETTINGS, ROLES, chain, usage_group

    env = os.environ if environ is None else environ
    out = {"engine": env.get("TYPESAFE_MODEL", "")}
    for job in JOB_SETTINGS:
        out.update({usage_group(job, role): "" for role in ROLES})
        out.update({usage_group(job, role): model for role, model in chain(env, job)})
    return out


def describe_models(environ=None):
    """The models a job works with, in one line for its log."""
    from .model_check import ROLE_LABELS, ROLES, usage_group

    m = models(environ)

    def job(name, key):
        backups = [f"{ROLE_LABELS[r]} {m[usage_group(key, r)]}" for r in ROLES[1:] if m[usage_group(key, r)]]
        return f"{name} {m[key] or 'none'}" + (f" ({', '.join(backups)})" if backups else "")

    return (f"models: steps {m['engine'] or 'default'} · {job('typing values', 'text')}"
            f" · {job('writing tests', 'author')}")


@lru_cache(maxsize=1)
def gateway_prices(base_url):
    """{model: {"input": $/token, "output": $/token}} as the AI gateway publishes them ({} when it can't say)."""
    import httpx

    try:
        response = httpx.get(base_url.rstrip("/") + "/models", timeout=15)
        response.raise_for_status()
        return {m["id"]: {k: float(v) for k, v in (m.get("pricing") or {}).items() if k in ("input", "output")}
                for m in response.json().get("data", []) if m.get("pricing")}
    except (httpx.HTTPError, ValueError, KeyError):
        return {}


def write(directory, *, kind, groups, started, detail="", name=USAGE_FILE, counts=None):
    """Writes the job's usage record (usage.json, or `name` where several jobs share a folder)."""
    record = {"kind": kind, "detail": detail, "counts": counts or {}, "started": started, "ended": time.time(),
              "models": models(),
              **summary(groups)}
    Path(directory).mkdir(parents=True, exist_ok=True)
    (Path(directory) / name).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
