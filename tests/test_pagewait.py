from everyrole.pagewait import chain, wait_until_stable


class Page:
    """Returns the scripted page states in order, then repeats the last one."""

    def __init__(self, states):
        self.states, self.reads = list(states), 0

    def evaluate(self, _expression):
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        return state


def wait(page, max_s=10):
    ticks = iter(i * 0.4 for i in range(10000))
    return wait_until_stable(page, max_s=max_s, poll_s=0.4, stable_reads=3, clock=lambda: next(ticks),
                             sleep=lambda s: None)


SKELETON = ["complete", 200, 90, "https://app.io/projects", 8]


def test_frozen_skeleton_with_a_request_in_flight_is_not_settled():
    waiting = [*SKELETON, 1]  # the screen does not change, but the projects request is still running
    loaded = ["complete", 900, 300, "https://app.io/projects", 9, 0]
    page = Page([waiting] * 6 + [loaded])
    assert wait(page)
    assert page.reads >= 9  # it waited through all six in-flight reads, then saw the loaded page settle


def test_page_that_never_goes_quiet_is_judged_at_the_limit():
    page = Page([[*SKELETON, 1]])  # e.g. long polling
    assert not wait(page, max_s=3)


def test_quiet_page_settles_quickly():
    page = Page([[*SKELETON, 0]])
    assert wait(page) and page.reads == 3


def test_chain_runs_hooks_in_order_and_skips_missing():
    calls = []
    chain(lambda b: calls.append("restore"), None, lambda b: calls.append("track"))(object())
    assert calls == ["restore", "track"]
