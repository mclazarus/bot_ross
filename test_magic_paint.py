"""Unit tests for the magic-paint mixin logic.

These exercise magic_paint.py in isolation (no Discord/OpenAI), asserting the core
promise: the configured rate is honored (roughly `rate` of N generations get a
mixin), rate parsing/formatting round-trips, and mixin application is well-formed.

Run from the repo root:  python -m unittest test_magic_paint -v
"""

import json
import os
import random
import tempfile
import unittest

import magic_paint
from magic_paint import (
    apply_random_magic_entry,
    format_magic_rate,
    load_magic_library,
    maybe_apply_magic_paint,
    parse_magic_rate,
    save_magic_library,
    slugify_magic_id,
)

# A stand-in library so the statistical tests never touch disk.
STUB_ENTRIES = [{"id": "stub", "text": "MIXIN"}]

SEED_LIBRARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "magic_prompts.json")
COSTANZA_TEXT = "...but it's George Costanza."


class RateHonoredTest(unittest.TestCase):
    """The headline promise: at rate R over N generations, ~R*N get a mixin."""

    def _count_triggered(self, rate, n, seed=1234):
        random.seed(seed)
        triggered = 0
        for _ in range(n):
            prompt, hit = maybe_apply_magic_paint("a happy little tree", rate, entries=STUB_ENTRIES)
            if hit:
                # When it fires, the mixin is actually appended.
                self.assertEqual(prompt, "a happy little tree MIXIN")
                triggered += 1
            else:
                self.assertEqual(prompt, "a happy little tree")
        return triggered

    def test_25_percent_over_100_runs_is_about_25(self):
        # The concrete case the maintainer asked for: 25% across 100 generations
        # should land near 25. Seeded so the count is deterministic; the band is wide
        # enough to absorb normal sampling variance (std ~4.3 at p=0.25, n=100).
        count = self._count_triggered(0.25, 100)
        self.assertGreaterEqual(count, 15)
        self.assertLessEqual(count, 35)

    def test_25_percent_large_sample_converges(self):
        # With a big sample the observed proportion should sit tight against 0.25,
        # which is the real guarantee that the rate is being followed.
        n = 20000
        count = self._count_triggered(0.25, n)
        self.assertAlmostEqual(count / n, 0.25, delta=0.02)

    def test_rate_zero_never_triggers(self):
        self.assertEqual(self._count_triggered(0.0, 5000), 0)

    def test_rate_one_always_triggers(self):
        self.assertEqual(self._count_triggered(1.0, 5000), 5000)

    def test_other_rate_converges(self):
        n = 20000
        self.assertAlmostEqual(self._count_triggered(0.10, n) / n, 0.10, delta=0.02)


class ApplyMixinTest(unittest.TestCase):
    def test_injected_entries_are_appended(self):
        result = apply_random_magic_entry("a barn", entries=[{"id": "x", "text": "with a cat"}])
        self.assertEqual(result, "a barn with a cat")

    def test_empty_library_fails_open(self):
        self.assertEqual(apply_random_magic_entry("a barn", entries=[]), "a barn")

    def test_missing_path_and_no_entries_fails_open(self):
        self.assertEqual(apply_random_magic_entry("a barn"), "a barn")

    def test_entry_without_text_leaves_prompt_unchanged(self):
        self.assertEqual(apply_random_magic_entry("a barn", entries=[{"id": "x"}]), "a barn")

    def test_maybe_returns_triggered_flag(self):
        random.seed(0)
        _, hit = maybe_apply_magic_paint("a barn", 1.0, entries=STUB_ENTRIES)
        self.assertTrue(hit)
        _, hit = maybe_apply_magic_paint("a barn", 0.0, entries=STUB_ENTRIES)
        self.assertFalse(hit)

    def test_only_reads_library_when_roll_succeeds(self):
        # path points at a nonexistent file; at rate 0 it must never be consulted.
        random.seed(0)
        for _ in range(1000):
            prompt, hit = maybe_apply_magic_paint("a barn", 0.0, path="/no/such/library.json")
            self.assertFalse(hit)
            self.assertEqual(prompt, "a barn")


