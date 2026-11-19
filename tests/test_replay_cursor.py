"""The replay's cursor and focus ring (replay_cursor.py): drawn where each input went, gliding there before it."""

from pathlib import Path

import pytest

from everyrole import replay_cursor
from everyrole.design_tokens import colors

Image = pytest.importorskip("PIL.Image")

VIEW = [100, 50]  # CSS pixels; the frames are drawn at twice that (a 2x screen)
STEP = {"step": 1, "kind": "click", "action": "Sign in", "executed_ms": 2000,
        "point": {"x": 60, "y": 25, "box": [40, 15, 40, 20], "view": VIEW, "scrolled": False}}


def frame(path, color):
    Image.new("RGB", (200, 100), color).save(path, quality=95)


def near(pixel, color, slack=40):
    return all(abs(a - b) <= slack for a, b in zip(pixel, color))


def rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def test_only_steps_with_a_whole_position_move_the_cursor():
    steps = [STEP, {"step": 2, "kind": "scroll", "executed_ms": 2500},  # no position: a scroll, or an old replay
             {"step": 3, "kind": "click", "executed_ms": 3000, "point": {"x": 1, "y": 1}},  # no window, no box
             {"step": 4, "kind": "fill", "executed_ms": 1000, "point": {**STEP["point"], "view": [0, 50]}}]
    found = replay_cursor.moves(steps)
    assert [m.at_ms for m in found] == [2000]


def test_the_cursor_glides_to_the_input_then_rings_and_ripples():
    moves = replay_cursor.moves([STEP])
    start = STEP["executed_ms"] - replay_cursor.MOVE_MS
    assert replay_cursor.look(moves, start - 1) is None  # nothing before it first moves
    first = replay_cursor.look(moves, start)
    assert (first.x, first.y) == (50, 25) and first.box is None  # from the middle of the window
    half = replay_cursor.look(moves, start + replay_cursor.MOVE_MS // 2)
    assert 50 < half.x < 60 and half.box == (40, 15, 40, 20)  # on its way; the element outlined
    at = replay_cursor.look(moves, 2000)
    assert (at.x, at.y, at.ripple) == (60, 25, 0)
    after = replay_cursor.look(moves, 2000 + replay_cursor.RIPPLE_MS + 1)
    assert (after.x, after.y, after.box, after.ripple) == (60, 25, None, None)  # the cursor stays where it went


def test_the_next_input_starts_from_the_last_one():
    second = {**STEP, "step": 2, "executed_ms": 5000, "point": {**STEP["point"], "x": 90, "y": 40}}
    moves = replay_cursor.moves([STEP, second])
    look = replay_cursor.look(moves, 5000 - replay_cursor.MOVE_MS)
    assert (look.x, look.y) == (60, 25)


def test_a_target_scrolled_into_view_is_not_pointed_at_on_the_page_before_the_scroll():
    scrolled = {**STEP, "point": {**STEP["point"], "scrolled": True}}
    moves = replay_cursor.moves([scrolled])
    assert replay_cursor.look(moves, 1900) is None
    assert replay_cursor.added_moments(moves, 0)[0] == 2000  # no frames of it gliding over the unscrolled page


def test_frames_get_the_cursor_and_the_glide_gets_frames(tmp_path):
    frame(tmp_path / "000000.jpg", "black")
    frame(tmp_path / "000500.jpg", "black")  # the page the input was decided on
    frame(tmp_path / "002600.jpg", "gray")  # after the click
    added = replay_cursor.draw(tmp_path, [STEP])
    names = sorted(int(p.stem) for p in tmp_path.glob("*.jpg"))
    assert added == len(names) - 3 and added > 5
    assert {1400, 1700, 2000, 2400} <= set(names)  # the glide and the ripple, every FRAME_MS
    palette = colors("replay-")
    with Image.open(tmp_path / "000000.jpg") as first:  # before the cursor moves: untouched
        assert near(first.getpixel((100, 50)), (0, 0, 0))
    with Image.open(tmp_path / "002000.jpg") as click:  # inside the cursor (its tip at 2x the input)
        assert near(click.getpixel((126, 72)), rgb(palette["cursor"]))
        assert near(click.getpixel((100, 24)), rgb(palette["focus"]), slack=70)  # the ring's top edge
        assert near(click.getpixel((10, 90)), (0, 0, 0))  # the rest of the page as it was
    with Image.open(tmp_path / "002600.jpg") as later:  # the next page: the cursor still there, no ring
        assert near(later.getpixel((126, 72)), rgb(palette["cursor"]))
        assert near(later.getpixel((100, 24)), (128, 128, 128))


def test_a_replay_without_positions_is_left_as_it_was(tmp_path):
    frame(tmp_path / "000000.jpg", "black")
    before = (tmp_path / "000000.jpg").read_bytes()
    old = {k: v for k, v in STEP.items() if k != "point"}
    assert replay_cursor.draw(tmp_path, [old]) == 0
    assert (tmp_path / "000000.jpg").read_bytes() == before and len(list(tmp_path.glob("*.jpg"))) == 1


def test_the_runner_keeps_the_position_and_a_clean_poster(tmp_path):
    from everyrole.runner import STEP_KEYS, FlowRunner
    from everyrole.video import find_ffmpeg

    assert "point" in STEP_KEYS
    record = tmp_path / "flow"
    record.mkdir()
    frame(record / "000000.jpg", "black")
    frame(record / "000500.jpg", "black")
    frame(record / "002600.jpg", "gray")

    class Result:
        steps, steps_judged, duration_ms = [STEP], [], 2600
        poster = video = ""
        timeline = []

    result = Result()
    FlowRunner._make_video(result, record)
    with Image.open(result.poster) as poster:  # the page the checks were judged on, without the cursor
        assert near(poster.getpixel((126, 72)), (128, 128, 128))
    labels = {m["label"]: m["ms"] for m in result.timeline}
    assert labels["Clicked “Sign in”"] == 2000  # the steps' marks still where they played
    if find_ffmpeg():
        assert Path(result.video).is_file()


def test_the_engine_reports_where_its_input_went():
    """P26: the engine's click returns the point, the element's box and the window's size (agent: step `point`)."""
    source = (Path(__file__).resolve().parents[1] / "engine" / "jev_ultrafast" / "browser.py").read_text("utf-8")
    assert "box:[b.x+o.x, b.y+o.y, b.width, b.height], view:[innerWidth, innerHeight], scrolled" in source
    agent = (Path(__file__).resolve().parents[1] / "engine" / "jev_ultrafast" / "agent.py").read_text("utf-8")
    assert '"point": done.get("point")' in agent
