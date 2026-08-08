"""Unit tests for pipe_chain.py: `|` pipe-chain splitting and the fixed status-message
templates. Pure module, no Discord/OpenAI/bot imports, no file I/O -- nothing here
touches data/.

Run from the repo root:  python -m unittest test_pipe_chain -v
"""

import os
import unittest

from pipe_chain import (
    MAX_SEGMENTS,
    TOO_MANY_MESSAGE,
    ArtResult,
    chain_complete_message,
    chain_stopped_message,
    dropped_note,
    split_pipeline,
)

README_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")


class SplitPipelineTest(unittest.TestCase):
    def test_no_pipe_is_verbatim(self):
        # Case 1: no "|" at all -> ([raw], 0, None), raw untouched.
        self.assertEqual(split_pipeline("a cat"), (["a cat"], 0, None))

    def test_no_pipe_preserves_whitespace_verbatim(self):
        # Case 2: the single most important compatibility guarantee -- with no "|",
        # the text is NOT stripped, so every existing non-piped command sees
        # byte-for-byte the same prompt it always has.
        self.assertEqual(split_pipeline("  a cat  "), (["  a cat  "], 0, None))

    def test_empty_string_no_pipe_is_verbatim(self):
        # Case 3: degenerate but contractual -- no "|" means verbatim, even for "".
        self.assertEqual(split_pipeline(""), ([""], 0, None))

    def test_two_segments_are_end_stripped(self):
        # Case 4.
        self.assertEqual(
            split_pipeline("a cat | make it blue"),
            (["a cat", "make it blue"], 0, None),
        )

    def test_no_spaces_required_around_pipe(self):
        # Case 5.
        self.assertEqual(split_pipeline("a|b"), (["a", "b"], 0, None))

    def test_internal_whitespace_preserved_only_ends_stripped(self):
        # Case 6: only the ends of each segment are stripped -- internal whitespace
        # runs (which a plain &paint prompt would also carry through unchanged) stay
        # exactly as typed.
        self.assertEqual(
            split_pipeline("a  big   cat | very   blue"),
            (["a  big   cat", "very   blue"], 0, None),
        )

    def test_four_pipes_five_segments_is_the_boundary_ok(self):
        # Case 7: 4 pipes / 5 segments is the cap -- allowed.
        self.assertEqual(
            split_pipeline("a|b|c|d|e"),
            (["a", "b", "c", "d", "e"], 0, None),
        )

    def test_five_pipes_is_too_many(self):
        # Case 8: one pipe past the boundary -> refused outright.
        self.assertEqual(split_pipeline("a|b|c|d|e|f"), ([], 0, "too_many"))

    def test_five_pipes_with_empty_middles_is_still_too_many(self):
        # Case 9: THE single most important case in the module. Raw parts are
        # counted BEFORE dropping empties -- "a | | | | | b" is five pipes (six raw
        # parts, four blank), so it must be a flat refusal, not a quiet reduction to
        # ["a", "b"]. A user mashing the pipe key should never end up with a
        # SHORTER chain than what they typed by accident.
        self.assertEqual(split_pipeline("a | | | | | b"), ([], 0, "too_many"))

    def test_four_pipes_with_two_empties_drops_and_counts_them(self):
        # Case 10: at the cap (4 pipes -> 5 raw parts), empties are dropped and
        # counted normally -- only a raw part count OVER the cap is refused.
        self.assertEqual(
            split_pipeline("a | | b | | c"),
            (["a", "b", "c"], 2, None),
        )

    def test_trailing_pipe_drops_one_empty(self):
        # Case 11.
        self.assertEqual(split_pipeline("a cat |"), (["a cat"], 1, None))

    def test_leading_pipe_drops_one_empty(self):
        # Case 12.
        self.assertEqual(split_pipeline("| a cat"), (["a cat"], 1, None))

    def test_single_pipe_all_empty_reports_empty_with_honest_dropped(self):
        # Case 13: both raw forms report "empty" with dropped == 2 (two raw parts,
        # both blank once stripped) -- never silently swallowed.
        self.assertEqual(split_pipeline("|"), ([], 2, "empty"))
        self.assertEqual(split_pipeline(" | "), ([], 2, "empty"))

    def test_three_pipes_all_empty_is_empty_not_too_many(self):
        # Case 14: 3 pipes -> 4 raw parts, all blank. Under the cap, so "empty" wins.
        self.assertEqual(split_pipeline("|||"), ([], 4, "empty"))

    def test_five_pipes_all_empty_is_too_many_not_empty(self):
        # Case 15: precedence test. 5 pipes -> 6 raw parts, all blank -- the cap
        # check runs BEFORE empty-dropping, so "too_many" wins over "empty" even
        # though every part is blank.
        self.assertEqual(split_pipeline("|||||"), ([], 0, "too_many"))

    def test_mixed_empties_dropped_counts_every_one(self):
        # Case 16.
        self.assertEqual(split_pipeline("a || b |"), (["a", "b"], 2, None))

    def test_flags_are_inert_text_to_the_splitter(self):
        # Case 17: --res/orientation flags are just prose here; they're parsed
        # per-segment later (by _prep_generation_size / _pipe_edit_once), never by
        # split_pipeline itself.
        self.assertEqual(
            split_pipeline("draw --res 1920x1080 a cat | make it night"),
            (["draw --res 1920x1080 a cat", "make it night"], 0, None),
        )

    def test_only_ascii_pipe_splits(self):
        # Case 18: U+FF5C (fullwidth vertical bar, "｜") looks similar but is NOT
        # ASCII "|" and must not split -- no unicode-lookalike escape hatch.
        self.assertEqual(split_pipeline("a｜b"), (["a｜b"], 0, None))

    def test_max_segments_parameter_is_honored_not_the_constant(self):
        # Case 19: with a custom cap of 2, two pipes (3 raw parts) is already over.
        self.assertEqual(split_pipeline("a|b|c", max_segments=2), ([], 0, "too_many"))

    def test_max_segments_parameter_boundary(self):
        # Case 20: one pipe (2 raw parts) is exactly at a custom cap of 2 -- allowed.
        self.assertEqual(split_pipeline("a|b", max_segments=2), (["a", "b"], 0, None))

    def test_non_str_input_raises_type_error(self):
        # Case 21: non-str is a caller bug, not a value to coerce -- let the natural
        # `"|" in raw` TypeError propagate rather than catching and hiding it.
        with self.assertRaises(TypeError):
            split_pipeline(None)

    def test_purity_no_mutation_distinct_objects(self):
        # Case 22: calling split_pipeline twice on the same input gives equal but
        # DISTINCT list objects (never a cached/shared list a caller could mutate
        # out from under a later call), and the input itself is untouched.
        raw = "a | b"
        result1 = split_pipeline(raw)
        result2 = split_pipeline(raw)
        self.assertEqual(result1, result2)
        self.assertIsNot(result1[0], result2[0])
        self.assertEqual(raw, "a | b")


class MessageTemplateTest(unittest.TestCase):
    def test_too_many_message_exact(self):
        # Case 23: exact, including the comma and the trailing period -- contractual.
        self.assertEqual(TOO_MANY_MESSAGE, "Okay, simmer down, buddy.")

    def test_chain_stopped_message_contractual_example(self):
        # Case 24.
        self.assertEqual(chain_stopped_message(2, 4), "Chain stopped at step 2 of 4.")

    def test_chain_stopped_message_step_one_failure(self):
        # Case 25: the first segment itself can fail -- same template, step 1.
        self.assertEqual(chain_stopped_message(1, 5), "Chain stopped at step 1 of 5.")

    def test_chain_stopped_message_last_step_failure(self):
        # Case 26.
        self.assertEqual(chain_stopped_message(5, 5), "Chain stopped at step 5 of 5.")

    def test_chain_complete_message_minimum_chain(self):
        # Case 27: 2 is the smallest possible real chain (1 segment isn't a chain).
        self.assertEqual(chain_complete_message(2), "Chain complete: 2 steps.")

    def test_chain_complete_message_max_chain(self):
        # Case 28.
        self.assertEqual(chain_complete_message(5), "Chain complete: 5 steps.")

    def test_dropped_note_singular(self):
        # Case 29: no trailing "s" for exactly one.
        self.assertEqual(dropped_note(1), "(skipping 1 empty pipe segment)")

    def test_dropped_note_plural(self):
        # Case 30.
        self.assertEqual(dropped_note(2), "(skipping 2 empty pipe segments)")

    def test_max_segments_is_five(self):
        # Case 31: pinned -- 4 pipes allowed, 5 refused. Changing this silently would
        # invalidate the README's documented cap and TOO_MANY_MESSAGE's threshold.
        self.assertEqual(MAX_SEGMENTS, 5)


class ArtResultTest(unittest.TestCase):
    def test_always_truthy_even_when_every_field_is_falsy(self):
        # Case 32: this is the whole reason ArtResult exists as a NamedTuple rather
        # than, say, a dataclass or a bare boolean. &meme's `if await do_the_art(...)`
        # (bot_ross.py) must see a truthy value on success no matter how "empty" the
        # actual fields are -- a NamedTuple is a non-empty tuple (4 elements) and is
        # therefore ALWAYS truthy, guarding against a future refactor (e.g. to a
        # dataclass with a custom __len__/__bool__, or an accidental empty tuple)
        # silently breaking every truthiness-only call site's counter/branch.
        result = ArtResult(message=None, image_bytes=b"", size=None, elapsed=0.0)
        self.assertIs(bool(result), True)
        self.assertTrue(result)

    def test_field_access(self):
        # Case 33.
        r = ArtResult("m", b"x", "1024x1024", 1.5)
        self.assertEqual(r.message, "m")
        self.assertEqual(r.image_bytes, b"x")
        self.assertEqual(r.size, "1024x1024")
        self.assertEqual(r.elapsed, 1.5)

    def test_tuple_order_is_contractual(self):
        # Case 34: field order is part of the contract -- positional unpacking (used
        # by _run_chain to pull anchor/prev_bytes out of a step's result) must land
        # in this exact order.
        r = ArtResult("m", b"x", "1024x1024", 1.5)
        self.assertEqual(tuple(r), ("m", b"x", "1024x1024", 1.5))
        m, b, s, e = r
        self.assertEqual((m, b, s, e), ("m", b"x", "1024x1024", 1.5))


class ReadmeConsistencyTest(unittest.TestCase):
    # split_pipeline refuses when len(raw.split("|")) > MAX_SEGMENTS, and
    # raw.split("|") has (pipes + 1) elements -- so the largest ALLOWED chain has
    # MAX_SEGMENTS - 1 pipes / MAX_SEGMENTS segments, and refusal first kicks in at
    # MAX_SEGMENTS pipes, i.e. MAX_SEGMENTS + 1 segments. README.md documents this
    # threshold in prose (it can't import MAX_SEGMENTS), so it drifts silently if
    # the constant ever changes -- this test pins the exact sentence so a mismatch
    # (like the "5+ segments" typo this test was written to catch, which should
    # have read "6+ segments" for MAX_SEGMENTS=5) fails the suite instead of only
    # misleading a README reader.
    def test_pipe_cap_wording_matches_max_segments(self):
        max_pipes_allowed = MAX_SEGMENTS - 1
        min_segments_refused = MAX_SEGMENTS + 1
        expected = "More than {} pipes ({}+ segments)".format(
            max_pipes_allowed, min_segments_refused
        )
        with open(README_PATH, "r", encoding="utf-8") as f:
            # README wraps this sentence across a line break; normalize whitespace
            # (including the newline) so the check isn't sensitive to line-wrap width.
            readme_text = " ".join(f.read().split())
        self.assertIn(expected, readme_text)


if __name__ == "__main__":
    unittest.main()
