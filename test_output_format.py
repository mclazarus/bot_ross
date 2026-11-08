"""Tests for output_format.py's flag parsing.

Run: python -m unittest test_output_format
"""

import unittest

import output_format as of


class ParseOutputFlagsTest(unittest.TestCase):
    def test_no_flags_is_verbatim(self):
        text = "a  fox   in snow"
        out, flags = of.parse_output_flags(text)
        self.assertEqual(out, text)
        self.assertEqual(flags, of.OutputFlags())
        self.assertEqual(flags.kind, of.PNG)

    def test_none_passes_through(self):
        self.assertEqual(of.parse_output_flags(None), (None, of.OutputFlags()))

    def test_gif_with_defaults(self):
        out, flags = of.parse_output_flags("a fox --GIF leaping")
        self.assertEqual(out, "a fox leaping")
        self.assertEqual((flags.kind, flags.frames, flags.ms), (of.GIF, 9, 200))
        self.assertTrue(flags.gif_requested)

    def test_frames_and_ms_both_forms_imply_gif(self):
        out, flags = of.parse_output_flags("a fox --frames 4 --ms=500")
        self.assertEqual(out, "a fox")
        self.assertEqual((flags.kind, flags.frames, flags.ms), (of.GIF, 4, 500))
        _, flags = of.parse_output_flags("--frames=16 a fox")
        self.assertEqual(flags.frames, 16)

    def test_bounds(self):
        for bad in ("--frames 1", "--frames 17", "--ms 10", "--ms 2001", "--frames lots", "--ms"):
            with self.subTest(bad=bad), self.assertRaises(of.FlagError):
                of.parse_output_flags(f"a fox {bad}")
        _, flags = of.parse_output_flags("x --frames 2 --ms 2000")
        self.assertEqual((flags.frames, flags.ms), (2, 2000))

    def test_svg(self):
        out, flags = of.parse_output_flags("a lighthouse --svg")
        self.assertEqual(out, "a lighthouse")
        self.assertEqual(flags.kind, of.SVG)
        self.assertTrue(flags.svg_requested)

    def test_gif_and_svg_conflict(self):
        with self.assertRaises(of.FlagError) as cm:
            of.parse_output_flags("x --gif --svg")
        self.assertIn("not both", str(cm.exception))

    def test_boring_is_independent_of_format(self):
        out, flags = of.parse_output_flags("a fox --boring")
        self.assertEqual(out, "a fox")
        self.assertTrue(flags.boring)
        self.assertEqual(flags.kind, of.PNG)
        _, flags = of.parse_output_flags("a fox --boring --gif")
        self.assertTrue(flags.boring)
        self.assertEqual(flags.kind, of.GIF)

    def test_size_flags_are_left_alone(self):
        out, _ = of.parse_output_flags("a fox --landscape --res 1920x1080 --gif")
        self.assertEqual(out, "a fox --landscape --res 1920x1080")

    def test_extension(self):
        self.assertEqual([of.extension(k) for k in (of.PNG, of.GIF, of.SVG)], ["png", "gif", "svg"])


class GifFrameSizeTest(unittest.TestCase):
    def dims(self, size):
        return tuple(int(x) for x in size.split("x"))

    def assertFits(self, size, frames):
        w, h = self.dims(size)
        edge, px = of.gif_frame_limits(frames)
        self.assertEqual((w % 16, h % 16), (0, 0), size)
        self.assertLessEqual(max(w, h), edge, size)
        self.assertLessEqual(w * h, px, size)

    def test_defaults(self):
        self.assertEqual(of.gif_frame_size(9), "768x768")
        self.assertEqual(of.gif_frame_size(4), "768x768")
        self.assertEqual(of.gif_frame_size(16), "720x720")

    def test_orientations_and_source_shape(self):
        w, h = self.dims(of.gif_frame_size(9, "landscape"))
        self.assertGreater(w, h)
        w, h = self.dims(of.gif_frame_size(9, "portrait"))
        self.assertLess(w, h)
        w, h = self.dims(of.gif_frame_size(9, src_wh=(1920, 1080)))
        self.assertGreater(w, h)
        self.assertEqual(of.gif_frame_size(9, src_wh=(None, None)), "768x768")

    def test_everything_fits_the_measured_limits(self):
        cases = [dict(), dict(orientation="landscape"), dict(orientation="portrait"),
                 dict(res_wh=(3840, 2160)), dict(res_wh=(5000, 100)), dict(res_wh=(100, 5000)),
                 dict(res_wh=(1024, 1024)), dict(src_wh=(4000, 1000))]
        for frames in (2, 4, 9, 10, 16):
            for kw in cases:
                with self.subTest(frames=frames, **kw):
                    self.assertFits(of.gif_frame_size(frames, **kw), frames)

    def test_measured_working_sizes_are_inside_the_limits(self):
        # Sizes the live API accepted on 2026-11-07.
        for size, frames in (("960x960", 9), ("1152x640", 9), ("720x720", 16), ("960x960", 4)):
            self.assertFits(size, frames)


if __name__ == "__main__":
    unittest.main()
