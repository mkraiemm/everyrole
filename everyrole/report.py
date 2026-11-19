"""Suite outputs: results.json, summary.md (PR comment), junit.xml. Inputs are already redacted.

Only app failures are shown as failures. Machine problems are shown as "not checked" and results that
depend on a failed sign-in as "not run" (see verdict.py). Flaky tests (flaky.py) are set aside: counted apart,
never as failures, so nothing that reads the summary alarms on them.
"""

import json
from pathlib import Path
from xml.etree import ElementTree

from . import importance, verdict

COMMENT_MARKER = "<!-- everyrole-report -->"
STATUS_LABELS = {
    "passed": "✅ passed",
    "failed_expect": "❌ failed",
    "forbidden_action": "⛔ forbidden action",
    "left_allowed_hosts": "⛔ left allowed hosts",
    "budget_exceeded": "⏱️ over budget",
    "blocked": "❌ agent blocked",
    "error": "⚠️ error",
}


def summarize(suite, *, base_url, environment, parts=None):
    results = suite["results"]
    access = suite.get("access", [])
    tally = verdict.counts([*results, *access])
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.get("passed")),
        "failed": tally[verdict.FAILED],  # app failures only
        "not_checked": tally[verdict.NOT_CHECKED],  # machine problems: say nothing about the app
        "not_run": tally[verdict.NOT_RUN],
        "changed": tally[verdict.CHANGED],  # the app works but its wording changed: the test needs an update
        "flaky": tally[verdict.FLAKY],  # set aside: they keep failing and passing on the same app (not failures)
        "flaky_tests": [r["name"] for r in results if verdict.of(r) == verdict.FLAKY],
        # per level (critical / important / minor), so alerts can be about what matters (importance.py)
        "by_importance": importance.summary(results),
        "access_total": len(access),
        "access_passed": sum(1 for a in access if a.get("passed")),
        "duration_ms": suite["duration_ms"],
        "base_url": base_url,
        "parts": dict(parts or {}),  # every address of the app (pages on other parts are written part:/path)
        "environment": environment,
        "input_tokens": sum(r.get("input_tokens", 0) for r in results),
    }


def _seconds(ms):
    return f"{(ms or 0) / 1000:.1f}s"


def flaky_text(result):
    """How a flaky test did lately, in words: "failed 2 of its last 6 runs; trusted again after 3 passes in a row"."""
    info = result.get("flaky") or {}
    left = info.get("passes_left", 0)
    return (f"failed {info.get('failed', 0)} of its last {plural(info.get('runs', 0), 'run')}; set aside, not counted "
            f"as broken, until it passes {plural(left, 'more time')} in a row")


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _detail(result):
    if verdict.of(result) == verdict.FLAKY:
        now = "passed this time" if result.get("passed") else f"failed this time ({_why(result)})"
        return f"flaky: {now}; {flaky_text(result)}"
    if verdict.of(result) == verdict.NOT_CHECKED:
        return f"machine problem, not your app: {result.get('error') or result.get('status', '')}"
    if verdict.of(result) == verdict.CHANGED:
        return "the wording changed: " + "; ".join(f"“{old}” is now “{new}”"
                                                   for old, new in verdict.reworded(result.get("expectations")))
    return _why(result)


def _why(result):
    if result.get("error"):
        return result["error"]
    failed = [e for e in result.get("expectations", []) if not e.get("passed")]
    if failed:
        return "; ".join(f"{e['kind']} “{e['expected']}”: {e['detail']}" for e in failed)
    return result.get("status", "") if not result.get("passed") else ""


def _cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")[:300]


ACCESS_LABELS = {
    ("allow", "allowed"): "✅ allowed",
    ("deny", "denied"): "✅ denied",
    ("deny", "allowed"): "❌ allowed (should deny)",
    ("allow", "denied"): "❌ denied (should allow)",
}


def _status(result):
    outcome = verdict.of(result)
    if outcome == verdict.NOT_CHECKED:
        return "⚠️ not checked"
    if outcome == verdict.NOT_RUN:
        return "⏭️ not run"
    if outcome == verdict.CHANGED:
        return "📝 wording changed"
    if outcome == verdict.FLAKY:
        return "🟡 flaky (set aside)"
    if outcome == verdict.PASSED and result.get("attempts", 1) > 1:
        return "✅ passed on 2nd try"
    if result.get("passed") and result.get("expect_denied"):
        return "✅ denied as expected"
    return STATUS_LABELS.get(result.get("status"), result.get("status", ""))


def _access_cell(row):
    outcome = verdict.of(row)
    if outcome == verdict.NOT_CHECKED:
        return "⚠️ not checked"
    if outcome == verdict.NOT_RUN:
        return "⏭️ not run"
    return ACCESS_LABELS.get((row["expected"], row["observed"]), row["observed"])


def headline(summary):
    checks = summary["total"] + summary.get("access_total", 0)
    unchecked = summary.get("not_checked", 0)
    changed = summary.get("changed", 0)
    also = f" · 📝 {changed} with new wording to review" if changed else ""
    if summary.get("flaky"):
        also += f" · 🟡 {summary['flaky']} flaky, set aside"
    if summary["failed"]:
        return f"❌ {summary['failed']} of {checks} checks failed{also}"
    if changed or summary.get("flaky"):
        return f"✅ Nothing broken found{also}"
    if unchecked:
        return (f"✅ Nothing broken found · ⚠️ {unchecked} of {checks} could not be checked "
                f"(machine problem, not your app)")
    if summary.get("not_run"):
        return f"✅ Nothing broken found · {summary['not_run']} not run"
    return "✅ Everything passed"


