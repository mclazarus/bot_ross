"""Unit tests for the deterministic &release_image prompt generation.

These exercise release_image.py in isolation (no Discord/OpenAI), asserting the core
promise: the same input always yields the same prompt, different inputs yield
different prompts, and every generated prompt is well-formed and sensible.

Run from the repo root:  python -m unittest test_release_image -v
"""

import re
import unittest

import release_image
from release_image import (
    RELEASE_ALGORITHMS,
    build_release_prompt,
    get_release_algorithm,
    latest_version,
    parse_release_args,
    release_seed,
)

# A large batch of distinct inputs standing in for real git hashes / release names.
SAMPLE_INPUTS = [f"{i:040x}" for i in range(500)]


class DeterminismTest(unittest.TestCase):
    def test_same_input_same_prompt(self):
        first = build_release_prompt("deadbeef")[0]
        for _ in range(50):
            self.assertEqual(build_release_prompt("deadbeef")[0], first)

    def test_whitespace_normalized(self):
        # Leading/trailing whitespace is stripped, so these hash identically.
        self.assertEqual(build_release_prompt("  deadbeef ")[0], build_release_prompt("deadbeef")[0])

    def test_seed_is_stable_and_short(self):
        self.assertEqual(release_seed("deadbeef"), release_seed("deadbeef"))
        self.assertEqual(len(release_seed("deadbeef")), 8)


class UniquenessAtScaleTest(unittest.TestCase):
    def test_all_distinct_and_reproducible(self):
        prompts = {}
        for src in SAMPLE_INPUTS:
            prompt, _, _ = build_release_prompt(src)
            # Re-running the same input reproduces its own prompt exactly.
            self.assertEqual(build_release_prompt(src)[0], prompt)
            prompts[src] = prompt
        # Every input produced a prompt distinct from every other input's.
        self.assertEqual(len(set(prompts.values())), len(SAMPLE_INPUTS))

    def test_different_inputs_differ(self):
        self.assertNotEqual(build_release_prompt("deadbeef")[0], build_release_prompt("cafef00d")[0])


class VarietyTest(unittest.TestCase):
    def test_each_category_covers_a_healthy_fraction(self):
        _, algo = get_release_algorithm()
        seen = {cat: set() for cat in algo["categories"]}
        for src in SAMPLE_INPUTS:
            _, sub_algo = get_release_algorithm()
            for cat in sub_algo["categories"]:
                wl = sub_algo["word_lists"][cat]
                idx = release_image._release_index(src, latest_version(), cat, len(wl))
                seen[cat].add(idx)
        # Over 500 inputs, selection should spread across most of every list, not
        # collapse onto a handful of values.
        for cat, indices in seen.items():
            list_len = len(algo["word_lists"][cat])
            coverage = len(indices) / list_len
            self.assertGreater(coverage, 0.5, f"category {cat} only covered {coverage:.0%} of its list")


class StructuralValidityTest(unittest.TestCase):
    def test_prompts_are_well_formed(self):
        _, algo = get_release_algorithm()
        for src in SAMPLE_INPUTS[:100]:
            prompt, _, _ = build_release_prompt(src)
            self.assertTrue(prompt.strip(), "prompt must be non-empty")
            # No leftover unfilled {placeholder} tokens.
            self.assertNotIn("{", prompt)
            self.assertNotIn("}", prompt)
            # Every category's chosen phrase actually appears in the output.
            for cat in algo["categories"]:
                wl = algo["word_lists"][cat]
                pick = wl[release_image._release_index(src, latest_version(), cat, len(wl))]
                self.assertIn(pick, prompt, f"{cat} pick missing from prompt for {src}")


class GeorgifyTest(unittest.TestCase):
    def test_georgify_wraps_subject_and_leaves_others_untouched(self):
        plain, _, _ = build_release_prompt("deadbeef", georgify=False)
        george, _, _ = build_release_prompt("deadbeef", georgify=True)
        self.assertNotEqual(plain, george)
        self.assertIn("George Costanza if he were", george)

        # Non-subject picks are identical between the two runs (same seed).
        _, algo = get_release_algorithm()
        for cat in algo["categories"]:
            if cat == "subject":
                continue
            wl = algo["word_lists"][cat]
            pick = wl[release_image._release_index("deadbeef", latest_version(), cat, len(wl))]
            self.assertIn(pick, plain)
            self.assertIn(pick, george)

    def test_georgified_prompt_contains_raw_subject(self):
        george, _, _ = build_release_prompt("deadbeef", georgify=True)
        _, algo = get_release_algorithm()
        wl = algo["word_lists"]["subject"]
        subject = wl[release_image._release_index("deadbeef", latest_version(), "subject", len(wl))]
        self.assertIn(f"George Costanza if he were {subject}", george)


