"""Generic JSON list-of-entry-dicts library I/O.

Shared by magic_paint.py (background-gag mixins) and macros.py (;token prompt
substitutions) -- both features store their library the same way: a JSON array
of {"id", "text", ...} dicts, with a read-only seed shipped in the image and a
mutable working copy seeded onto the data/ volume at startup so user edits
survive image rebuilds/redeploys. Lifted out of magic_paint.py (the original,
magic-specific load/save/seed) so a second feature doesn't have to duplicate
it. No Discord/OpenAI/bot side effects -- every function takes its path(s)
explicitly.
"""

import json
import logging
import os

logger = logging.getLogger("bot_ross.json_library")


def load_library(path, label):
    """Read a JSON list-of-entry-dicts library fresh from `path`. Not cached in
    memory. Returns [] (fails open) if the file is missing, malformed, not a
    list, or empty. `label` (e.g. "magic", "macro") only identifies the caller
    in log messages."""
    try:
        with open(path, 'r') as f:
            entries = json.load(f)
        if not isinstance(entries, list) or not entries:
            logger.error(f"{label} library {path} is empty or malformed.")
            return []
        return entries
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load {label} library from {path}: {e}")
        return []


def save_library(entries, path):
    """Write a list-of-entry-dicts library to `path`. Preserves unicode (e.g.
    ♥️, —) for readability instead of escaping it."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def seed_library(working_path, default_path, label):
    """Deploy the bundled default library onto `working_path` if it isn't
    there yet.

    Runs at startup so user-added entries survive image rebuilds/redeploys --
    only the seed at `default_path` ships in the image; `working_path` lives on
    the persistent data/ volume. `label` (e.g. "magic", "macro") only
    identifies the caller in the log message."""
    if os.path.exists(working_path):
        return
    try:
        with open(default_path, 'r', encoding='utf-8') as src:
            entries = json.load(src)
        save_library(entries, working_path)
        logger.info(f"Seeded {working_path} from {default_path} ({len(entries)} {label} entries).")
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to seed {label} library from {default_path}: {e}")
