"""Unit tests for ;macro expansion logic.

These exercise macros.py in isolation (no Discord/OpenAI), asserting: in-place
substitution, fallback behavior on a miss, no-recursion, id
normalization/validation, library I/O, and seed-data integrity. Mirrors
test_magic_paint.py's structure.

Run from the repo root:  python -m unittest test_macros -v
"""

import os
import tempfile
import unittest

from macros import (
    FALLBACK_EXPANSIONS,
    entry_id,
    expand_macros,
    is_valid_macro_id,
    load_macro_library,
    normalize_macro_id,
    save_macro_library,
)

SEED_LIBRARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macros.json")

# A stand-in library so most tests never touch disk.
STUB_ENTRIES = [{"id": "cat", "text": "the world's most patient cat,"}]


class ExpandMacrosTest(unittest.TestCase):
    def test_single_macro_mid_sentence(self):
        # The README example, verbatim, against the real seed library.
        entries = load_macro_library(SEED_LIBRARY_FILE)
        rhe_text = next(e["text"] for e in entries if e["id"] == "rhe")
        prompt, hits, misses = expand_macros("A ;rhe is trapped in a datacenter", entries=entries)
        self.assertEqual(prompt, f"A {rhe_text} is trapped in a datacenter")
        self.assertEqual(hits, ["rhe"])
        self.assertEqual(misses, [])

    def test_multiple_macros_in_one_prompt(self):
        entries = [
            {"id": "cat", "text": "CAT,"},
            {"id": "dog", "text": "DOG,"},
        ]
        prompt, hits, misses = expand_macros("A ;cat and a ;dog walk in", entries=entries)
        self.assertEqual(prompt, "A CAT, and a DOG, walk in")
        self.assertEqual(hits, ["cat", "dog"])
        self.assertEqual(misses, [])

    def test_repeated_macro(self):
        prompt, hits, misses = expand_macros(";cat meets ;cat", entries=STUB_ENTRIES)
        self.assertEqual(prompt, "the world's most patient cat, meets the world's most patient cat,")
        self.assertEqual(hits, ["cat", "cat"])
        self.assertEqual(misses, [])

    def test_unknown_macro_gets_fallback_and_is_reported(self):
        prompt, hits, misses = expand_macros("A ;nope stands here", entries=STUB_ENTRIES)
        self.assertEqual(hits, [])
        self.assertEqual(misses, ["nope"])
        self.assertNotIn(";nope", prompt)
        self.assertTrue(any(fb in prompt for fb in FALLBACK_EXPANSIONS))

    def test_mixed_hit_and_miss(self):
        prompt, hits, misses = expand_macros("A ;cat and a ;dog", entries=STUB_ENTRIES)
        self.assertEqual(hits, ["cat"])
        self.assertEqual(misses, ["dog"])
        self.assertIn("the world's most patient cat,", prompt)
        self.assertNotIn(";dog", prompt)
        self.assertTrue(any(fb in prompt for fb in FALLBACK_EXPANSIONS))

    def test_case_insensitive_lookup(self):
        prompt, hits, misses = expand_macros(";CAT and ;Cat", entries=STUB_ENTRIES)
        self.assertEqual(hits, ["cat", "cat"])
        self.assertEqual(misses, [])
        self.assertEqual(prompt, "the world's most patient cat, and the world's most patient cat,")

    def test_semicolon_before_space_or_punctuation_is_untouched(self):
        prompt, hits, misses = expand_macros("Wait; ok. Also: nope; really?", entries=STUB_ENTRIES)
        self.assertEqual(prompt, "Wait; ok. Also: nope; really?")
        self.assertEqual(hits, [])
        self.assertEqual(misses, [])

    def test_double_semicolon_does_not_escape(self):
        # ';;cat' -- the first ';' is untouched prose, the second still triggers 'cat'.
        prompt, hits, misses = expand_macros(";;cat", entries=STUB_ENTRIES)
        self.assertEqual(prompt, ";the world's most patient cat,")
        self.assertEqual(hits, ["cat"])
        self.assertEqual(misses, [])

    def test_no_recursion_into_expanded_text(self):
        entries = [
            {"id": "a", "text": "see ;b here,"},
            {"id": "b", "text": "NESTED"},
        ]
        prompt, hits, misses = expand_macros("start ;a end", entries=entries)
        self.assertEqual(prompt, "start see ;b here, end")
        self.assertEqual(hits, ["a"])
        self.assertEqual(misses, [])  # 'b' is never scanned -- it's inert literal text

    def test_empty_library_fails_open(self):
        prompt, hits, misses = expand_macros("A ;cat here", entries=[])
        self.assertEqual(hits, [])
        self.assertEqual(misses, ["cat"])
        self.assertNotIn(";cat", prompt)

    def test_missing_path_and_no_entries_fails_open(self):
        prompt, hits, misses = expand_macros("A ;cat here")
        self.assertEqual(hits, [])
        self.assertEqual(misses, ["cat"])

    def test_missing_library_file_fails_open(self):
        prompt, hits, misses = expand_macros("A ;cat here", path="/no/such/macros.json")
        self.assertEqual(hits, [])
        self.assertEqual(misses, ["cat"])

    def test_entry_without_text_is_a_miss(self):
        prompt, hits, misses = expand_macros("A ;cat here", entries=[{"id": "cat"}])
        self.assertEqual(hits, [])
        self.assertEqual(misses, ["cat"])

    def test_malformed_entries_are_skipped_not_crash(self):
        # A single hand-corrupted row in data/macros.json (non-dict item, non-string
        # id or text, blank id) must not take down expansion for the other, valid
        # macros -- that's the fails-open promise. The good ;cat still resolves.
        entries = [
            {"id": "cat", "text": "CAT,"},
            "not a dict",
            {"id": 42, "text": "numeric id,"},
            {"id": "bad", "text": {"nested": "object"}},
            {"id": "", "text": "blank id,"},
            {"text": "no id at all,"},
        ]
        prompt, hits, misses = expand_macros("A ;cat and a ;bad", entries=entries)
        self.assertEqual(hits, ["cat"])
        self.assertEqual(misses, ["bad"])  # its non-string text made it unusable -> miss
        self.assertIn("CAT,", prompt)

    def test_no_tokens_leaves_prompt_untouched(self):
        prompt, hits, misses = expand_macros("just a normal prompt", entries=STUB_ENTRIES)
        self.assertEqual(prompt, "just a normal prompt")
        self.assertEqual(hits, [])
        self.assertEqual(misses, [])


class NormalizeAndValidateTest(unittest.TestCase):
    def test_normalize_strips_semicolon_whitespace_and_lowercases(self):
        self.assertEqual(normalize_macro_id(" ;RHE "), "rhe")
        self.assertEqual(normalize_macro_id("Cat"), "cat")
        self.assertEqual(normalize_macro_id(";already-lower"), "already-lower")

    def test_valid_ids(self):
        for good in ("rhe", "a", "a_b-9", "a" * 32):
            self.assertTrue(is_valid_macro_id(good), good)

    def test_invalid_ids(self):
        for bad in ("", "UPPER", "has space", "semi;colon", "a" * 33):
            self.assertFalse(is_valid_macro_id(bad), bad)

    def test_entry_id_normalizes_good_rows_and_rejects_malformed(self):
        # The &macro_* commands match on entry_id(); it must never raise on a
        # hand-corrupted row, returning None so that row is simply skipped.
        self.assertEqual(entry_id({"id": " ;Cat "}), "cat")
        for malformed in ("not a dict", 42, None, {"id": 42}, {"id": ""}, {"id": "  "}, {"text": "no id"}):
            self.assertIsNone(entry_id(malformed), malformed)


class LibraryIoTest(unittest.TestCase):
    def test_save_load_round_trip_preserves_unicode(self):
        entries = [{"id": "heart", "text": "I ♥️ this — really,"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lib.json")
            save_macro_library(entries, path)
            self.assertEqual(load_macro_library(path), entries)
            # ensure_ascii=False keeps the raw unicode on disk (not \uXXXX escapes).
            with open(path, encoding="utf-8") as f:
                self.assertIn("♥️", f.read())

    def test_load_missing_file_fails_open(self):
        self.assertEqual(load_macro_library("/no/such/file.json"), [])

    def test_load_malformed_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lib.json")
            with open(path, "w") as f:
                f.write("{ not valid json")
            self.assertEqual(load_macro_library(path), [])


class SeedLibraryDataTest(unittest.TestCase):
    """The shipped seed library must be structurally sound."""

    def setUp(self):
        self.entries = load_macro_library(SEED_LIBRARY_FILE)

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

    def test_all_ids_are_valid(self):
        for e in self.entries:
            self.assertTrue(is_valid_macro_id(e["id"]), e["id"])

    def test_rhe_exists(self):
        ids = {e["id"] for e in self.entries}
        self.assertIn("rhe", ids)

    def test_no_expansion_carries_a_leading_article(self):
        # The writer supplies the article ("A ;rhe is trapped..."), so an expansion
        # that started with one would render "A a caucasian engineer...". The same
        # convention binds the joke fallbacks, which substitute in the same position.
        articles = ("a ", "an ", "the ")
        for text in [e["text"] for e in self.entries] + list(FALLBACK_EXPANSIONS):
            self.assertFalse(text.lower().startswith(articles), f"leading article in: {text}")


if __name__ == "__main__":
    unittest.main()
