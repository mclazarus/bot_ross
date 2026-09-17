"""Dependency-pin smoke test for the Python 3.14 / current-floors move.

Before this file existed, nothing in the suite ever imported `discord` or
`aiohttp`, so a dependency pin that failed at import time (as `discord.py`
2.3.2 does on Python 3.13+, raising `ModuleNotFoundError: No module named
'audioop'` from `discord/player.py`) passed every test and only crash-looped
in production, behind `run.sh`'s `--restart=unless-stopped`. This module
imports `discord` and `aiohttp` at their real installed versions, proves
`zoneinfo` actually carries DST rules (not just that it constructs), and
parses `requirements.txt` to assert every pin is satisfied by what pip
actually installed -- ASSERT, never skip, because a dev environment that
cannot satisfy requirements.txt IS the bug this file exists to catch.

Run from the repo root:  python -m unittest test_runtime_deps -v
"""

import importlib
import importlib.metadata
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_FILE = os.path.join(REPO_ROOT, "requirements.txt")

# `~=X.Y[.Z...]` is the only operator requirements.txt uses today. Matched
# strictly on purpose (see _parse_requirements) so a future pin written with
# an operator this parser doesn't understand fails loudly instead of being
# silently skipped and therefore silently unverified.
_REQUIREMENT_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*~=\s*([0-9][0-9.]*[0-9]|[0-9])\s*$"
)

# Leading digit run per dot-separated component, e.g. "1a1" -> "1", "post1" -> no match.
_LEADING_DIGITS_RE = re.compile(r"^(\d+)")


def _version_tuple(text):
    """Best-effort numeric version tuple, stopping at the first non-numeric component.

    "3.14.3" -> (3, 14, 3); "2.7.1a1" -> (2, 7, 1) (the "a1" suffix on the last
    component is dropped, not parsed); "2.7.post1" -> (2, 7) (component "post1"
    has no leading digit, so parsing stops there, excluding it). "abc" -> ().

    Deliberately NOT a full PEP 440 comparator: pre/post/dev segments are
    ignored, so "2.7.1a1" compares equal to "2.7.1". That's acceptable because
    pip never installs a pre-release from a `~=` pin without `--pre`, and a
    hand-installed pre-release slipping past this floor check is a smaller
    risk than adding the `packaging` library as a new test dependency, which
    the project style forbids (stdlib unittest only).
    """
    parts = []
    for component in text.split("."):
        match = _LEADING_DIGITS_RE.match(component)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts)


def _parse_requirements(path):
    """Parse a `~=`-only requirements.txt into [(distribution_name, pin_string), ...].

    Raises AssertionError naming the offending line verbatim if any non-blank,
    non-comment line doesn't match the `~=` shape -- see _REQUIREMENT_RE's
    docstring note above for why this is a raise, not a skip. Missing/unreadable
    file: the OSError propagates, failing the test loudly either way.
    """
    result = []
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            match = _REQUIREMENT_RE.match(line)
            if not match:
                raise AssertionError(
                    f"requirements.txt line does not match the supported `~=` "
                    f"format and was not verified: {raw_line!r}"
                )
            result.append((match.group(1), match.group(2)))
    return result


def _compatible_release_satisfied(installed, pin):
    """PEP 440 `~=` ("compatible release") check on top of _version_tuple.

    `~=X.Y` means: at least X.Y, and the series identified by dropping the
    last pin component must match exactly (X.* for `~=X.Y`, X.Y.* for
    `~=X.Y.Z`). E.g. `~=2.7.1` is satisfied by 2.7.1 and 2.7.5, but not 2.8.0.
    """
    p = _version_tuple(pin)
    assert len(p) >= 2, f"~=X is not valid PEP 440 (need at least two components): {pin!r}"
    i = _version_tuple(installed)
    n = len(p)
    # Tuple comparison zero-pads the shorter side for equality purposes here
    # only via slicing to matching lengths, matching PEP 440 release-segment
    # semantics: (2, 7) == (2, 7)[:1] compared against (2, 7, 1)[:1] -> (2,) == (2,).
    return i >= p and i[: n - 1] == p[: n - 1]


class HelperContractTest(unittest.TestCase):
    """Exercises the module's own parsing helpers before trusting them elsewhere."""

    def test_version_tuple_drops_prerelease_suffix(self):
        self.assertEqual(_version_tuple("2.7.1a1"), (2, 7, 1))

    def test_version_tuple_stops_at_non_numeric_component(self):
        self.assertEqual(_version_tuple("2.7.post1"), (2, 7))

    def test_version_tuple_handles_no_leading_digit(self):
        self.assertEqual(_version_tuple("abc"), ())

    def test_compatible_release_rejects_next_minor_series(self):
        # The easiest part of ~= to get wrong: 2.8.0 satisfies ">= 2.7.1" but
        # NOT the minor-series constraint "~=2.7.1" pins.
        self.assertFalse(_compatible_release_satisfied("2.8.0", "2.7.1"))

    def test_compatible_release_accepts_patch_bump_within_series(self):
        self.assertTrue(_compatible_release_satisfied("2.7.5", "2.7.1"))

    def test_parse_requirements_raises_on_unsupported_operator(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "requirements.txt")
            with open(bad_path, "w", encoding="utf-8") as f:
                f.write("foo>=1.0\n")
            with self.assertRaises(AssertionError):
                _parse_requirements(bad_path)


