"""Pure size selection for image generation (&paint family) and edits (&remix).

Both the /v1/images/generations and /v1/images/edits endpoints (gpt-image-2) accept
an arbitrary WxH size subject to: divisible by 16, aspect ratio in [1/3, 3], and
within a max box (documented up to 3840x2160). The edit docs only advertise the three
standard sizes + "auto", but the endpoint honors arbitrary sizes in practice, so both
&paint and &remix coerce a requested size into that space with `coerce_generation_size`
rather than snapping to a fixed size.

- The command flags (--square/--landscape/--portrait/--res) are parsed here
  (`parse_size_flags`/`parse_resolution`).
- `resolve_generation_size` (paint family) and `resolve_edit_size` (remix) map a
  parsed request to a final size. They differ only in their no-flag default: paint
  defaults to a square, while remix matches the first attachment's own dimensions as
  closely as a valid size allows (falling back to "auto" when Discord didn't report
  them).

No Discord/OpenAI/bot side effects -- bot_ross.py can't be imported under test because
module load ends in bot.run(), so this logic lives here for test_image_size.py to
exercise directly.
"""

import re

# The three standard sizes both endpoints accept, reused as the orientation presets.
SQUARE = "1024x1024"
LANDSCAPE = "1536x1024"
PORTRAIT = "1024x1536"
AUTO = "auto"


def describe_edit_size(size):
    """Human-readable orientation label for a size, for logging only (never shown in
    Discord). Handles the standard constants plus any arbitrary "WxH" (now that --res
    can coerce to an arbitrary edit size); anything unparseable is "unknown"."""
    known = {SQUARE: "square", LANDSCAPE: "landscape", PORTRAIT: "portrait", AUTO: "auto"}
    if size in known:
        return known[size]
    try:
        w, h = parse_resolution(size)
    except (ValueError, TypeError):
        return "unknown"
    if w > h:
        return "landscape"
    if h > w:
        return "portrait"
    return "square"


# --- Generation (/v1/images/generations, gpt-image-2) sizing -----------------------
#
# Unlike the edit endpoint (only 3 fixed sizes + auto), the generate endpoint also
# accepts an arbitrary WxH, subject to: divisible by 16, aspect ratio in [1/3, 3], and
# within a max box (documented by OpenAI as up to 3840x2160). --res on a generation
# command is coerced to satisfy these constraints (see coerce_generation_size).
#
# The three orientation presets reuse the exact same SQUARE/LANDSCAPE/PORTRAIT values
# already defined above for &remix's edit sizing -- both endpoints happen to accept
# these three standard sizes, so there is no separate "generation" square/landscape/
# portrait constant.
ORIENTATIONS = {"square": SQUARE, "landscape": LANDSCAPE, "portrait": PORTRAIT}

GEN_STEP      = 16    # generated dims must be multiples of 16
GEN_MAX_RATIO = 3.0   # aspect ratio clamped to [1/3, 3]
GEN_MAX_LONG  = 3840  # longer side cap
GEN_MAX_SHORT = 2160  # shorter side cap ("max 3840x2160")
GEN_MIN_SHORT = 256   # conservative floor for the shorter side

# coerce_generation_size's rounding step (nearest multiple of GEN_STEP) can only ever
# leave a dimension <= a cap that is itself already a multiple of GEN_STEP (see the
# module docstring / coerce_generation_size below for why) -- if these constants are
# ever changed to non-multiples of 16, that guarantee breaks, so assert it here rather
# than let it fail silently later.
assert GEN_MAX_LONG % GEN_STEP == 0
assert GEN_MAX_SHORT % GEN_STEP == 0
assert GEN_MIN_SHORT % GEN_STEP == 0


