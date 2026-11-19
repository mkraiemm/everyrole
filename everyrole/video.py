"""Turn a flow's recorded frames into a real-time replay video.

The engine saves one JPEG per observation, named by milliseconds since the flow started. Each frame is
shown until the next one was taken, so the video plays at the speed the flow really ran; the last frame
(the page the checks were judged on) is held a little longer. A long time with nothing new on screen (a journey
waiting for slow work, a film being made) plays as IDLE_S: the replay shows what happened, not the wait. Needs
ffmpeg (on PATH or FFMPEG_PATH); without it the frames are kept and no video is made.
"""

import os
import shutil
import subprocess
from pathlib import Path

IDLE_S = 5.0  # the longest any one frame is shown (a 40-minute wait would otherwise be a 40-minute video)


def find_ffmpeg(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")


ENCODE_TIMEOUT_S = 120  # the longest a replay's encoding may take (suite.py lets a test be quiet that long)


def frames(directory):
    found = [p for p in Path(directory).glob("*.jpg") if p.stem.isdigit()]
    return sorted(found, key=lambda p: int(p.stem))


def concat_list(frame_paths, hold_s):
    lines = ["ffconcat version 1.0"]
    for i, frame in enumerate(frame_paths):
        if i + 1 < len(frame_paths):
            duration = min((int(frame_paths[i + 1].stem) - int(frame.stem)) / 1000, IDLE_S)
        else:
            duration = hold_s
        lines += [f"file '{frame.name}'", f"duration {max(duration, 0.05):.3f}"]
    lines.append(f"file '{frame_paths[-1].name}'")  # repeated so the last duration is honored; output cut to total
    return "\n".join(lines) + "\n"


def total_seconds(frame_paths, hold_s):
    shown = sum(min((int(b.stem) - int(a.stem)) / 1000, IDLE_S) for a, b in zip(frame_paths, frame_paths[1:]))
    return shown + hold_s


def on_replay(ms, frame_ms):
    """A moment of the run (ms since it started) -> where it plays in the replay, whose idle stretches are cut to
    IDLE_S (frame_ms: when each frame was taken)."""
    times, idle = sorted(frame_ms), IDLE_S * 1000
    if not times or ms <= times[0]:
        return ms
    played, previous = times[0], times[0]
    for taken in times[1:]:
        if ms <= taken:
            break
        played, previous = played + min(taken - previous, idle), taken
    return int(played + min(ms - previous, idle))


def make_video(frames_dir, out_path, *, hold_s=2.0, fps=10, ffmpeg=None, keep_frames=False):
    """-> path of the MP4, or "" when there is nothing to encode or no ffmpeg."""
    frames_dir, out_path = Path(frames_dir), Path(out_path)
    paths = frames(frames_dir)
    exe = ffmpeg or find_ffmpeg()
    if not paths or not exe:
        return ""
    listing = frames_dir / "frames.ffconcat"
    listing.write_text(concat_list(paths, hold_s), encoding="utf-8")
    command = [
        exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing.name,
        # the fps filter repeats each frame for its full duration (the -r output option would drop the holds)
        "-vf", f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-t", f"{total_seconds(paths, hold_s):.3f}", "-c:v", "libx264", "-movflags", "+faststart",
        str(out_path.resolve()),
    ]
    try:
        done = subprocess.run(command, cwd=frames_dir, capture_output=True, text=True,
                              timeout=ENCODE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if done.returncode != 0 or not out_path.is_file():
        return ""
    if not keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return str(out_path)


def last_frame(video_path, out_path, *, ffmpeg=None):
    """Save the final frame of a replay as a picture (for replays recorded before pictures were kept).
    -> path, or "" without ffmpeg or on failure."""
    exe = ffmpeg or find_ffmpeg()
    video_path, out_path = Path(video_path), Path(out_path)
    if not exe or not video_path.is_file():
        return ""
    command = [exe, "-y", "-loglevel", "error", "-sseof", "-0.3", "-i", str(video_path.resolve()),
               "-frames:v", "1", "-q:v", "3", str(out_path.resolve())]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return str(out_path) if done.returncode == 0 and out_path.is_file() else ""


def timeline(steps, duration_ms, judged=()):
    """Captions for the player: when each step happened, in plain words. `judged`: a journey's steps
    (runner.FlowResult.steps_judged), each marked where its checks were judged."""
    marks = [{"ms": 0, "label": "Start"}]
    for step in judged:
        verdict = "passed" if step.get("passed") else "failed"
        marks.append({"ms": int(step.get("at_ms") or 0),
                      "label": f"Step {step['step']} {verdict}: {step.get('goal', '')}".strip()})
    for step in steps:
        action = (step.get("action") or "").strip()
        kind = step.get("kind") or ""
        words = {"fill": "Typed into", "click": "Clicked", "select": "Chose", "scroll": "Scrolled",
                 "wait": "Waited for the page", "submit": "Pressed Enter in"}
        label = words.get(kind, kind.capitalize())
        action = action.removeprefix("Press Enter in ") if kind == "submit" else action
        if kind not in {"scroll", "wait"} and action:
            label += f" “{action}”"
        marks.append({"ms": int(step.get("executed_ms") or 0), "label": label})
    marks.append({"ms": int(duration_ms or 0), "label": "Checks judged on this page"})
    return sorted(marks, key=lambda mark: mark["ms"])
