"""Build a demo GIF from real material: still images (e.g. the rendered PR comment) and replay videos, each
with a caption bar, joined in order.

Usage:
  python scripts/make_demo_gif.py docs/media/demo.gif \
      "docs/media/pr-comment.png|The pull-request comment" \
      "everyrole-report/videos/member-creates-project-member.mp4|Replay: the form is submitted, saving is blocked" \
      --width 720 --height 610 --still-seconds 5 --video-crop 720:554:200:0

Images are scaled to the width and shown from the top; videos are scaled to fit, top-aligned. Needs ffmpeg (on PATH or
FFMPEG_PATH). Captions are passed through files, so any text is safe. --font picks a font file for the
captions; without it ffmpeg uses its default font.
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

from everyrole.video import find_ffmpeg

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}


def _escape(path):
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def caption_filter(text_file, font=None):
    font_part = f"fontfile='{_escape(font)}':" if font else ""
    return (f"drawbox=x=0:y=ih-56:w=iw:h=56:color=0x1d2433@0.92:t=fill,"
            f"drawtext={font_part}textfile='{_escape(text_file)}':fontcolor=white:fontsize=22:x=20:y=h-40")


def frame_filter(kind, width, height, video_crop=None):
    if kind == "image":  # full width, top of the image; short images are padded with white
        return f"scale={width}:-2,pad={width}:'max(ih,{height})':0:0:white,crop={width}:{height}:0:0"
    crop = f"crop={video_crop}," if video_crop else ""
    return (f"{crop}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:0:white")


def build(out, items, *, width, height, still_seconds, fps, font=None, video_crop=None, ffmpeg=None):
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("ffmpeg not found: install it or set FFMPEG_PATH")

    def run(*args):
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", *args], check=True)

    with tempfile.TemporaryDirectory(prefix="everyrole-gif-") as tmp:
        tmp = Path(tmp)
        segments = []
        for index, item in enumerate(items):
            source, _, text = item.partition("|")
            kind = "video" if Path(source).suffix.lower() in VIDEO_SUFFIXES else "image"
            text_file = tmp / f"caption{index}.txt"
            text_file.write_text(text, encoding="utf-8")
            vf = (f"{frame_filter(kind, width, height, video_crop)},setsar=1,{caption_filter(text_file, font)},"
                  f"fps={fps},format=yuv420p")
            segment = tmp / f"segment{index}.mp4"
            inputs = ["-loop", "1", "-t", str(still_seconds), "-i", source] if kind == "image" else ["-i", source]
            run(*inputs, "-vf", vf, "-an", "-c:v", "libx264", str(segment))
            segments.append(segment)
        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8")
        joined = tmp / "joined.mp4"
        run("-f", "concat", "-safe", "0", "-i", str(listing), "-vf", f"fps={fps},format=yuv420p", "-c:v", "libx264",
            str(joined))
        run("-i", str(joined), "-vf",
            f"fps={fps},split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
            str(Path(out).resolve()))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("items", nargs="+", help="path|caption (images and videos, in order)")
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=450)
    parser.add_argument("--still-seconds", type=float, default=4)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--font", help="font file for the captions")
    parser.add_argument("--video-crop", help="crop replays before scaling, as ffmpeg W:H:X:Y (e.g. 720:554:200:0)")
    args = parser.parse_args()
    print(build(args.out, args.items, width=args.width, height=args.height, still_seconds=args.still_seconds,
                fps=args.fps, font=args.font, video_crop=args.video_crop))


if __name__ == "__main__":
    main()
