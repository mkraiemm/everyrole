"""The way in, as a test runs it: a test starts where its kind of user really starts (the visitor on the home page, a
signed-in user on the page sign-in lands on) and clicks its way to the page it checks, never by opening that page's
address. Opening /pricing directly proves /pricing works, not that anyone can get there.

A test keeps its own page as `start` and carries `way_in`:
{"<kind of user, "" for the visitor>": {"entry": "/", "clicks": [{"label": "Pricing", "to": "/pricing"}]}}.
When it runs (spec.parse_flows), it becomes a journey: open the entry, one step per click (checked by the address it
leads to), then its own task and checks.
"""

from .site import split as split_page


def steps(way, goal, expect, template):
    """The journey a test with a way in runs: the clicks, each checked by where it leads (a page whose address
    holds an id is checked by the next step instead), then the test's own goal and checks."""
    out = []
    for click in way["clicks"]:
        path = address_path(click["to"])
        # a link inside a closed menu (an account menu) is only clickable once the menu is open
        click_it = (f"Open “{click['menu']}”, then click “{click['label']}”" if click.get("menu")
                    else f"Click “{click['label']}”")
        # the link itself, so the run clicks it by where it leads (runner._click_link), not by guessing from words
        link = {k: click[k] for k in ("label", "to", "menu") if click.get(k)}
        if ":" in path or path == "/":
            step = {"goal": click_it, "expect": [], "checked_by_next": True, "click": link}
        else:  # where it leads tells same-named controls apart ("Start free" in the menu, and one that stays)
            step = {"goal": f"{click_it}, the link to {path}", "expect": [{"url_contains": path}], "click": link}
        out.append(step)
    # its checks are told apart from the page the last click was on (they already hold once the click arrived)
    out.append({"goal": goal, "expect": list(expect), "reached_by_previous": True})
    return template(way["entry"]), out


def address_path(to):
    """A click's address as the path its URL check looks for: "/login?mode=signup" -> "/login" (a page's own query
    can gain parameters on the way, "next=" say; the click itself is told apart by it, runner._click_link)."""
    return split_page(to)[1].split("?", 1)[0].split("#", 1)[0] or "/"
