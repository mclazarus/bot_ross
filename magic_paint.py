"""Pure, side-effect-free magic-paint logic.

Kept separate from bot_ross.py (which ends in bot.run() at import) so it can be
imported and unit tested without Discord/OpenAI secrets. All functions here take
their paths/rate/library explicitly rather than reading bot_ross module globals,
so the magic-mixin rate calculation is testable in isolation. See test_magic_paint.py.
"""

import os
import json
import random
import re
import logging

logger = logging.getLogger("bot_ross.magic_paint")


def parse_magic_rate(s):
    """Parse a user-supplied magic rate into a probability in [0.0, 1.0].

    Trailing '%' is interpreted exactly as a percent (10% -> 0.10, .1% -> 0.001).
    Without '%': a value >= 1 is a percent number (10 -> 0.10, 1 -> 0.01),
    a value < 1 is a raw fraction (.1 -> 0.10, .001 -> 0.001).
    Raises ValueError on unparseable input or a result outside [0.0, 1.0]."""
    s = s.strip()
    if s.endswith('%'):
        rate = float(s[:-1]) / 100.0
    else:
        num = float(s)
        rate = num / 100.0 if num >= 1 else num
    if not (0.0 <= rate <= 1.0):
        raise ValueError(f"rate {rate} out of range [0.0, 1.0]")
    return rate


def format_magic_rate(rate):
    """Render a probability as a percent string: 0.05 -> '5%', 0.001 -> '0.1%', 0.10 -> '10%'."""
    return f"{rate * 100:g}%"


def load_magic_library(path):
    """Read the magic-prompt library fresh from `path`. Not cached in memory.
    Returns [] (fails open) if the file is missing or malformed."""
    try:
        with open(path, 'r') as f:
            entries = json.load(f)
        if not isinstance(entries, list) or not entries:
            logger.error(f"{path} is empty or malformed.")
            return []
        return entries
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load magic prompt library: {e}")
        return []


def save_magic_library(entries, path):
    """Write the magic-prompt library to `path`. Preserves unicode (♥️, —) for readability."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def seed_magic_library(working_path, default_path):
    """Deploy the bundled default library onto the working path if it isn't there yet.
    Runs at startup so user-added mixins survive image rebuilds/redeploys; only the
    seed ships in the image."""
    if os.path.exists(working_path):
        return
    try:
        with open(default_path, 'r', encoding='utf-8') as src:
            entries = json.load(src)
        save_magic_library(entries, working_path)
        logger.info(f"Seeded {working_path} from {default_path} ({len(entries)} entries).")
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to seed magic library from {default_path}: {e}")


def slugify_magic_id(text, existing_ids):
    """Build a stable, human-referenceable slug from the first few words of the text,
    appending -2, -3, ... until it's unique against existing_ids."""
    words = re.findall(r'[a-z0-9]+', text.lower())[:4]
    base = '-'.join(words) or "magic"
    slug = base
    n = 2
    while slug in existing_ids:
        slug = f"{base}-{n}"
        n += 1
    return slug


def apply_random_magic_entry(prompt, entries=None, path=None):
    """Pick one entry at random and append its text to the prompt.

    Supply `entries` directly (tests, or an already-loaded list), or `path` to load
    the library fresh from disk. Fails open (returns the unchanged prompt) if the
    library is missing/empty."""
    if entries is None:
        entries = load_magic_library(path) if path is not None else []
    if not entries:
        return prompt
    text = random.choice(entries).get("text", "")
    return f"{prompt} {text}" if text else prompt


def maybe_apply_magic_paint(prompt, rate, entries=None, path=None):
    """Roll against `rate`; on success append a random magic entry.

    The library is only consulted when the roll succeeds (pass `path` for a fresh
    disk read, or `entries` for an in-memory list). Returns (new_prompt, triggered)."""
    if random.random() < rate:
        return apply_random_magic_entry(prompt, entries=entries, path=path), True
    return prompt, False
