"""docs/flow-spec.md must document every field of both schemas, so the reference cannot drift."""

import re
from pathlib import Path

import pytest

from everyrole.spec import schema

DOC = (Path(__file__).resolve().parents[1] / "docs" / "flow-spec.md").read_text(encoding="utf-8")
PLACEHOLDERS = {"as": "<persona>", "personas": "<persona>", "access": "<path>"}


def field_paths(node, prefix="", parent=""):
    """Dotted paths of every documented field: `login.max_steps`, `personas.<persona>.values`..."""
    for name, child in node.get("properties", {}).items():
        yield prefix + name
        yield from field_paths(child, f"{prefix}{name}.", name)
    extra = node.get("additionalProperties")
    if isinstance(extra, dict) and parent in PLACEHOLDERS:
        yield from field_paths(extra, f"{prefix}{PLACEHOLDERS[parent]}.", "")
    for option in node.get("oneOf", []):
        yield from field_paths(option, prefix, parent)


def documented(path):
    return re.search(rf"^\| `{re.escape(path)}` \|", DOC, re.MULTILINE) is not None


@pytest.mark.parametrize("name", ["flow.schema.json", "config.schema.json"])
def test_every_schema_field_is_documented(name):
    paths = list(dict.fromkeys(field_paths(schema(name))))
    assert len(paths) > 10
    missing = [p for p in paths if not documented(p)]
    assert not missing, f"document these {name} fields in docs/flow-spec.md: {missing}"


def test_every_check_kind_is_documented():
    kinds = schema("flow.schema.json")["$defs"]["check"]["properties"]
    missing = [k for k in kinds if not documented(k)]
    assert not missing, f"document these checks in docs/flow-spec.md: {missing}"


def test_documented_defaults_match_the_schema():
    for name in ("flow.schema.json", "config.schema.json"):
        stack = [("", schema(name), "")]
        while stack:
            prefix, node, parent = stack.pop()
            for key, child in node.get("properties", {}).items():
                path = prefix + key
                if isinstance(child.get("default"), (int, bool)) and not isinstance(child.get("default"), dict):
                    value = str(child["default"]).lower()
                    row = re.search(rf"^\| `{re.escape(path)}` \|[^\n]*", DOC, re.MULTILINE)
                    assert row and f"`{value}`" in row.group(0), f"{path}: default {value} not in its row"
                stack.append((f"{path}.", child, key))
            extra = node.get("additionalProperties")
            if isinstance(extra, dict) and parent in PLACEHOLDERS:
                stack.append((f"{prefix}{PLACEHOLDERS[parent]}.", extra, ""))