def _where(summary):
    """Page -> what people read: the real address when the app has several (never "app:/x")."""
    parts = summary.get("parts") or {}
    if len(parts) < 2:
        return lambda page: page
    from .site import Site

    return Site.of(parts).label


def _access_table(access, where=lambda page: page):
    personas = list(dict.fromkeys(a["persona"] for a in access))
    paths = list(dict.fromkeys(a["path"] for a in access))
    cell = {(a["path"], a["persona"]): a for a in access}
    lines = ["", "#### Access matrix (no AI)", "", "| Page | " + " | ".join(personas) + " |",
             "|---|" + "---|" * len(personas)]
    for path in paths:
        row = []
        for persona in personas:
            a = cell.get((path, persona))
            row.append("—" if a is None else _access_cell(a))
        lines.append(f"| `{_cell(where(path))}` | " + " | ".join(row) + " |")
    for a in (a for a in access if verdict.of(a) == verdict.FAILED):
        landed = a.get("url") or "—"
        lines.append(f"\n> `{_cell(where(a['path']))}` as **{a['persona']}**: {_cell(a.get('detail'))} "
                     f"(landed on {landed})")
    unchecked = [a for a in access if verdict.of(a) == verdict.NOT_CHECKED]
    if unchecked:
        pages = ", ".join(f"`{_cell(where(a['path']))}` as {a['persona']}" for a in unchecked)
        lines.append(f"\n> ⚠️ Not checked (machine problem, not your app): {pages}")
    return lines


def markdown(summary, results, access=()):
    head = headline(summary)
    with_persona = any(r.get("persona") for r in results)
    access_part = (f" · access {summary['access_passed']}/{summary['access_total']}"
                   if summary.get("access_total") else "")
    lines = [
        COMMENT_MARKER,
        f"### Does my app still work? {head}",
        "",
        f"`{summary['base_url']}` · env `{summary['environment']}` · flows {summary['passed']}/{summary['total']}"
        f"{access_part} · {_seconds(summary['duration_ms'])} total",
    ]
    if results:  # an access-only run has no flow table
        lines += ["", "| Flow | Level | " + ("As | " if with_persona else "") + "Result | Time | Steps | Detail |",
                  "|---|---|" + ("---|" if with_persona else "") + "---|---|---|---|"]
    for r in sorted(results, key=lambda r: importance.rank(r.get("importance"))):  # critical first
        persona = f"{r.get('persona') or 'guest'} | " if with_persona else ""
        lines.append(
            f"| `{r['name']}` | {importance.LABELS[importance.level(r.get('importance'))]} | {persona}{_status(r)} "
            f"| {_seconds(r.get('duration_ms'))} "
            f"| {len(r.get('steps', []))} | {_cell(_detail(r))} |"
        )
    failing = [r for r in results if verdict.of(r) == verdict.FAILED and r.get("steps")]
    for r in failing:
        lines += ["", f"<details><summary>Steps for <code>{r['name']}</code></summary>", ""]
        lines += [f"{s['step']}. {s['kind']} “{_cell(s['action'])}”" for s in r["steps"]]
        lines += ["", "</details>"]
    for r in results:
        for warning in r.get("warnings", []):
            lines.append(f"\n> ⚠️ `{r['name']}`: {_cell(warning)}")
    if access:
        lines += _access_table(list(access), _where(summary))
    return "\n".join(lines) + "\n"


def junit(summary, results, access=()):
    suite = ElementTree.Element(
        "testsuite",
        name="everyrole",
        tests=str(summary["total"] + summary.get("access_total", 0)),
        failures=str(summary["failed"]),
        skipped=str(summary.get("not_checked", 0) + summary.get("not_run", 0) + summary.get("flaky", 0)),
        time=f"{summary['duration_ms'] / 1000:.3f}",
    )
    for r in results:
        case = ElementTree.SubElement(
            suite, "testcase", classname="everyrole", name=r["name"], time=f"{(r.get('duration_ms') or 0) / 1000:.3f}"
        )
        outcome = verdict.of(r)
        if outcome in (verdict.NOT_CHECKED, verdict.NOT_RUN, verdict.CHANGED, verdict.FLAKY):  # never fails CI
            ElementTree.SubElement(case, "skipped", message=_detail(r))
        elif outcome == verdict.FAILED:
            failure = ElementTree.SubElement(case, "failure", message=r.get("status", "failed"))
            failure.text = _detail(r)
    for a in access:
        case = ElementTree.SubElement(suite, "testcase", classname="everyrole.access",
                                      name=f"{a['path']} as {a['persona']}", time="0")
        outcome = verdict.of(a)
        if outcome in (verdict.NOT_CHECKED, verdict.NOT_RUN):
            ElementTree.SubElement(case, "skipped", message=a.get("detail", ""))
        elif outcome == verdict.FAILED:
            failure = ElementTree.SubElement(case, "failure", message=f"expected {a['expected']}, got {a['observed']}")
            failure.text = a.get("detail", "")
    return ElementTree.tostring(suite, encoding="unicode", xml_declaration=True)


def write(output_dir, summary, results, access=()):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summary, "results": results, "access": list(access)}
    (output_dir / "results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "summary.md").write_text(markdown(summary, results, access), encoding="utf-8")
    (output_dir / "junit.xml").write_text(junit(summary, results, access), encoding="utf-8")
    return output_dir
