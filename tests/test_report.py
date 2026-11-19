from xml.etree import ElementTree

from everyrole import report

RESULTS = [
    {"name": "login", "status": "passed", "passed": True, "duration_ms": 8200, "steps": [{}] * 3,
     "expectations": [], "warnings": [], "input_tokens": 1000},
    {"name": "pricing", "status": "failed_expect", "passed": False, "duration_ms": 5100, "input_tokens": 500,
     "steps": [{"step": 1, "kind": "click", "action": "Pricing | Plans"}],
     "expectations": [{"kind": "text_visible", "expected": "Pro", "passed": False, "detail": "not found in page text"}],
     "warnings": ["screenshot failed: timeout"]},
]


def summary():
    return report.summarize({"results": RESULTS, "duration_ms": 9000}, base_url="https://x.io", environment="prod")


def test_summary_counts():
    s = summary()
    assert (s["total"], s["passed"], s["failed"], s["input_tokens"]) == (2, 1, 1, 1500)


def test_markdown_has_marker_table_and_failure_detail():
    md = report.markdown(summary(), RESULTS)
    assert md.startswith(report.COMMENT_MARKER)
    assert "1 of 2 checks failed" in md
    assert "| `pricing` | Important | ❌ failed | 5.1s | 1 | text_visible “Pro”: not found in page text |" in md
    assert "Pricing \\| Plans" in md  # table-breaking pipes escaped
    assert "screenshot failed" in md


def test_junit_is_valid_xml(tmp_path):
    xml = report.junit(summary(), RESULTS)
    root = ElementTree.fromstring(xml.split("?>", 1)[1])
    assert root.get("tests") == "2" and root.get("failures") == "1"
    assert root.findall("testcase")[1].find("failure").get("message") == "failed_expect"
    out = report.write(tmp_path / "r", summary(), RESULTS)
    assert {p.name for p in out.iterdir()} == {"results.json", "summary.md", "junit.xml"}


def test_an_access_only_run_has_no_empty_flow_table():
    access = [{"path": "/admin", "persona": "guest", "expected": "deny", "observed": "deny", "passed": True,
               "status": "passed"}]
    s = report.summarize({"results": [], "access": access, "duration_ms": 900}, base_url="https://x.io",
                         environment="prod")
    md = report.markdown(s, [], access)
    assert "| Flow |" not in md and "/admin" in md
    assert "| Flow |" in report.markdown(summary(), RESULTS)
