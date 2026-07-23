"""Pure aspect-ratio-to-edit-size selection for &remix.

The OpenAI /v1/images/edits endpoint only accepts four `size` values: 1024x1024,
1536x1024, 1024x1536, or "auto". &remix wants the output to roughly match the
orientation of the first input image rather than always forcing a square, so this
module picks the allowed size whose aspect ratio is closest to the input's.

Because resizing to a *different* aspect ratio necessarily stretches the image (a
multiplicative distortion, not an additive one), "closest" is measured in log-ratio
space: the boundary between two neighboring target ratios is their GEOMETRIC mean,
not their arithmetic mean. A ratio sitting exactly at a boundary is equidistant (in
log space) from both neighbors; ties are broken toward the non-square neighbor (see
edit_size_for_dimensions).

No Discord/OpenAI/bot side effects -- bot_ross.py can't be imported under test because
module load ends in bot.run(), so this logic lives here for test_image_size.py to
exercise directly.
"""

import math

# The only four sizes the /v1/images/edits endpoint accepts.
SQUARE = "1024x1024"
LANDSCAPE = "1536x1024"
PORTRAIT = "1024x1536"
AUTO = "auto"

# Aspect ratios (width / height) of the three fixed sizes.
SQUARE_RATIO = 1.0
LANDSCAPE_RATIO = 1536 / 1024   # 1.5
PORTRAIT_RATIO = 1024 / 1536    # 0.6666...

# Bucket boundaries are the geometric mean of neighboring target ratios -- the
# log-space midpoint, since aspect distortion is multiplicative. The two thresholds
# are reciprocals of each other (sqrt(1.5) * sqrt(2/3) == 1 exactly in the reals,
# to ~1e-16 in float), which is what makes the three buckets symmetric under
# transposition: (w, h) -> (h, w). That symmetry is exact for every realistic image
# size; only a ratio landing within a float ULP of a boundary can transpose
# asymmetrically, and no integer pixel dimensions in range do.
LANDSCAPE_THRESHOLD = math.sqrt(SQUARE_RATIO * LANDSCAPE_RATIO)   # sqrt(1.5) ~= 1.224745
PORTRAIT_THRESHOLD = math.sqrt(SQUARE_RATIO * PORTRAIT_RATIO)     # sqrt(2/3) ~= 0.816497


def edit_size_for_dimensions(width, height):
    """Pick the allowed /v1/images/edits `size` for an input image of `width` x `height`.

    - ratio >= LANDSCAPE_THRESHOLD  -> LANDSCAPE (the boundary value itself is landscape)
    - ratio <= PORTRAIT_THRESHOLD   -> PORTRAIT  (the boundary value itself is portrait)
    - otherwise                     -> SQUARE
    These two boundary conditions can never both fire (LANDSCAPE_THRESHOLD is strictly
    greater than PORTRAIT_THRESHOLD) and leave no gap: every ratio falls into exactly
    one bucket.

    Returns AUTO when `width`/`height` are missing (None), non-numeric, or <= 0 --
    e.g. discord.Attachment.width/.height are Optional[int] and are None for
    attachments Discord didn't recognize as images, or couldn't probe.
    """
    try:
        w = float(width)
        h = float(height)
    except (TypeError, ValueError):
        return AUTO
    if w <= 0 or h <= 0:
        return AUTO

    ratio = w / h
    if ratio >= LANDSCAPE_THRESHOLD:
        return LANDSCAPE
    if ratio <= PORTRAIT_THRESHOLD:
        return PORTRAIT
    return SQUARE


def describe_edit_size(size):
    """Human-readable label for a size constant, for logging only (never shown in Discord)."""
    return {
        SQUARE: "square",
        LANDSCAPE: "landscape",
        PORTRAIT: "portrait",
        AUTO: "auto",
    }.get(size, "unknown")
