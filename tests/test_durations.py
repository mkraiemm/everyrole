"""Tests classed by the time they take, measured: a deploy's check never waits for the app's slow
work (30-40 minutes of it), and people see about how long a run takes."""


from everyrole import durations
from everyrole.spec import Config, parse_flows

MIN = 60_000


def run(name, ms, status="passed"):
    return {"name": name, "status": status, "duration_ms": ms}


def test_a_tests_time_is_the_median_of_its_last_runs_that_really_ran_it():
    past = [({}, [run("film", 38 * MIN), run("home", 20_000)]),
            ({}, [run("film", 0, "not_run"), run("home", 25_000)]),  # couldn't start: no time
            ({}, [run("film", 41 * MIN, "failed_expect"), run("home", 90_000)])]
    times = durations.measured(past)
    assert times == {"film": int((38 + 41) / 2 * MIN), "home": 25_000}
    assert durations.class_of(times["home"]) == durations.QUICK and durations.class_of(times["film"]) == durations.LONG
    assert durations.class_of(5 * MIN) == durations.MEDIUM and durations.class_of(None) is None


def test_an_estimate_shares_the_tests_between_the_parallel_lanes():
    times = {"a": 30_000, "b": 30_000, "c": 60_000, "film": 40 * MIN}
    assert durations.estimate(["a", "b", "c"], times, 3) == 60_000  # the longest one
    assert durations.estimate(["a", "b", "c"], times, 1) == 120_000  # one after the other
    assert durations.estimate(["a", "new"], times, 1) == 60_000  # never measured: as the measured ones
    assert durations.estimate(["new"], times, 3) is None  # nothing measured: no guess
    assert durations.spoken(40 * MIN) == "about 40 min" and durations.spoken(42_000) == "about 40 s"


def flows(*specs):
    raw = [{"name": n, "start": "{{BASE_URL}}/", "goal": "Open the page", "expect": [{"text_visible": "x"}],
            **({"measured_ms": ms} if ms else {})} for n, ms in specs]
    return [parse_flows(r, config=Config(), base_url="https://app.example.com", env={})[0] for r in raw]


def test_a_deploys_check_leaves_the_long_tests_for_the_daily_run():
    kept, long = durations.without_long(flows(("home", 0), ("film", 0)), [({}, [run("home", 20_000),
                                                                                run("film", 38 * MIN)])])
    assert [f.name for f in kept] == ["home"] and long == {"film": 38 * MIN}
    # a journey kept from a change has its time before its first run as a test
    kept, long = durations.without_long(flows(("film", 38 * MIN)), [])
    assert kept == [] and list(long) == ["film"]

