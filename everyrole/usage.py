"""What a job used of the AI models, exactly as the gateway reported it.

Every call's `usage` object is summed field by field, whatever fields the provider returns (tokens in and out,
cached tokens, and the gateway's own dollar `cost` when it gives one), grouped by what made the call:

  engine   the Jev engine choosing each step of a test or a sign-in
  text     the text helper choosing values to type

A job writes its total to `usage.json` next to its results, so what people see is always what was really used.
"""

import json
import os
import time
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
    """The model behind each kind of call, from the environment the job ran with."""
    env = os.environ if environ is None else environ
    return {"engine": env.get("TYPESAFE_MODEL", ""), "text": env.get("TEXT_MODEL", "")}


def write(directory, *, kind, groups, started, detail="", name=USAGE_FILE, counts=None):
    """Writes the job's usage record (usage.json, or `name` where several jobs share a folder)."""
    record = {"kind": kind, "detail": detail, "counts": counts or {}, "started": started, "ended": time.time(),
              "models": models(),
              **summary(groups)}
    Path(directory).mkdir(parents=True, exist_ok=True)
    (Path(directory) / name).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
