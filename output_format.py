"""Pure parsing of the output-format and magic flags: --gif, --frames, --ms, --svg, --boring.

Mirrors image_size.parse_size_flags's tokenize-strip-rejoin shape, and like it has no
Discord/OpenAI/bot side effects, so test_output_format.py exercises it directly.

  --gif                         animated GIF output (gpt-image-2.6 family only)
  --frames N / --frames=N       GIF frame count, 2-16 (implies --gif)
  --ms N / --ms=N               milliseconds per GIF frame, 20-2000 (implies --gif)
  --svg                         SVG output (generations only; never in a pipe or a remix)
  --boring                      no magic mixins for this whole command, pipes included

The API's own limits (frame_count 2-16, frame_duration_ms 20-2000, rounded to 10 ms)
are checked here so a bad value is refused before anything is spent.
"""

from typing import NamedTuple

PNG, GIF, SVG = "png", "gif", "svg"

FRAMES_MIN, FRAMES_MAX = 2, 16
MS_MIN, MS_MAX = 20, 2000
DEFAULT_FRAMES = 9
DEFAULT_MS = 200


class OutputFlags(NamedTuple):
    kind: str = PNG          # PNG / GIF / SVG
    frames: int = DEFAULT_FRAMES
    ms: int = DEFAULT_MS
    boring: bool = False
    gif_requested: bool = False   # an explicit --gif/--frames/--ms appeared
    svg_requested: bool = False


class FlagError(ValueError):
    """A user-facing problem with the flags. str(e) is the message to send."""


def _int_value(name, raw, lo, hi):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise FlagError(f"`--{name} {raw}` isn't a number — use something from {lo} to {hi}.")
    if not lo <= value <= hi:
        raise FlagError(f"`--{name}` has to be from {lo} to {hi}, not {value}.")
    return value


def parse_output_flags(text):
    """Split `text` into (remaining_text, OutputFlags). Raises FlagError for bad values or
    for asking for both --gif and --svg.

    As with parse_size_flags, text with no recognized flag comes back verbatim; otherwise
    the remainder is rebuilt from tokens. `--frames`/`--ms` consume the next token as
    their value (or take `=value`); a trailing one with no value is an error rather than
    being silently dropped, since the user clearly meant to set something.
    """
    if text is None:
        return None, OutputFlags()
    frames, ms = DEFAULT_FRAMES, DEFAULT_MS
    gif = svg = boring = False
    matched = False
    words = []
    tokens = text.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        name, eq, val = low.partition("=")
        if low == "--gif":
            gif = matched = True
        elif low == "--svg":
            svg = matched = True
        elif low == "--boring":
            boring = matched = True
        elif name in ("--frames", "--ms"):
            key = name[2:]
            if eq:
                raw = tok.split("=", 1)[1]
            elif i + 1 < len(tokens):
                raw = tokens[i + 1]
                i += 1
            else:
                raise FlagError(f"`--{key}` needs a number after it.")
            if key == "frames":
                frames = _int_value("frames", raw, FRAMES_MIN, FRAMES_MAX)
            else:
                ms = _int_value("ms", raw, MS_MIN, MS_MAX)
            gif = matched = True
        else:
            words.append(tok)
        i += 1
    if gif and svg:
        raise FlagError("Pick one: `--gif` or `--svg`, not both.")
    kind = GIF if gif else SVG if svg else PNG
    remaining = " ".join(words) if matched else text
    return remaining, OutputFlags(kind, frames, ms, boring, gif, svg)


def extension(kind):
    return {GIF: "gif", SVG: "svg"}.get(kind, "png")


# --- GIF frame sizing --------------------------------------------------------------------
#
# The API packs GIF frames into one sprite sheet that must fit the model's pixel budget
# (8,294,400) and 3840px edge limit, so the per-frame size shrinks as frames go up.
# Measured against the live API (2026-11-07): 9 frames at 960x960 and 1152x640 work,
# 16 frames at 720x720 works, 9 at 1024x1024 and 16 at 1024x1024 are refused.
# `auto` picks 512x512. Default frames are 768 on a side (a 9-frame low GIF there costs
# about 2.4x a low still and comes out around 3 MB); an exact --res is scaled down to
# fit. If the API still refuses the sheet, the caller retries once with "auto".

GIF_DEFAULT_EDGE = 768
GIF_STEP = 16


def gif_frame_limits(frames):
    """(max long edge, max pixels) for one frame at this frame count."""
    if frames <= 9:
        return 1152, 960 * 960
    return 960, 720 * 720


def _fit(width, height, frames):
    max_edge, max_px = gif_frame_limits(frames)
    scale = min(1.0, max_edge / max(width, height), (max_px / (width * height)) ** 0.5)
    w = max(GIF_STEP, int(width * scale) // GIF_STEP * GIF_STEP)
    h = max(GIF_STEP, int(height * scale) // GIF_STEP * GIF_STEP)
    return f"{w}x{h}"


def gif_frame_size(frames, orientation=None, res_wh=None, src_wh=None):
    """The per-frame size to request for a GIF.

    Precedence matches the still-image flags: an explicit --res wins, then an
    orientation flag, then the source image's shape (edits), then a square. Aspect
    ratio is clamped to 1:3..3:1 like stills."""
    edge = GIF_DEFAULT_EDGE if frames <= 9 else 720
    if res_wh:
        w, h = res_wh
    elif orientation == "landscape":
        w, h = edge * 3 // 2, edge
    elif orientation == "portrait":
        w, h = edge, edge * 3 // 2
    elif orientation == "square" or not src_wh or not all(src_wh):
        w, h = edge, edge
    else:
        sw, sh = src_wh
        ratio = max(1 / 3, min(3.0, sw / sh))
        w, h = (edge * ratio, edge) if ratio >= 1 else (edge, edge / ratio)
    ratio = w / h
    if ratio > 3:
        w = h * 3
    elif ratio < 1 / 3:
        h = w * 3
    return _fit(w, h, frames)