def parse_size_flags(text):
    """Split a generation-command prompt into (remaining_text, orientation, res_raw).

    Mirrors release_image.parse_release_args's tokenize-strip-rejoin shape. Recognized
    flags, case-insensitive, are removed from `text`; every other whitespace-separated
    token is rejoined (space-separated) into `remaining_text`.

      --square / --landscape / --portrait  -> orientation (last one wins)
      --res <value>                        -> res_raw = the next token, verbatim
      --res=<value>                        -> res_raw = the text after '='
      --res  (no following token)          -> dropped; res_raw stays whatever it was

    `res_raw` is returned RAW (untouched, original casing) -- this function does no
    size validation; that's parse_resolution's job. `orientation` is one of
    "square"/"landscape"/"portrait"/None.

    When NO recognized flag is present, `text` is returned verbatim (whitespace and
    all), so the common no-flags prompt is passed through untouched. Only when a flag
    is actually stripped is the remainder rebuilt from tokens (which collapses runs of
    whitespace to single spaces -- acceptable, since the caller explicitly edited it).

    Like release_image.parse_release_args's "--version", "--res" blindly consumes the
    very next token as its value even if that token itself looks like a flag (e.g.
    "--res --landscape" makes res_raw="--landscape") -- this is intentional parity with
    the existing convention, not a bug to fix here.
    """
    orientation = None
    res_raw = None
    matched = False  # did we strip any recognized flag? if not, return text verbatim
    words = []
    tokens = text.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low in ("--square", "--landscape", "--portrait"):
            orientation = low[2:]
            matched = True
        elif low == "--res" and i + 1 < len(tokens):
            res_raw = tokens[i + 1]
            matched = True
            i += 1
        elif low == "--res":
            matched = True  # trailing --res with no value: drop it, res_raw unset
        elif low.startswith("--res="):
            res_raw = tok.split("=", 1)[1]
            matched = True
        else:
            words.append(tok)
        i += 1
    remaining = " ".join(words) if matched else text
    return remaining, orientation, res_raw


_RESOLUTION_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")


def parse_resolution(res_raw):
    """Parse a raw '--res' value into (width, height) ints.

    Accepts "<w>x<h>" or "<w>X<h>", with optional whitespace around the 'x' and at the
    ends. Raises ValueError for anything else: non-numeric, missing a part, extra
    parts, non-positive, or non-integer (e.g. "16.5x16").
    """
    match = _RESOLUTION_RE.match(res_raw or "")
    if not match:
        raise ValueError(f"not a WIDTHxHEIGHT resolution: {res_raw!r}")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError(f"resolution must be positive: {res_raw!r}")
    return width, height