class VersioningTest(unittest.TestCase):
    def test_unknown_version_raises_keyerror(self):
        with self.assertRaises(KeyError):
            build_release_prompt("deadbeef", version="999")

    def test_default_is_latest(self):
        _, _, ver = build_release_prompt("deadbeef")
        self.assertEqual(ver, latest_version())

    def test_version_folded_into_selection(self):
        # A synthetic v2 with the SAME word lists as v1 must still (almost always)
        # produce a different prompt for the same input, because version is part of
        # the hash key. This guards the versioning contract even before a real v2.
        v1 = RELEASE_ALGORITHMS[latest_version()]
        algos = {"1": v1, "2": dict(v1)}
        p1, _, _ = build_release_prompt("deadbeef", version="1", algorithms=algos)
        p2, _, _ = build_release_prompt("deadbeef", version="2", algorithms=algos)
        self.assertNotEqual(p1, p2)


class ArgParsingTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_release_args("abc123f"), ("abc123f", None, False))

    def test_george_flag(self):
        self.assertEqual(parse_release_args("--george abc123f"), ("abc123f", None, True))
        self.assertEqual(parse_release_args("--costanza abc123f"), ("abc123f", None, True))

    def test_input_with_spaces_survives_flags(self):
        self.assertEqual(parse_release_args("--george my cool release"), ("my cool release", None, True))

    def test_version_forms(self):
        self.assertEqual(parse_release_args("--v2 abc")[1], "2")
        self.assertEqual(parse_release_args("--version=3 abc")[1], "3")
        self.assertEqual(parse_release_args("--version 4 abc")[1], "4")

    def test_flags_only_yields_empty_source(self):
        self.assertEqual(parse_release_args("--george")[0], "")


class DataIntegrityTest(unittest.TestCase):
    MINIMUMS = {"subject": 100, "setting": 100}
    DEFAULT_MINIMUM = 24

    def test_every_version_is_well_formed(self):
        self.assertTrue(RELEASE_ALGORITHMS, "algorithm artifact must not be empty")
        placeholder = re.compile(r"\{(\w+)\}")
        for version, algo in RELEASE_ALGORITHMS.items():
            categories = algo["categories"]
            word_lists = algo["word_lists"]

            # Every category has a non-empty list meeting its minimum, with no dupes.
            for cat in categories:
                self.assertIn(cat, word_lists, f"v{version}: {cat} missing from word_lists")
                wl = word_lists[cat]
                self.assertTrue(wl, f"v{version}: {cat} is empty")
                minimum = self.MINIMUMS.get(cat, self.DEFAULT_MINIMUM)
                self.assertGreaterEqual(len(wl), minimum, f"v{version}: {cat} has only {len(wl)} entries")
                self.assertEqual(len(wl), len(set(wl)), f"v{version}: {cat} has duplicate entries")

            # Every {placeholder} in the template maps to a category.
            for name in placeholder.findall(algo["template"]):
                self.assertIn(name, categories, f"v{version}: template uses unknown {{{name}}}")

            # georgify_template must reference {subject} and subject must be a category.
            self.assertIn("{subject}", algo["georgify_template"])
            self.assertIn("subject", categories)

    def test_no_stray_format_braces_in_word_lists(self):
        # Word-list phrases feed str.format via the template; stray braces would raise.
        for version, algo in RELEASE_ALGORITHMS.items():
            for cat, wl in algo["word_lists"].items():
                for phrase in wl:
                    self.assertNotIn("{", phrase, f"v{version} {cat}: stray '{{' in {phrase!r}")
                    self.assertNotIn("}", phrase, f"v{version} {cat}: stray '}}' in {phrase!r}")


if __name__ == "__main__":
    unittest.main()