class ParseRateTest(unittest.TestCase):
    def test_percent_suffix_is_exact(self):
        self.assertAlmostEqual(parse_magic_rate("10%"), 0.10)
        self.assertAlmostEqual(parse_magic_rate(".1%"), 0.001)
        self.assertAlmostEqual(parse_magic_rate("100%"), 1.0)
        self.assertAlmostEqual(parse_magic_rate("0%"), 0.0)

    def test_bare_number_ge_one_is_percent(self):
        self.assertAlmostEqual(parse_magic_rate("10"), 0.10)
        self.assertAlmostEqual(parse_magic_rate("1"), 0.01)
        self.assertAlmostEqual(parse_magic_rate("100"), 1.0)

    def test_bare_fraction_lt_one_is_raw(self):
        self.assertAlmostEqual(parse_magic_rate(".1"), 0.10)
        self.assertAlmostEqual(parse_magic_rate(".001"), 0.001)
        self.assertAlmostEqual(parse_magic_rate("0"), 0.0)

    def test_whitespace_is_tolerated(self):
        self.assertAlmostEqual(parse_magic_rate("  25%  "), 0.25)

    def test_out_of_range_raises(self):
        for bad in ("101%", "200", "-1", "-5%"):
            with self.assertRaises(ValueError):
                parse_magic_rate(bad)

    def test_unparseable_raises(self):
        for bad in ("banana", "", "  ", "%"):
            with self.assertRaises(ValueError):
                parse_magic_rate(bad)

    def test_parse_format_round_trip(self):
        self.assertEqual(format_magic_rate(parse_magic_rate("25%")), "25%")
        self.assertEqual(format_magic_rate(parse_magic_rate("5")), "5%")
        self.assertEqual(format_magic_rate(parse_magic_rate(".1%")), "0.1%")


class FormatRateTest(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(format_magic_rate(0.05), "5%")
        self.assertEqual(format_magic_rate(0.001), "0.1%")
        self.assertEqual(format_magic_rate(0.10), "10%")
        self.assertEqual(format_magic_rate(1.0), "100%")
        self.assertEqual(format_magic_rate(0.0), "0%")


class SlugifyTest(unittest.TestCase):
    def test_first_four_words(self):
        self.assertEqual(slugify_magic_id("In the background a cat", set()), "in-the-background-a")

    def test_collision_suffixing(self):
        existing = {"in-the-background-a"}
        self.assertEqual(slugify_magic_id("In the background a cat", existing), "in-the-background-a-2")

    def test_empty_text_falls_back(self):
        self.assertEqual(slugify_magic_id("!!!", set()), "magic")


class LibraryIoTest(unittest.TestCase):
    def test_save_load_round_trip_preserves_unicode(self):
        entries = [{"id": "heart", "text": "I ♥️ Philosophy — really"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lib.json")
            save_magic_library(entries, path)
            self.assertEqual(load_magic_library(path), entries)
            # ensure_ascii=False keeps the raw unicode on disk (not \uXXXX escapes).
            with open(path, encoding="utf-8") as f:
                self.assertIn("♥️", f.read())

    def test_load_missing_file_fails_open(self):
        self.assertEqual(load_magic_library("/no/such/file.json"), [])

    def test_load_malformed_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lib.json")
            with open(path, "w") as f:
                f.write("{ not valid json")
            self.assertEqual(load_magic_library(path), [])


class SeedLibraryDataTest(unittest.TestCase):
    """The shipped seed library must be structurally sound."""

    def setUp(self):
        self.entries = load_magic_library(SEED_LIBRARY_FILE)

    def test_non_empty_list(self):
        self.assertIsInstance(self.entries, list)
        self.assertTrue(self.entries)

    def test_every_entry_has_id_and_text(self):
        for e in self.entries:
            self.assertTrue(e.get("id"), f"entry missing id: {e}")
            self.assertTrue(e.get("text"), f"entry missing text: {e}")

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.entries]
        self.assertEqual(len(ids), len(set(ids)))

    def test_exactly_one_costanza(self):
        costanza = [e for e in self.entries if e.get("text") == COSTANZA_TEXT]
        self.assertEqual(len(costanza), 1)


if __name__ == "__main__":
    unittest.main()
