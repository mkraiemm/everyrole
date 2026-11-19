from everyrole import assertions
from everyrole.assertions import PageFacts

FACTS = PageFacts(
    url="https://shop.example.com/dashboard?tab=projects",
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
        "detail": "URL is https://shop.example.com/dashboard?tab=projects",
    }


def test_a_hash_in_a_check_stands_for_any_number():
    from everyrole.assertions import REGISTRY, PageFacts

    page = PageFacts(url="https://shop.io/returns", text="Returns\n1. Refunds\n2. Damaged, defective, or wrong",
                     labels=("Add 2 to cart",))
    check = lambda kind, value: REGISTRY[kind](value).check(page).passed  # noqa: E731
    assert check("text_visible", "#. Refunds") and check("text_visible", "#. Damaged, defective, or wrong")
    assert check("element_visible", "Add # to cart") and not check("text_absent", "#. Refunds")
    assert not check("text_visible", "#. Shipping")  # still has to be there
    assert check("text_visible", "1. Refunds")  # a real number still matches itself


def test_a_reworded_text_is_found_and_a_missing_one_is_not():
    from everyrole.assertions import REGISTRY, PageFacts

    page = PageFacts(url="https://shop.example.com/pricing", labels=("Make a free report", "Sign up"),
                     text="PRICING\nStart free. Pay the day you want a report.\nNo subscription to make reports.")
    reworded = REGISTRY["text_visible"]("Start free. Buy a report when you want one.").check(page)
    assert not reworded.passed and reworded.closest == "Start free. Pay the day you want a report."
    gone = REGISTRY["text_visible"]("BILLING & INVOICES").check(page)
    assert not gone.passed and gone.closest == ""  # nothing like it: really missing
    button = REGISTRY["element_visible"]("Make a free video").check(page)
    assert button.closest == "Make a free report"


def test_a_heading_split_over_lines_is_recognised_and_a_short_name_never_is():
    from everyrole.assertions import REGISTRY, PageFacts

    page = PageFacts(url="https://shop.example.com/pricing", labels=(),
                     text="PRICING\nStart free.\nPay the day you want a film.\nYour plan\nFree")
    pricing = REGISTRY["text_visible"]("Start free. Buy a film when you want one.").check(page)
    assert pricing.closest == "Start free. Pay the day you want a film."
    films = REGISTRY["text_visible"]("Your films").check(page)  # the test account's own list: missing, not reworded
    assert films.closest == ""


def test_a_save_check_with_a_number_marker_matches_any_row():
    from everyrole.assertions import PageFacts, build, evaluate

    facts = PageFacts(url="https://app.io/admin", text="", labels=(),
                      blocked=(("POST", "https://app.io/admin/workspaces/13/plan"),))
    assert evaluate(build([{"tried_to_save": "/admin/workspaces/#/plan"}]), facts)[0].passed
    assert not evaluate(build([{"tried_to_save": "/admin/workspaces/#/credits"}]), facts)[0].passed
    assert not evaluate(build([{"tried_to_save": "/admin/workspaces/3/plan"}]), facts)[0].passed  # exact still exact


def test_a_count_that_grew_is_the_same_text_reworded_never_broken():
    """Seen on EveryRole itself: "Tests 38" failed once tests were added (the page said "Tests 32")."""
    from everyrole.assertions import other_number

    assert other_number("Tests 38", "Home\nTests\n32\nRuns") == "Tests 32"  # the page's own words
    assert other_number("$19 / month", "Pro $29 / month") == "$29 / month"
    assert other_number("Run #14", "Run #15 · Oct 08") == "Run #15"
    assert other_number("Your films", "Your films") == ""  # no number: never by this rule
    assert other_number("Tests 38", "Runs 12") == ""


def test_words_inside_an_empty_field_are_visible_text():
    """Conduit (2026-11-18): its sign-up form writes "Email" and "Password" inside its empty fields (placeholders), not
    in the page's text; checks of them failed while a person sees them."""
    facts = PageFacts(url="https://conduit.example/#/register", text="Sign up\nHave an account?",
                      labels=("Username", "Email", "Password", "Sign up"))
    shown = assertions.evaluate(assertions.build([{"text_visible": "Email"}]), facts)[0]
    assert shown.passed and "Email" in shown.detail
    assert not assertions.evaluate(assertions.build([{"text_visible": "Billing"}]), facts)[0].passed


def test_words_said_in_a_browser_alert_are_shown_too():
    """demoblaze (2026-11-18): "Product added." and "Sign up successful." are browser alerts, never in the page's
    text: the check failed four rounds running."""
    from everyrole.assertions import PageFacts, build, evaluate

    facts = PageFacts(url="https://shop.example/", text="Home Cart", labels=(), dialogs=("Product added.",))
    [shown] = evaluate(build([{"text_visible": "Product added"}]), facts)
    assert shown.passed and shown.detail == "shown in a browser alert"
    [missing] = evaluate(build([{"text_visible": "Product added"}]), PageFacts(url="u", text="Home", labels=()))
    assert not missing.passed
