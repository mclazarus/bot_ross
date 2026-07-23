"""Unit tests for image_size.py's aspect-ratio-to-edit-size selection (used by &remix).

Run from the repo root:  python -m unittest test_image_size -v
"""

import math
import random
import unittest

import image_size
from image_size import (
    AUTO,
    LANDSCAPE,
    LANDSCAPE_THRESHOLD,
    PORTRAIT,
    PORTRAIT_THRESHOLD,
    SQUARE,
    describe_edit_size,
    edit_size_for_dimensions,
)

# (label, width, height, expected_size) -- concrete samples demonstrating the ratio
# buckets, per the approved plan's table.
SAMPLES = [
    ("square",          1024, 1024, SQUARE),
    ("4:3",             1600, 1200, LANDSCAPE),
    ("3:2",             1500, 1000, LANDSCAPE),
    ("16:9",            1920, 1080, LANDSCAPE),
    ("3:1 panorama",    3000, 1000, LANDSCAPE),
    ("5:4",             1280, 1024, LANDSCAPE),
    ("10:9",            1000, 900,  SQUARE),
    ("slightly wide",   1100, 1000, SQUARE),
    ("4:5",             1024, 1280, PORTRAIT),
    ("3:4",             1200, 1600, PORTRAIT),
    ("9:16",            1080, 1920, PORTRAIT),
    ("tall strip",      1000, 3000, PORTRAIT),
]


class SampleTableTest(unittest.TestCase):
    def test_concrete_samples(self):
        # Asserts each (width, height) sample maps to exactly the size the plan's
        # table claims -- the explicitly requested demonstration of the ratio math.
        for label, w, h, expected in SAMPLES:
            with self.subTest(label=label, w=w, h=h):
                self.assertEqual(
                    edit_size_for_dimensions(w, h), expected,
                    f"{label}: {w}x{h} (ratio={w/h:.3f}) expected {expected}",
                )


class BoundaryTest(unittest.TestCase):
    def test_exact_landscape_threshold_is_landscape(self):
        # A ratio exactly equal to LANDSCAPE_THRESHOLD (constructed from the same
        # float, height=1.0) must be LANDSCAPE, not SQUARE -- the ">=" rule means the
        # boundary value itself belongs to the landscape bucket.
        self.assertEqual(edit_size_for_dimensions(LANDSCAPE_THRESHOLD, 1.0), LANDSCAPE)

    def test_exact_portrait_threshold_is_portrait(self):
        # Same, for the "<=" rule on the portrait side.
        self.assertEqual(edit_size_for_dimensions(PORTRAIT_THRESHOLD, 1.0), PORTRAIT)

    def test_just_below_landscape_threshold_is_square(self):
        # One float ULP below the landscape threshold must NOT be landscape.
        just_below = math.nextafter(LANDSCAPE_THRESHOLD, 0.0)
        self.assertEqual(edit_size_for_dimensions(just_below, 1.0), SQUARE)

    def test_just_above_landscape_threshold_is_landscape(self):
        just_above = math.nextafter(LANDSCAPE_THRESHOLD, math.inf)
        self.assertEqual(edit_size_for_dimensions(just_above, 1.0), LANDSCAPE)

    def test_just_above_portrait_threshold_is_square(self):
        # One float ULP above the portrait threshold must NOT be portrait.
        just_above = math.nextafter(PORTRAIT_THRESHOLD, math.inf)
        self.assertEqual(edit_size_for_dimensions(just_above, 1.0), SQUARE)

    def test_just_below_portrait_threshold_is_portrait(self):
        just_below = math.nextafter(PORTRAIT_THRESHOLD, 0.0)
        self.assertEqual(edit_size_for_dimensions(just_below, 1.0), PORTRAIT)

    def test_thresholds_are_exact_reciprocals(self):
        # sqrt(1.5) * sqrt(2/3) == 1 -- the property that makes the buckets symmetric
        # under transposition (see TranspositionSymmetryTest).
        self.assertAlmostEqual(LANDSCAPE_THRESHOLD * PORTRAIT_THRESHOLD, 1.0, places=12)

    def test_landscape_and_portrait_rules_cannot_both_fire(self):
        # A ratio can only satisfy ">= LANDSCAPE_THRESHOLD" and "<= PORTRAIT_THRESHOLD"
        # at once if LANDSCAPE_THRESHOLD <= PORTRAIT_THRESHOLD, which must be false.
        self.assertGreater(LANDSCAPE_THRESHOLD, PORTRAIT_THRESHOLD)