class DiscordRuntimeTest(unittest.TestCase):
    def test_discord_imports_and_meets_floor(self):
        # discord.py 2.3.x-2.4.x raise ModuleNotFoundError: audioop on Python
        # 3.13+ (audioop was removed by PEP 594); 2.5+ depends on audioop-lts.
        import discord
        from discord.ext import commands  # noqa: F401  -- import succeeding is the assertion

        self.assertGreaterEqual(
            discord.version_info[:2],
            (2, 7),
            f"discord.py {discord.__version__} is below the 2.7 floor; 2.3.x-2.4.x "
            "raise ModuleNotFoundError: audioop on Python 3.13+",
        )

    def test_discord_player_imports_by_name(self):
        # discord/player.py is the module whose unconditional `import audioop`
        # was the entire 3.13+ blocker. `import discord` already reaches it
        # transitively via discord/__init__.py, but this test names it
        # directly so a future discord.py that makes the import lazy can't
        # silently un-cover the failure mode this file exists to catch.
        importlib.import_module("discord.player")


class AiohttpRuntimeTest(unittest.TestCase):
    def test_aiohttp_imports_and_meets_floor(self):
        # aiohttp 3.9.x ships no cp313/cp314 wheels and will not compile
        # there. The floor is (3, 13), not (3, 14): the wheel cliff is at
        # 3.13, and an over-tight test floor is how routine pin bumps start
        # requiring test edits for no reason.
        import aiohttp

        self.assertGreaterEqual(
            _version_tuple(aiohttp.__version__)[:2],
            (3, 13),
            f"aiohttp {aiohttp.__version__} is below the 3.13 floor; 3.9.x has no "
            "cp313/cp314 wheels",
        )


class TimezoneDataTest(unittest.TestCase):
    def test_tzdata_is_installed(self):
        # On this macOS dev box the OS itself supplies zoneinfo data, so
        # ZoneInfo(...) constructing successfully proves nothing about the
        # wheel the (Linux) container will actually rely on -- only checking
        # the installed distribution does.
        version = importlib.metadata.version("tzdata")
        self.assertTrue(version)

    def test_new_york_has_real_dst_rules(self):
        # Proves timezone DATA with real DST transitions is present, which is
        # what daily_schedule.slot_instant's through-UTC normalization
        # depends on. A ZoneInfo backed by no data raises outright; one backed
        # by stale or truncated data is the silent, scheduler-shifting
        # failure this test is meant to catch. Exact offsets (not just
        # assertNotEqual) because EST/EDT have been fixed since 2007, and
        # exact values also catch a corrupted rules file that happens to
        # produce two different wrong offsets.
        zone = ZoneInfo("America/New_York")
        winter = datetime(2026, 1, 15, 12, tzinfo=zone)
        summer = datetime(2026, 7, 15, 12, tzinfo=zone)
        self.assertEqual(
            winter.utcoffset(),
            timedelta(hours=-5),
            "ZoneInfo('America/New_York') shows no EST/EDT offset difference "
            "between January and July; timezone DATA is missing or stale, which "
            "silently breaks daily_schedule.slot_instant",
        )
        self.assertEqual(
            summer.utcoffset(),
            timedelta(hours=-4),
            "ZoneInfo('America/New_York') shows no EST/EDT offset difference "
            "between January and July; timezone DATA is missing or stale, which "
            "silently breaks daily_schedule.slot_instant",
        )


class RequirementsPinsTest(unittest.TestCase):
    def test_every_requirement_pin_is_satisfied(self):
        pins = _parse_requirements(REQUIREMENTS_FILE)
        self.assertTrue(pins, "requirements.txt yielded no parseable pins")
        for name, pin in pins:
            try:
                installed = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                self.fail(
                    f"{name} is pinned in requirements.txt but not installed in "
                    "this environment"
                )
                continue
            self.assertTrue(
                _compatible_release_satisfied(installed, pin),
                f"requirements.txt pins {name}~={pin} but {installed} is installed; "
                "a dev env that cannot satisfy requirements.txt IS the bug -- "
                "reinstall, do not skip",
            )

    def test_audioop_lts_is_not_pinned_directly(self):
        # discord.py owns audioop-lts transitively. Pinning it here would keep
        # it installed even after a future discord.py drops the dependency,
        # and encodes a transitive dep as if it were ours to manage.
        with open(REQUIREMENTS_FILE, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                name = match.group(1) if match else line
                normalized = name.lower().replace("_", "-").replace(".", "-")
                self.assertFalse(
                    normalized == "audioop-lts" or normalized.startswith("audioop"),
                    "audioop-lts must not be pinned directly; discord.py owns it "
                    "(see CLAUDE.md Key Dependencies)",
                )


if __name__ == "__main__":
    unittest.main()
