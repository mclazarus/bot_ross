"""Pure, side-effect-free prompt-macro logic.

Kept separate from bot_ross.py (which ends in bot.run() at import) so it can be
imported and unit tested without Discord/OpenAI secrets, mirroring
magic_paint.py. All functions take their path/library explicitly rather than
reading a bot_ross module global. See test_macros.py.

A macro is a `;token` embedded anywhere in a &paint/&remix/etc. prompt (e.g.
"A ;rhe is trapped in a datacenter") that gets replaced, in place, with a short
noun-phrase snippet from the macro library before the prompt reaches magic
paint or the image API. By convention that snippet carries no leading article
and ends in a comma -- the writer supplies the article ("A ;rhe", "two ;cat"),
which is why the seed entries read "caucasian engineer ...," not "a caucasian
engineer ...,". There is no escape syntax: ';' is only special when
immediately followed by [A-Za-z0-9_-], so ';;rhe' still expands 'rhe' (the
leading ';' is untouched prose, the second ';' is what triggers the match),
and a bare ';' followed by a space or punctuation is left as ordinary prose.
Expansion is a single pass over the ORIGINAL prompt -- the substituted text is
never rescanned for further macros, so a macro whose text happens to contain
';something' cannot recurse or chain.

Unlike magic paint (a probabilistic background gag), macro expansion is
deterministic: it fires exactly when the requester types a ';token', and only
for tokens present in the library. An unresolved token doesn't block
generation -- it's swapped for a joke placeholder from the static
FALLBACK_EXPANSIONS list (a code constant, not user-editable, so it can't be
griefed into something in poor taste) and reported back to the requester so
they know a macro didn't resolve as expected.
"""

import random
import re

import json_library

# A ';' immediately followed by at least one id character is a macro
# reference. ';' followed by whitespace/punctuation (or nothing) is ordinary
# prose and is left alone. There is no escape syntax -- see the module
# docstring for the ';;' case.
MACRO_PATTERN = re.compile(r";([A-Za-z0-9_-]+)")

# Static joke substitutions used when a typed macro isn't found in the
# library. Deliberately a code constant (not stored in macros.json) so it
# can't be edited via &macro_add/&macro_update. Each follows the same
# convention as a library entry: an article-less noun phrase with a trailing
# comma, so it drops into "A ;token is ..." without doubling the article.
FALLBACK_EXPANSIONS = [
    "suspiciously confident golden retriever in a three-piece suit,",
    "inflatable tube man mid-existential-crisis,",
    "very small horse in enormous novelty sunglasses,",
    "medieval knight who is visibly late for something,",
    "sentient traffic cone with strong opinions,",
    "raccoon who has just been handed a briefcase,",
]


def normalize_macro_id(name):
    """Canonicalize a user-typed or stored macro id: strip a leading ';',
    strip surrounding whitespace, lowercase. Idempotent -- safe to call on
    already-normalized ids (e.g. values read straight from the library)."""
    name = name.strip()
    if name.startswith(";"):
        name = name[1:]
    return name.strip().lower()


def entry_id(entry):
    """Return an entry's normalized id, or None if the entry is malformed (not a
    dict, or its id is missing/blank/not a string). Lets the &macro_* commands
    match/skip hand-corrupted library rows without crashing -- the same fails-open
    tolerance expand_macros applies when building its lookup table."""
    if not isinstance(entry, dict):
        return None
    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    return normalize_macro_id(raw_id)


def is_valid_macro_id(name):
    """True if `name` (already normalized) is a legal macro id: 1-32
    characters of lowercase letters, digits, '_', or '-'."""
    return re.fullmatch(r"[a-z0-9_-]{1,32}", name) is not None


def load_macro_library(path):
    """Read the macro library fresh from `path`. Not cached in memory.
    Returns [] (fails open) if the file is missing or malformed."""
    return json_library.load_library(path, label="macro")


def save_macro_library(entries, path):
    """Write the macro library to `path`. Preserves unicode for readability."""
    json_library.save_library(entries, path)


def seed_macro_library(working_path, default_path):
    """Deploy the bundled default macro library onto the working path if it
    isn't there yet, so user-added macros survive image rebuilds/redeploys."""
    json_library.seed_library(working_path, default_path, label="macro")


def expand_macros(prompt, entries=None, path=None, fallbacks=FALLBACK_EXPANSIONS):
    """Replace every ';token' in `prompt` with its macro text (case-insensitive
    lookup), leaving everything else untouched.

    Supply `entries` directly (tests, or an already-loaded list), or `path` to
    load the library fresh from disk; if neither resolves to a non-empty list,
    every token is a miss (fails open -- the prompt is still usable, just
    without expansions).

    A found token's stored text replaces it in place. An entry whose text is
    empty/missing is treated the same as not found (a miss), matching
    magic_paint's apply_random_magic_entry no-op-on-empty-text behavior. An
    unresolved token is replaced with a random pick from `fallbacks` so the
    prompt stays coherent even when a macro was mistyped or removed. This is a
    single pass over the ORIGINAL prompt -- replacement text is never
    rescanned, so a macro/fallback containing ';something' cannot chain.

    Returns (new_prompt, hits, misses): `hits` and `misses` are lists of
    normalized token ids in the order encountered, with duplicates preserved
    (so counters built on their lengths are honest about how many
    substitutions actually happened).
    """
    if entries is None:
        entries = load_macro_library(path) if path is not None else []

    # Build the id -> text lookup defensively: a single hand-corrupted row in
    # data/macros.json (a non-dict list item, or an id/text that isn't a string)
    # must not take down expansion for every other, valid macro -- that would
    # violate the fails-open promise. Skip anything that isn't well-formed.
    lookup = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        text = entry.get("text", "")
        if not isinstance(raw_id, str) or not raw_id or not isinstance(text, str):
            continue
        lookup[normalize_macro_id(raw_id)] = text

    hits = []
    misses = []

    def _replace(match):
        token = normalize_macro_id(match.group(1))
        text = lookup.get(token)
        if text:
            hits.append(token)
            return text
        misses.append(token)
        return random.choice(fallbacks)

    new_prompt = MACRO_PATTERN.sub(_replace, prompt)
    return new_prompt, hits, misses
