"""Deterministic release-avatar prompt generation for the &release_image command.

This module is intentionally pure: it has no Discord, OpenAI, or bot side effects,
so it imports cleanly and can be unit tested in isolation (see test_release_image.py).

Given a git hash (or any text), it deterministically builds a mad-libs-style image
*prompt* -- the same input always yields the same prompt. The generated image is not
deterministic (OpenAI generation isn't), but the prompt is, which is what makes a
release's avatar stable and reproducible.

The word lists + templates live in release_algorithms.json as a versioned, read-only
artifact shipped in the image. Because the prompt is a pure function of
(input, algorithm), ANY change to a template or word list changes the space of
possible outputs, so such a change must ship as a NEW version while old versions are
retained. Selection folds the version into the hash key so the same input yields
different picks per version. Unlike the magic library, this file is static content and
is never copied onto the data/ volume.
"""

import hashlib
import json
import os
import re

# Static, read-only artifact shipped in the image. Resolve relative to this file (not
# the cwd) so it loads whether run from /app in Docker or from the repo root in tests.
RELEASE_ALGORITHMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "release_algorithms.json")


def load_release_algorithms(path=RELEASE_ALGORITHMS_FILE):
    """Read and return the versioned algorithm artifact as a dict keyed by version string."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Loaded once at import. Safe: this is pure file I/O with no bot side effects.
RELEASE_ALGORITHMS = load_release_algorithms()


def latest_version(algorithms=None):
    """Return the highest numeric version key (as a string)."""
    algorithms = RELEASE_ALGORITHMS if algorithms is None else algorithms
    return max(algorithms, key=lambda v: int(v))


def get_release_algorithm(version=None, algorithms=None):
    """Return (version_string, algorithm_dict) for the requested version.

    Defaults to the latest version. Raises KeyError for an unknown version.
    """
    algorithms = RELEASE_ALGORITHMS if algorithms is None else algorithms
    version = latest_version(algorithms) if version is None else str(version)
    if version not in algorithms:
        raise KeyError(version)
    return version, algorithms[version]


def release_seed(source):
    """The short public seed shown to users: first 8 hex chars of sha256(normalized input)."""
    return hashlib.sha256(source.strip().encode("utf-8")).hexdigest()[:8]


def _release_index(source, version, category, n):
    """Deterministically map (input, version, category) to an index in [0, n).

    Hashing each category with its own name keeps categories independent -- adding or
    reordering categories, or editing one word list, won't shift another category's
    pick. Using sha256 (not Python's random) keeps results stable across interpreter
    versions and platforms.
    """
    key = f"{source.strip()}|{version}|{category}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % n


def parse_release_args(args):
    """Split raw command args into (source, version, georgify).

    Recognized flags (case-insensitive), stripped from the input:
      --george / --costanza      -> georgify = True
      --version N / --version=N  -> version = N
      --vN                       -> version = N
    Every remaining token is joined back together as the source string, so the input
    to hash may itself contain spaces.
    """
    georgify = False
    version = None
    words = []
    tokens = args.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low in ("--george", "--costanza"):
            georgify = True
        elif low == "--version" and i + 1 < len(tokens):
            version = tokens[i + 1]
            i += 1
        elif low.startswith("--version="):
            version = tok.split("=", 1)[1]
        elif re.fullmatch(r"--v\d+", low):
            version = low[3:]
        else:
            words.append(tok)
        i += 1
    return " ".join(words), version, georgify


def build_release_prompt(source, version=None, georgify=False, algorithms=None):
    """Build the deterministic release prompt for the given input.

    Returns (prompt, seed, version_used). Pure function -- this is the unit of testing.
    Raises KeyError if `version` is not a known algorithm version.
    """
    version_used, algo = get_release_algorithm(version, algorithms)
    word_lists = algo["word_lists"]
    picks = {
        category: word_lists[category][_release_index(source, version_used, category, len(word_lists[category]))]
        for category in algo["categories"]
    }
    if georgify:
        picks["subject"] = algo["georgify_template"].format(subject=picks["subject"])
    prompt = algo["template"].format(**picks)
    return prompt, release_seed(source), version_used