class AutoFallbackTest(unittest.TestCase):
    def test_none_width(self):
        self.assertEqual(edit_size_for_dimensions(None, 1024), AUTO)

    def test_none_height(self):
        self.assertEqual(edit_size_for_dimensions(1024, None), AUTO)

    def test_both_none(self):
        self.assertEqual(edit_size_for_dimensions(None, None), AUTO)

    def test_zero_width(self):
        self.assertEqual(edit_size_for_dimensions(0, 1024), AUTO)

    def test_zero_height(self):
        self.assertEqual(edit_size_for_dimensions(1024, 0), AUTO)

    def test_negative_dimensions(self):
        self.assertEqual(edit_size_for_dimensions(-100, 1024), AUTO)
        self.assertEqual(edit_size_for_dimensions(1024, -100), AUTO)

    def test_non_numeric_dimensions(self):
        self.assertEqual(edit_size_for_dimensions("abc", 1024), AUTO)
        self.assertEqual(edit_size_for_dimensions(1024, object()), AUTO)


class ExtremeSizeTest(unittest.TestCase):
    def test_tiny_image(self):
        # 16x9 -- same ratio (1.778) as the 16:9 sample, at a much smaller scale.
        self.assertEqual(edit_size_for_dimensions(16, 9), LANDSCAPE)

    def test_huge_image(self):
        # 8000x4500 -- same ratio (1.778), at a much larger scale.
        self.assertEqual(edit_size_for_dimensions(8000, 4500), LANDSCAPE)


class TranspositionSymmetryTest(unittest.TestCase):
    def test_samples_swap_landscape_and_portrait_when_transposed(self):
        # Swapping width/height inverts the ratio; landscape picks must become
        # portrait and vice versa, square must stay square.
        for label, w, h, expected in SAMPLES:
            with self.subTest(label=label, w=w, h=h):
                transposed = edit_size_for_dimensions(h, w)
                if expected == LANDSCAPE:
                    self.assertEqual(transposed, PORTRAIT)
                elif expected == PORTRAIT:
                    self.assertEqual(transposed, LANDSCAPE)
                else:
                    self.assertEqual(transposed, SQUARE)

    def test_random_pairs_are_symmetric_under_transposition(self):
        rng = random.Random(12345)
        for _ in range(500):
            w = rng.uniform(1, 5000)
            h = rng.uniform(1, 5000)
            size = edit_size_for_dimensions(w, h)
            transposed_size = edit_size_for_dimensions(h, w)
            if size == LANDSCAPE:
                self.assertEqual(transposed_size, PORTRAIT)
            elif size == PORTRAIT:
                self.assertEqual(transposed_size, LANDSCAPE)
            else:
                self.assertEqual(transposed_size, SQUARE)


class ClosestInLogSpacePropertyTest(unittest.TestCase):
    """Independent check: for many random (w, h), the size edit_size_for_dimensions
    returns must be the allowed size whose ratio is nearest to w/h in LOG space. This
    does NOT call edit_size_for_dimensions's threshold constants or re-derive the
    bucketing rule -- it independently computes, for each of the three fixed target
    ratios, the absolute log-distance to the input ratio and picks the minimizer via
    a plain argmin, then asserts that matches the module's answer. This is a genuine
    cross-check of the algorithm (nearest-neighbor classification), not a restatement
    of the >=/<= threshold implementation."""

    TARGETS = {
        image_size.SQUARE_RATIO: SQUARE,
        image_size.LANDSCAPE_RATIO: LANDSCAPE,
        image_size.PORTRAIT_RATIO: PORTRAIT,
    }

    def _closest_by_log_distance(self, ratio):
        log_ratio = math.log(ratio)
        best_size = None
        best_distance = math.inf
        for target_ratio, size in self.TARGETS.items():
            distance = abs(log_ratio - math.log(target_ratio))
            if distance < best_distance:
                best_distance = distance
                best_size = size
        return best_size

    def test_matches_independent_nearest_neighbor_in_log_space(self):
        rng = random.Random(54321)
        for _ in range(2000):
            w = rng.uniform(1, 10000)
            h = rng.uniform(1, 10000)
            expected = self._closest_by_log_distance(w / h)
            actual = edit_size_for_dimensions(w, h)
            self.assertEqual(
                actual, expected,
                f"w={w}, h={h}, ratio={w/h}: implementation picked {actual}, "
                f"nearest-in-log-space is {expected}",
            )


class DescribeEditSizeTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(describe_edit_size(SQUARE), "square")
        self.assertEqual(describe_edit_size(LANDSCAPE), "landscape")
        self.assertEqual(describe_edit_size(PORTRAIT), "portrait")
        self.assertEqual(describe_edit_size(AUTO), "auto")

    def test_unknown_size_label(self):
        self.assertEqual(describe_edit_size("bogus"), "unknown")


if __name__ == "__main__":
    unittest.main()
