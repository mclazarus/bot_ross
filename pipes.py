"""Pure logic for `|` pipe chaining across the image commands (&paint/&hpaint/&mpaint/
&lpaint/&dpaint/&xpaint/&remix/&release_image).

A pipe chain is one command invocation split on literal `|` into up to five segments:
the first segment behaves exactly like the command does today, and every later
segment is treated as an image-edit of the previous segment's output. This module
owns only the pure, no-side-effect pieces -- splitting the raw text and formatting the
handful of fixed status strings -- so they're unit-testable despite bot_ross.py being
unimportable under test (it ends in bot.run() at module scope). The actual chain
runner (_piped/_run_chain/_pipe_edit_once) stays in bot_ross.py, thin and untested,
since it has to talk to Discord/do_the_art.

No escape syntax for a literal `|` -- consistent with macros.py's documented stance on
`;`. A prompt that genuinely needs a pipe character in it can't have one; that's a
deliberate simplicity trade-off, not an oversight.

Naming note: this local module deliberately shadows the deprecated stdlib `pipes`
module (removed outright in Python 3.13). That's harmless here -- nothing in this
codebase or its dependencies imports the stdlib one (only release_image, magic_paint,
macros, image_size, and json_library are local imports; discord.py/aiohttp/coloredlogs
don't touch stdlib `pipes` either) -- but it's worth a comment so nobody "fixes" the
Python 3.10 shadowing warning by renaming this file.
"""

from typing import NamedTuple

# 4 pipes = 5 segments is the cap; the 5th pipe (6 raw parts) gets refused outright.
# Pinned as a constant (not just a literal in split_pipeline) because it's also the
# number the README/&help text and TOO_MANY_MESSAGE's threshold need to stay honest.
MAX_SEGMENTS = 5

# Exact, load-bearing: this must be the ONLY message sent for a too-many-pipes
# invocation -- no dropped-segment note, no quote, nothing else.
TOO_MANY_MESSAGE = "Okay, simmer down, buddy."


class ArtResult(NamedTuple):
    """What do_the_art returns on success. A NamedTuple is a 4-element tuple and is
    therefore ALWAYS truthy -- even when every field is falsy (None/b""/None/0.0) --
    which is load-bearing: &meme's `if await do_the_art(...)` (bot_ross.py) and every
    other call site that only checks truthiness must keep working unchanged. Defined
    here (not in bot_ross.py) so that truthiness guarantee itself is unit-testable
    (see ArtResultTest) despite bot_ross.py being unimportable under test.

    Field order is part of the contract -- callers are allowed to positionally unpack
    it (`message, image_bytes, size, elapsed = result`), so don't reorder these.
    """
    message: object      # the discord.Message carrying the posted image (None in tests)
    image_bytes: bytes   # decoded PNG bytes of the generated/edited image
    size: object          # the size string this call was made with, or None (config default)
    elapsed: float         # generation wall time in seconds


def split_pipeline(raw, max_segments=MAX_SEGMENTS):
    """Split a raw command prompt on literal `|` into pipeline segments.

    Returns (segments, dropped, error):
      - segments: list[str] of the surviving, end-stripped segment texts.
      - dropped: int, how many empty segments (after stripping) were removed.
      - error: None, "too_many", or "empty".

    No `|` in `raw` at all -> ([raw], 0, None), with `raw` returned VERBATIM --
    unstripped, whitespace and all. This is the single most important compatibility
    guarantee in the module: every existing non-piped command path (the overwhelming
    majority of invocations) must see byte-for-byte the same text it saw before this
    feature existed, so `_prep_generation_size`/`expand_prompt_macros` etc. never
    notice a pipe chain that isn't there.

    Otherwise, raw.split("|") gives the raw parts. The length of THAT list -- BEFORE
    any empty segments are dropped -- is what's checked against max_segments. This
    ordering is deliberate and is the module's single most important behavior:
    "a | | | | | b" has five pipes (six raw parts, four of them blank) and must be
    refused with "too_many", not quietly reduced to two real segments ["a", "b"]. A
    user who mashes the pipe key shouldn't get a chain shorter than they typed by
    accident -- they should get told to simmer down.

    Below the cap, each raw part is stripped at both ends only (internal whitespace,
    including newlines within a segment, is preserved -- only stripping the part's
    own leading/trailing whitespace, the same one-line-of-prose granularity a plain
    &paint prompt gets). Parts that strip to "" are dropped and counted in `dropped`.
    If nothing survives, error is "empty" (still reporting the honest dropped count);
    otherwise error is None.
    """
    if "|" not in raw:
        return [raw], 0, None

    parts = raw.split("|")
    if len(parts) > max_segments:
        return [], 0, "too_many"

    stripped = [p.strip() for p in parts]
    segments = [p for p in stripped if p]
    dropped = len(stripped) - len(segments)

    if not segments:
        return [], dropped, "empty"
    return segments, dropped, None


def chain_stopped_message(step, total):
    """Exact abort notice for a chain that failed at 1-based `step` of `total`
    (the post-drop segment count, so the numbering a user sees always matches what
    they'd count in their own prompt). No validation here -- callers guarantee
    1 <= step <= total; this is pure string formatting."""
    return f"Chain stopped at step {step} of {total}."


def chain_complete_message(total):
    """Exact success notice for a completed chain of `total` steps. Only ever called
    with total >= 2 (a single-segment run is just today's plain command and posts
    nothing new), so there's deliberately no singular-friendly wording to worry about."""
    return f"Chain complete: {total} steps."


def dropped_note(dropped):
    """Exact notice for empty pipe segments silently removed before running the
    chain (e.g. a trailing `|`, or `a | | b`). Only ever called with dropped >= 1."""
    if dropped == 1:
        return "(skipping 1 empty pipe segment)"
    return f"(skipping {dropped} empty pipe segments)"
