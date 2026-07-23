"""Unit tests for image_size.py: &remix's aspect-ratio-to-edit-size selection and the
&paint family's generation sizing (flag parsing, --res coercion, size resolution).

Run from the repo root:  python -m unittest test_image_size -v
"""

import random
import unittest

import image_size
from image_size import (
    AUTO,
    GEN_MAX_LONG,
    GEN_MAX_RATIO,
    GEN_MAX_SHORT,
    GEN_MIN_SHORT,
    GEN_STEP,
    LANDSCAPE,
    ORIENTATIONS,
    PORTRAIT,
    SQUARE,
    coerce_generation_size,
    describe_edit_size,
    parse_resolution,
    parse_size_flags,
    resolve_edit_size,
    resolve_generation_size,
)


class DescribeEditSizeTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(describe_edit_size(SQUARE), "square")
        self.assertEqual(describe_edit_size(LANDSCAPE), "landscape")
        self.assertEqual(describe_edit_size(PORTRAIT), "portrait")
        self.assertEqual(describe_edit_size(AUTO), "auto")

    def test_unknown_size_label(self):
        self.assertEqual(describe_edit_size("bogus"), "unknown")

    def test_arbitrary_size_classified_by_orientation(self):
        # --res can now coerce to an arbitrary edit size, so the label is derived from
        # the WxH rather than falling back to "unknown".
        self.assertEqual(describe_edit_size("1536x640"), "landscape")
        self.assertEqual(describe_edit_size("640x1536"), "portrait")
        self.assertEqual(describe_edit_size("1200x1200"), "square")


class ParseSizeFlagsTest(unittest.TestCase):
    def test_orientation_flags_stripped_and_mapped(self):
        for flag, orientation in (
            ("--square", "square"),
            ("--landscape", "landscape"),
            ("--portrait", "portrait"),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(
                    parse_size_flags(f"{flag} a wide valley"),
                    ("a wide valley", orientation, None),
                )

    def test_res_with_space_value(self):
        self.assertEqual(
            parse_size_flags("a cat --res 800x600"),
            ("a cat", None, "800x600"),
        )

    def test_res_with_equals_value(self):
        self.assertEqual(
            parse_size_flags("a cat --res=800x600"),
            ("a cat", None, "800x600"),
        )

    def test_case_insensitive_orientation(self):
        self.assertEqual(
            parse_size_flags("--Landscape a cat"),
            ("a cat", "landscape", None),
        )

    def test_case_insensitive_res(self):
        self.assertEqual(
            parse_size_flags("--RES 800x600 a cat"),
            ("a cat", None, "800x600"),
        )

    def test_flag_positions_start_middle_end_preserve_prose(self):
        self.assertEqual(
            parse_size_flags("--landscape a wide valley"),
            ("a wide valley", "landscape", None),
        )
        self.assertEqual(
            parse_size_flags("a wide --res 800x600 valley"),
            ("a wide valley", None, "800x600"),
        )
        self.assertEqual(
            parse_size_flags("a wide valley --portrait"),
            ("a wide valley", "portrait", None),
        )

    def test_no_flags_passthrough(self):
        self.assertEqual(
            parse_size_flags("just a normal prompt"),
            ("just a normal prompt", None, None),
        )

    def test_multiple_orientation_flags_last_wins(self):
        self.assertEqual(
            parse_size_flags("--square --landscape --portrait a cat"),
            ("a cat", "portrait", None),
        )

    def test_trailing_res_with_no_value_is_dropped(self):
        self.assertEqual(
            parse_size_flags("a cat --res"),
            ("a cat", None, None),
        )

    def test_invalid_res_value_returned_raw(self):
        self.assertEqual(
            parse_size_flags("--res notasize a cat"),
            ("a cat", None, "notasize"),
        )
        with self.assertRaises(ValueError):
            parse_resolution("notasize")

    def test_flags_only_leaves_empty_remaining_text(self):
        # This gates real control flow: an empty remaining prompt drives the
        # "...I need something to paint besides the size flags" branch, and remix's
        # fall-back-to-None (image-only remix) path.
        self.assertEqual(parse_size_flags("--landscape"), ("", "landscape", None))
        self.assertEqual(parse_size_flags("--res 800x600"), ("", None, "800x600"))

    def test_no_flags_preserves_original_whitespace_verbatim(self):
        # With no recognized flag, the text is returned untouched (not re-joined), so
        # a multi-line / multi-space prompt on the common no-flags path is unchanged.
        original = "a wide   valley\nwith two lines"
        self.assertEqual(parse_size_flags(original), (original, None, None))


class ParseResolutionTest(unittest.TestCase):
    def test_valid_forms(self):
        for text in ("1920x1080", "1920X1080", " 1920 x 1080 "):
            with self.subTest(text=text):
                self.assertEqual(parse_resolution(text), (1920, 1080))

    def test_invalid_raises_value_error(self):
        for bad in (
            "abc", "1920", "1920x", "x1080", "1920x1080x1",
            "-16x16", "0x100", "16.5x16", "",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_resolution(bad)


class CoerceGenerationSizeTest(unittest.TestCase):
    # (w, h, expected) -- the 15-row worked table from the implementation spec.
    TABLE = [
        (1536, 1024, "1536x1024"),
        (1000, 1000, "1008x1008"),
        (4000, 1000, "3008x1008"),
        (1000, 4000, "1008x3008"),
        (5000, 2000, "3840x1536"),
        (2000, 5000, "1536x3840"),
        (8000, 6000, "2880x2160"),
        (3000, 3000, "2160x2160"),
        (100, 100, "256x256"),
        (30, 10, "768x256"),
        (10, 30, "256x768"),
        (50, 40, "320x256"),
        (3840, 2160, "3840x2160"),
        (3840, 1280, "3840x1280"),
        (783, 261, "768x256"),
    ]

    def test_sample_table(self):
        for w, h, expected in self.TABLE:
            with self.subTest(w=w, h=h):
                self.assertEqual(coerce_generation_size(w, h), expected)

    def test_rounding_reclamp_is_required(self):
        # Without step 5's re-clamp, independent per-axis rounding of (783, 261)
        # would produce (784, 256) -- ratio 3.0625, which is > GEN_MAX_RATIO and
        # therefore invalid. Step 5 snaps it back to (768, 256), ratio exactly 3.0.
        self.assertEqual(coerce_generation_size(783, 261), "768x256")
        self.assertGreater(784 / 256, GEN_MAX_RATIO)

    def test_property_sweep(self):
        rng = random.Random(20260721)
        pairs = []
        for _ in range(2000):
            pairs.append((rng.randint(1, 20000), rng.randint(1, 20000)))
        for _ in range(500):
            if rng.random() < 0.5:
                pairs.append((rng.randint(1, 50), rng.randint(1000, 20000)))
            else:
                pairs.append((rng.randint(1000, 20000), rng.randint(1, 50)))
        for _ in range(500):
            pairs.append((rng.randint(1, 50), rng.randint(1, 50)))
        for _ in range(500):
            pairs.append((rng.randint(20000, 200000), rng.randint(20000, 200000)))

        for w, h in pairs:
            with self.subTest(w=w, h=h):
                size = coerce_generation_size(w, h)
                out_w, out_h = parse_resolution(size)
                self.assertEqual(out_w % GEN_STEP, 0)
                self.assertEqual(out_h % GEN_STEP, 0)
                self.assertGreater(out_w, 0)
                self.assertGreater(out_h, 0)
                ratio = out_w / out_h
                self.assertGreaterEqual(ratio, (1 / GEN_MAX_RATIO) - 1e-9)
                self.assertLessEqual(ratio, GEN_MAX_RATIO + 1e-9)
                self.assertLessEqual(max(out_w, out_h), GEN_MAX_LONG)
                self.assertLessEqual(min(out_w, out_h), GEN_MAX_SHORT)
                # the shorter side is never dropped below the floor (step 3 raises it,
                # and step 4/5 rounding can only land it on GEN_MIN_SHORT at lowest).
                self.assertGreaterEqual(min(out_w, out_h), GEN_MIN_SHORT)


class ResolveGenerationSizeTest(unittest.TestCase):
    def test_orientation_only(self):
        for o in ("square", "landscape", "portrait"):
            with self.subTest(o=o):
                self.assertEqual(
                    resolve_generation_size(orientation=o), (ORIENTATIONS[o], None)
                )

    def test_res_wh_already_valid_no_notice(self):
        size, requested = resolve_generation_size(res_wh=(1536, 1024))
        self.assertEqual((size, requested), ("1536x1024", "1536x1024"))
        self.assertEqual(size, requested)

    def test_res_wh_coerced_notice_expected(self):
        size, requested = resolve_generation_size(res_wh=(1920, 1081))
        self.assertEqual((size, requested), ("1920x1088", "1920x1081"))
        self.assertNotEqual(size, requested)

    def test_res_wh_overrides_orientation(self):
        size, requested = resolve_generation_size(orientation="portrait", res_wh=(1536, 1024))
        self.assertEqual((size, requested), ("1536x1024", "1536x1024"))
        self.assertNotEqual(size, PORTRAIT)

    def test_neither_defaults_to_square(self):
        self.assertEqual(resolve_generation_size(), (SQUARE, None))


class ResolveEditSizeTest(unittest.TestCase):
    def test_orientation_only(self):
        for o in ("square", "landscape", "portrait"):
            with self.subTest(o=o):
                size, requested = resolve_edit_size(orientation=o)
                self.assertEqual(size, ORIENTATIONS[o])
                self.assertIsNone(requested)

    def test_res_wh_coerces_like_generation(self):
        # gpt-image-2's edit endpoint honors arbitrary sizes, so --res on remix is
        # coerced the same way as on the generation path -- NOT snapped to one of the
        # three standard sizes. 1920x1080 -> 1920x1088 (rounded to a multiple of 16).
        size, requested = resolve_edit_size(res_wh=(1920, 1080))
        self.assertEqual((size, requested), ("1920x1088", "1920x1080"))
        self.assertEqual(size, coerce_generation_size(1920, 1080))
        self.assertNotEqual(size, requested)  # coerced -> notice fires

    def test_res_wh_ultrawide_is_kept_not_snapped(self):
        # The case that motivated this: an ultrawide --res is preserved (coerced only
        # to satisfy the /16 + ratio + box constraints), not collapsed to 1536x1024.
        size, requested = resolve_edit_size(res_wh=(1536, 640))
        self.assertEqual((size, requested), ("1536x640", "1536x640"))
        self.assertEqual(size, requested)  # already valid -> no notice

    def test_res_wh_already_valid_no_notice(self):
        size, requested = resolve_edit_size(res_wh=(1536, 1024))
        self.assertEqual((size, requested), ("1536x1024", "1536x1024"))
        self.assertEqual(size, requested)

    def test_res_wh_overrides_orientation(self):
        size, requested = resolve_edit_size(orientation="square", res_wh=(1080, 1920))
        self.assertEqual((size, requested), (coerce_generation_size(1080, 1920), "1080x1920"))
        self.assertNotEqual(size, SQUARE)

    def test_neither_matches_attachment_dimensions(self):
        # The no-flag default: match the input image's OWN dimensions as closely as a
        # valid edit size allows (coerce them), not bucket them to a standard size.
        # requested stays None so the default remix never posts a coercion notice.
        self.assertEqual(
            resolve_edit_size(width=1600, height=1200),
            (coerce_generation_size(1600, 1200), None),
        )
        self.assertEqual(resolve_edit_size(width=1600, height=1200)[0], "1600x1200")
        # 16:9 photo -> kept as a 16:9-ish size (1920x1088), not snapped to 1536x1024.
        self.assertEqual(resolve_edit_size(width=1920, height=1080)[0], "1920x1088")
        # An oddball size is nudged to the nearest valid one, still matching closely.
        self.assertEqual(resolve_edit_size(width=1327, height=842)[0],
                         coerce_generation_size(1327, 842))

    def test_neither_falls_back_to_auto_on_unusable_dimensions(self):
        # Discord reports Optional[int] width/height; None/0/negative/non-numeric all
        # mean "unknown", so the default resolves to AUTO (no notice).
        for w, h in [(None, None), (None, 1024), (1024, None), (0, 1024),
                     (1024, 0), (-100, 1024), ("abc", 1024)]:
            with self.subTest(w=w, h=h):
                self.assertEqual(resolve_edit_size(width=w, height=h), (AUTO, None))


if __name__ == "__main__":
    unittest.main()
