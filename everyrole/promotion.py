"""Tests found on staging, tried on production: promoted when the feature is live there, quiet until then.

Exploring another environment (staging) keeps what it finds for that environment only (`found_on`, and `run_on`
set to it: init.found_on). Every production run then tries those tests once more, apart from the others: one that
works is promoted (its `run_on` is removed: from now on it runs everywhere, like any test); one that doesn't is
"not live on production yet" and says nothing (no failure, no alert, no failed build), until a later deploy brings
the feature. A test that changes data keeps its own `run_on` (e.g. preview only) and is never tried on production.
"""

import json
from pathlib import Path

PRODUCTION = "production"


def on_trial(flow, environment):
    """A test found on another environment, restricted to exactly that one, while this run is on production."""
    return (environment == PRODUCTION and bool(flow.found_on) and flow.found_on != PRODUCTION
            and tuple(flow.run_on) == (flow.found_on,))


def settle(trial_flows, results):
    """Promote the tried tests that passed (their files lose `run_on`). -> {"promoted": [...], "not_live": [...]}"""
    by_name = {r.get("name"): r for r in results}
    promoted, not_live = [], []
    for flow in trial_flows:
        result = by_name.get(flow.name) or {}
        if result.get("passed") and _unrestrict(flow):
            promoted.append(flow.name)
        else:
            not_live.append(flow.name)
    return {"promoted": promoted, "not_live": not_live}


def _unrestrict(flow):
    """Remove `run_on` from the flow's own file (one flow per file, or a list of flows). -> changed?"""
    path = Path(flow.source) if flow.source else None
    if path is None or not path.is_file():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict) and item.get("name") == flow.name and "run_on" in item:
            item.pop("run_on")
            item["promoted_from"] = item.get("found_on", "")
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed
