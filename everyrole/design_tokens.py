"""The design tokens (web/static/tokens.css) as plain colors, for what can't read CSS variables: the replays'
drawn cursor (--replay-*: drawn into the video)."""

import math
import re
from pathlib import Path

TOKENS = Path(__file__).parent / "web" / "static" / "tokens.css"


def colors(prefix, tokens_css=None):
    """The :root tokens named --<prefix>... as plain colors (#rrggbb), keyed without the prefix, '-' as '_'."""
    text = Path(tokens_css or TOKENS).read_text(encoding="utf-8")
    root = text[text.index(":root {"):]  # the block, not a comment naming it
    root = root[:root.index("}")]
    values = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", root))

    def resolve(value, depth=0):
        found = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value.strip())
        return resolve(values.get(found.group(1), ""), depth + 1) if found and depth < 10 else value.strip()

    return {key[len(prefix):].replace("-", "_"): _hex(resolve(value)) for key, value in values.items()
            if key.startswith(prefix)}


def _hex(color):
    """oklch(L C H) -> #rrggbb (mail apps don't know oklch); anything else as it is."""
    found = re.fullmatch(r"oklch\(\s*([\d.]+)(%?)\s+([\d.]+)\s+([\d.]+)\s*\)", color.strip())
    if not found:
        return color
    lightness = float(found.group(1)) / (100 if found.group(2) else 1)
    chroma, hue = float(found.group(3)), math.radians(float(found.group(4)))
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    long_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    medium_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    short_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    cone_l, cone_m, cone_s = long_ ** 3, medium_ ** 3, short_ ** 3  # the cone responses (OKLab's LMS)
    linear = (4.0767416621 * cone_l - 3.3077115913 * cone_m + 0.2309699292 * cone_s,
              -1.2684380046 * cone_l + 2.6097574011 * cone_m - 0.3413193965 * cone_s,
              -0.0041960863 * cone_l - 0.7034186147 * cone_m + 1.7076147010 * cone_s)

    def channel(value):
        value = min(max(value, 0.0), 1.0)
        value = 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
        return round(value * 255)

    return "#" + "".join(f"{channel(v):02x}" for v in linear)
