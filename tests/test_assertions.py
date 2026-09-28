from everyrole import assertions
from everyrole.assertions import PageFacts

FACTS = PageFacts(
    url="https://app.example.com/dashboard?tab=projects",
    text="Welcome back\n  Your   Projects\nCreate project",
    labels=("Create project", "Account menu"),
)


def run(expect):
    return assertions.evaluate(assertions.build(expect), FACTS)


def test_each_check_type_passes_and_fails():
    cases = [
        ({"url_contains": "/dashboard"}, {"url_contains": "/login"}),
        ({"url_matches": r"/dashboard\?tab=\w+$"}, {"url_matches": r"^http://"}),
        ({"text_visible": "your projects"}, {"text_visible": "Billing"}),
        ({"text_absent": "Something went wrong"}, {"text_absent": "Welcome back"}),
        ({"element_visible": "create project"}, {"element_visible": "Delete"}),
    ]
    for good, bad in cases:
        assert run([good])[0].passed, good
        assert not run([bad])[0].passed, bad


def test_outcome_reports_detail():
    outcome = run([{"url_contains": "/login"}])[0]
    assert outcome.as_dict() == {
        "kind": "url_contains",
        "expected": "/login",
        "passed": False,
        "detail": "URL is https://app.example.com/dashboard?tab=projects",
    }