def coerce_generation_size(width, height):
    """Coerce an arbitrary (width, height) into a "WxH" string valid for the
    /v1/images/generations endpoint: both dimensions positive multiples of GEN_STEP,
    aspect ratio within [1/GEN_MAX_RATIO, GEN_MAX_RATIO], and within the
    GEN_MAX_LONG x GEN_MAX_SHORT box. Deterministic pipeline, in order:

      1. Clamp ratio to [1/3, 3] by pulling the longer side in to `shorter * 3`.
      2. Fit the max box, preserving ratio (factor = min(MAX_LONG/long,
         MAX_SHORT/short, 1) -- this can only shrink, never grow).
      3. Raise to the floor: if the shorter side is now < GEN_MIN_SHORT, scale UP
         preserving ratio so the shorter side becomes exactly GEN_MIN_SHORT.
      4. Round each dimension independently to the nearest multiple of GEN_STEP
         (round-half-up: int(x / GEN_STEP + 0.5) * GEN_STEP, floored at GEN_STEP so
         the result is never 0).
      5. Re-clamp: rounding each dimension INDEPENDENTLY in step 4 can, on its own,
         push the ratio back outside [1/3, 3] even though step 1 guaranteed it going
         in (worked counter-example in the module's test file: (783, 261) has an
         exact ratio of 3.0 after step 1-3, but rounds to (784, 256) -- ratio 3.0625,
         which violates the bound). So after rounding, check again: if the longer
         rounded side now exceeds `shorter_rounded * GEN_MAX_RATIO`, snap it down to
         exactly `shorter_rounded * GEN_MAX_RATIO`. This is always an EXACT multiple
         of GEN_STEP already (the shorter side is a multiple of GEN_STEP, and
         GEN_MAX_RATIO is the integer 3), so no further rounding is needed and no
         further violation is possible.

    Returns "WxH" (both components plain ints, no leading zeros).

    Precondition (not enforced): width > 0 and height > 0. Every caller in this
    codebase (resolve_generation_size, via parse_resolution) already guarantees this;
    behavior for non-positive input is undefined/untested, matching how
    parse_resolution is the actual validation gate.
    """
    w, h = float(width), float(height)

    # Step 1: clamp ratio into [1/3, 3] by pulling the longer side toward the shorter.
    if w > h * GEN_MAX_RATIO:
        w = h * GEN_MAX_RATIO
    elif h > w * GEN_MAX_RATIO:
        h = w * GEN_MAX_RATIO

    # Step 2: fit the max box, preserving ratio (never upscales here).
    long_side, short_side = max(w, h), min(w, h)
    factor = min(GEN_MAX_LONG / long_side, GEN_MAX_SHORT / short_side, 1.0)
    w *= factor
    h *= factor

    # Step 3: raise the shorter side to the floor, preserving ratio (never downscales
    # here -- GEN_MIN_SHORT is far below GEN_MAX_SHORT so this can't re-violate the box).
    short_side = min(w, h)
    if short_side < GEN_MIN_SHORT:
        factor = GEN_MIN_SHORT / short_side
        w *= factor
        h *= factor

    # Step 4: round each dimension independently to the nearest multiple of GEN_STEP.
    def _round_step(x):
        return max(GEN_STEP, int(x / GEN_STEP + 0.5) * GEN_STEP)

    w, h = _round_step(w), _round_step(h)

    # Step 5: re-clamp ratio, which independent per-axis rounding can have violated.
    if w > h * GEN_MAX_RATIO:
        w = int(h * GEN_MAX_RATIO)
    elif h > w * GEN_MAX_RATIO:
        h = int(w * GEN_MAX_RATIO)

    return f"{int(w)}x{int(h)}"


def resolve_generation_size(orientation=None, res_wh=None):
    """Resolve the /v1/images/generations `size` for a generation command.

    Returns (size, requested):
      - res_wh (an (w, h) int tuple, e.g. from parse_resolution) WINS over
        orientation: size = coerce_generation_size(w, h), requested = f"{w}x{h}".
      - else orientation ("square"/"landscape"/"portrait"): size =
        ORIENTATIONS[orientation], requested = None.
      - else (neither given): size = SQUARE, requested = None.

    Callers should post a coercion notice iff `requested and requested != size` --
    orientation presets and an already-valid --res both leave requested unset or
    equal to size, so they stay silent.
    """
    if res_wh is not None:
        w, h = res_wh
        requested = f"{w}x{h}"
        return coerce_generation_size(w, h), requested
    if orientation is not None:
        return ORIENTATIONS[orientation], None
    return SQUARE, None


def resolve_edit_size(orientation=None, res_wh=None, width=None, height=None):
    """Resolve the /v1/images/edits `size` for &remix.

    Empirically, gpt-image-2's /v1/images/edits endpoint honors an arbitrary WxH
    exactly like /v1/images/generations does (despite the docs only listing the three
    standard sizes + auto), so every path here coerces to an arbitrary valid size the
    same way the generation path does, rather than snapping to a standard size.

    Returns (size, requested):
      - res_wh WINS: size = coerce_generation_size(w, h), requested = f"{w}x{h}".
      - else orientation: size = ORIENTATIONS[orientation], requested = None.
      - else (neither given): match the first attachment's OWN dimensions as closely
        as a valid edit size allows -- size = coerce_generation_size(width, height),
        requested = None -- so the remix keeps the input's exact aspect/scale instead
        of being bucketed. Falls back to AUTO (requested None) when Discord didn't
        report usable width/height (None, non-numeric, or <= 0).
    """
    if res_wh is not None:
        w, h = res_wh
        requested = f"{w}x{h}"
        return coerce_generation_size(w, h), requested
    if orientation is not None:
        return ORIENTATIONS[orientation], None
    try:
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return AUTO, None
    if w <= 0 or h <= 0:
        return AUTO, None
    return coerce_generation_size(width, height), None
