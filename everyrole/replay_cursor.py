"""The cursor and focus ring in a replay: before each click or typing, a cursor glides to where the input went and a
ring outlines the element acted on; at the input itself a ripple shows the click.

The engine records each input's screen position with its step (P26 `point`: the click's x, y, the element's box,
the window's size, in CSS pixels). This draws on the recorded frames (one JPEG per observation, named by ms since
the flow started) before they are encoded: the frames on screen while the cursor moves get the cursor drawn, and
frames are added in between (one every FRAME_MS) so the movement plays smoothly. Drawn into the video, the cursor is
the same everywhere the replay is shown (the player, a shared link, a downloaded MP4).

Replays recorded before positions were kept have no `point` on their steps: nothing is drawn for them. Needs Pillow
(pip install "everyrole[replays]"); without it the replay is made as before, without the cursor.
"""

from dataclasses import dataclass
from pathlib import Path

from .design_tokens import colors

MOVE_MS = 600  # the cursor glides to the target in this long, ending at the moment of the input
FRAME_MS = 100  # a drawn frame every 100 ms while it moves (the replay plays at 10 fps: video.make_video)
RIPPLE_MS = 400  # the click's ripple and the ring stay this long after the input
RING_FROM = 0.5  # the ring shows once the cursor is this far along its way
CURSOR_PX = 22  # the cursor's height in CSS pixels (scaled like the page in the frame)
RING_PAD_PX = 4  # the ring sits this far outside the element
RING_PX = 3  # the ring's line width
RIPPLE_PX = 26  # the ripple's widest radius
QUALITY = 80  # JPEG quality of a drawn frame (the engine records at 72)
# the arrow, in units of its height: tip at (0, 0)
ARROW = ((0, 0), (0, 0.82), (0.22, 0.63), (0.37, 0.98), (0.5, 0.92), (0.35, 0.58), (0.62, 0.58))


@dataclass(frozen=True)
class Move:
    at_ms: int  # when the input happened
    x: float  # where it went, CSS pixels from the window's top left
    y: float
    box: tuple  # the element: x, y, width, height
    view: tuple  # the window: width, height
    scrolled: bool  # the element was scrolled into view first (the frames before it show the page unscrolled)


@dataclass(frozen=True)
class Look:
    """What is drawn at one moment: the cursor where, the ring around which box, how far the ripple is."""
    x: float
    y: float
    view: tuple
    box: tuple | None
    ripple: float | None  # 0..1 from the input on, None before it or once it's over


def moves(steps):
    """The steps with a recorded screen position, in time order."""
    found = []
    for step in steps or ():
        point = step.get("point")
        if not isinstance(point, dict) or step.get("executed_ms") is None:
            continue
        try:
            view = tuple(float(v) for v in point["view"])
            box = tuple(float(v) for v in point["box"])
            move = Move(int(step["executed_ms"]), float(point["x"]), float(point["y"]), box, view,
                        bool(point.get("scrolled")))
        except (KeyError, TypeError, ValueError):
            continue
        if len(view) == 2 and view[0] > 0 and view[1] > 0 and len(box) == 4:
            found.append(move)
    return sorted(found, key=lambda move: move.at_ms)


def look(all_moves, ms):
    """What the replay shows at `ms`: None before the cursor first moves."""
    started = [m for m in all_moves if m.at_ms - MOVE_MS <= ms]
    if not started:
        return None
    move = started[-1]
    before = started[-2] if len(started) > 1 else None
    if ms >= move.at_ms:
        done = ms - move.at_ms
        ripple = done / RIPPLE_MS if done <= RIPPLE_MS else None
        return Look(move.x, move.y, move.view, move.box if ripple is not None else None, ripple)
    # on its way: from the last input (or the middle of the window), eased in and out
    origin = (before.x, before.y) if before else (move.view[0] / 2, move.view[1] / 2)
    if move.scrolled:  # the frames still show the page before it scrolled: the cursor waits for the input
        return Look(*origin, move.view, None, None) if before else None
    part = (ms - (move.at_ms - MOVE_MS)) / MOVE_MS
    eased = part * part * (3 - 2 * part)
    x, y = origin[0] + (move.x - origin[0]) * eased, origin[1] + (move.y - origin[1]) * eased
    return Look(x, y, move.view, move.box if part >= RING_FROM else None, None)


def added_moments(all_moves, first_frame_ms):
    """The moments to add a drawn frame at: along each glide and through each ripple, after the first frame."""
    moments = set()
    for move in all_moves:
        start = move.at_ms if move.scrolled else move.at_ms - MOVE_MS
        moments.update(range(start, move.at_ms + RIPPLE_MS + 1, FRAME_MS))
    return sorted(m for m in moments if m > first_frame_ms)


def draw(record_dir, steps):
    """Draw the cursor and focus ring on the frames in `record_dir` and add the frames between.
    -> how many frames were added (0 when nothing has a position, or without Pillow)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return 0
    record_dir = Path(record_dir)
    all_moves = moves(steps)
    frames = {int(p.stem): p for p in record_dir.glob("*.jpg") if p.stem.isdigit()} if record_dir.is_dir() else {}
    if not all_moves or not frames:
        return 0
    palette = colors("replay-")
    taken = sorted(frames)
    added = [m for m in added_moments(all_moves, taken[0]) if m not in frames]
    base, picture, made = None, None, 0
    # in time order: each moment shows the last frame taken by then, read once and drawn on before it is saved
    # again, so every moment's picture comes from a clean frame
    for ms in sorted({*added, *taken}):
        latest = max(t for t in taken if t <= ms)
        if latest != base:
            with Image.open(frames[latest]) as opened:
                base, picture = latest, opened.convert("RGB")
        seen = look(all_moves, ms)
        if seen is None:
            continue
        frame = picture.copy()
        _paint(ImageDraw.Draw(frame), frame.width / seen.view[0], seen, palette)
        frame.save(record_dir / f"{ms:06d}.jpg", quality=QUALITY)
        made += ms not in frames
    return made


def _paint(pen, scale, seen, palette):
    """The ring around the element, the ripple at the input, then the cursor on top."""
    focus, edge, cursor = palette["focus"], palette["cursor_edge"], palette["cursor"]
    if seen.box is not None:
        x, y, w, h = (v * scale for v in seen.box)
        pad = RING_PAD_PX * scale
        pen.rounded_rectangle((x - pad, y - pad, x + w + pad, y + h + pad), radius=6 * scale, outline=focus,
                              width=max(1, round(RING_PX * scale)))
    if seen.ripple is not None:
        radius = RIPPLE_PX * scale * (0.3 + 0.7 * seen.ripple)
        cx, cy = seen.x * scale, seen.y * scale
        pen.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=focus,
                    width=max(1, round(RING_PX * scale)))
    size = CURSOR_PX * scale
    tip = (seen.x * scale, seen.y * scale)
    pen.polygon([(tip[0] + dx * size, tip[1] + dy * size) for dx, dy in ARROW], fill=cursor, outline=edge,
                width=max(1, round(1.5 * scale)))
