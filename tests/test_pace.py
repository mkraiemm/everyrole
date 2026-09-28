import subprocess
import sys
import tempfile
import threading

from everyrole.pace import Pace, Settings, settings_of


def _pace(**kw):
    settings = Settings(**{"pages_at_once": 2, "min_gap_s": 0, "max_gap_s": 15, "page_timeout_s": 60, **kw})
    return Pace(tempfile.mkdtemp(prefix="pace-"), settings, sleep=lambda s: None, resolve=lambda host: True)


def test_defaults_come_from_the_schema():
    assert settings_of(None) == {"pages_at_once": 3, "min_gap_s": 1, "max_gap_s": 15, "page_timeout_s": 60}


def test_no_more_pages_load_at_once_than_allowed_in_the_whole_job():
    first, second = _pace(pages_at_once=1), None
    second = Pace(first.dir, first.settings, sleep=lambda s: None, resolve=lambda host: True)  # another process
    ticket = first.acquire("https://app.io/a")
    started = threading.Event()

    def other():
        second.acquire("https://app.io/b")
        started.set()

    worker = threading.Thread(target=other, daemon=True)
    worker.start()
    assert not started.wait(0.3)  # the job's one place is taken
    first.release(ticket)
    assert started.wait(5)


def test_the_gap_follows_how_fast_pages_load():
    pace = _pace(pages_at_once=2, min_gap_s=1, max_gap_s=15)
    assert pace.gap({"loads": []}) == 1  # nothing measured yet: the shortest gap
    assert pace.gap({"loads": [4, 6, 8]}) == 3  # pages take 6 s, two at a time: one may start every 3 s
    assert pace.gap({"loads": [60, 60]}) == 15  # a very slow network: never slower than the longest gap
    assert pace.gap({"loads": [0.2, 0.4]}) == 1  # a fast one: never faster than the shortest gap


def test_a_load_is_measured_when_the_page_gives_its_place_back():
    now = [100.0]
    pace = Pace(tempfile.mkdtemp(), Settings(2, 0, 15, 60), clock=lambda: now[0], sleep=lambda s: None,
                resolve=lambda host: True)
    ticket = pace.acquire("https://app.io/")
    now[0] += 7
    pace.release(ticket)
    with pace._state() as state:
        assert state["loads"] == [7] and state["open"] == {}


def test_when_the_address_stops_resolving_nothing_starts_and_the_pause_is_recorded():
    answers = iter([False, False, True])
    now = [0.0]

    def sleep(seconds):
        now[0] += 11  # past the DNS check interval each time it waits

    pace = Pace(tempfile.mkdtemp(), Settings(2, 0, 15, 60), clock=lambda: now[0], sleep=sleep,
                resolve=lambda host: next(answers))
    seen = []
    real = pace._dns_fine

    def spy(host):
        ok = real(host)
        seen.append((ok, pace.status()["paused"] is not None))
        return ok

    pace._dns_fine = spy
    pace.acquire("https://app.io/")
    assert seen == [(False, True), (False, True), (True, False)]  # paused while it failed, cleared once it resolves


def test_a_page_whose_process_died_gives_its_place_back():
    pace = _pace(pages_at_once=1)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with pace._state() as state:
        state["open"]["gone"] = {"pid": dead.pid, "at": 0, "url": "https://app.io/"}
        state["last_start"] = 0
    ticket = pace.acquire("https://app.io/next")  # would wait forever if the dead page kept its place
    assert ticket


def test_a_page_access_batch_shares_one_browser_session():
    from everyrole.access import AccessChecker
    from everyrole.assertions import PageFacts

    opened = []

    class Tab:
        def __init__(self, url, **options):
            self.url, self.options = url, options
            self.context = options.get("context") or f"ctx-{len(opened)}"
            opened.append(self)

        def observe(self, screenshot=False):
            return {}

        def close(self):
            pass

    facts = lambda browser, page: PageFacts(url=browser.url, text="", labels=())  # noqa: E731
    checker = AccessChecker(browser_factory=Tab, facts_reader=facts, sleep=lambda s: None)
    checker.run(base_url="https://app.io", persona="p", rules=[("/a", "allow"), ("/b", "allow"), ("/c", "allow")],
                denied_when=[], settle_s=0)
    first = opened[0]
    assert first.options.get("isolated") and "context" not in first.options
    assert all(tab.options.get("context") == first.context and not tab.options.get("isolated")
               for tab in opened[1:3])
