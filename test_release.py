"""Tests for release.py: the changelog parser, the shipped CHANGELOG.md, and the message text.

Run: python -m unittest test_release
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import release

SAMPLE = """# Changelog

Intro text, ignored.

## Build 2 · 2026-10-14
- Second thing.
  More about it.

## Build 1 · 2026-10-01 · 7a1fe80
- First thing.
"""


class ParseTest(unittest.TestCase):
    def test_parse_changelog(self):
        two, one = release.parse_changelog(SAMPLE)
        self.assertEqual((two.build, two.date, two.commit), (2, "2026-10-14", None))
        self.assertEqual(two.notes, "- Second thing.\n  More about it.")
        self.assertEqual((one.build, one.date, one.commit, one.notes),
                         (1, "2026-10-01", "7a1fe80", "- First thing."))


class ShippedChangelogTest(unittest.TestCase):
    def test_counts_up_and_records_commits(self):
        rs = release.releases()
        self.assertEqual([r.build for r in rs], list(range(len(rs), 0, -1)))
        self.assertTrue(all(r.notes for r in rs))
        # Every build before the current one has shipped, so its heading names its commit.
        self.assertTrue(all(r.commit for r in rs[1:]), [r.build for r in rs[1:] if not r.commit])
        self.assertIs(release.find(1), rs[-1])
        self.assertIsNone(release.find(999))

    def test_every_heading_parses(self):
        # A typo'd heading (wrong separator, bad date) would silently swallow a build's notes
        # into the previous build -- catch it by counting raw headings.
        text = (release.HERE / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = [l for l in text.splitlines() if l.startswith("## ")]
        self.assertEqual(len(headings), len(release.releases()))
        for h in headings:
            self.assertRegex(h, release.HEADING)


class CommitTest(unittest.TestCase):
    def setUp(self):
        release.commit.cache_clear()

    def tearDown(self):
        release.commit.cache_clear()

    def test_prefers_the_git_archive_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_commit.txt").write_text("abc1234\n")
            with mock.patch.object(release, "HERE", Path(d)):
                self.assertEqual(release.commit(), "abc1234")

    def test_unstamped_placeholder_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_commit.txt").write_text("$Format:%h$\n")
            with mock.patch.object(release, "HERE", Path(d)), \
                    mock.patch.object(release.subprocess, "run", side_effect=OSError):
                self.assertIsNone(release.commit())

    def test_shipped_file_is_placeholder_or_stamp(self):
        # A working tree has the export-subst placeholder; a `git archive` build (how
        # home-docks-infra builds the image) has the real short hash stamped in.
        text = (release.HERE / "_commit.txt").read_text().strip()
        self.assertRegex(text, r"^(\$Format:%h\$|[0-9a-f]{7,40})$")


class MessagesTest(unittest.TestCase):
    def test_version_and_greeting(self):
        two, _ = release.parse_changelog(SAMPLE)
        self.assertIn("**Build 2**", release.version_message(two, None))
        self.assertIn("`dev`", release.version_message(two, None))
        g = release.greeting_message(two, "abc1234")
        self.assertTrue(g.startswith("A wild Bot Ross appears!"))
        self.assertIn("`abc1234`", g)
        self.assertIn("&changelog", g)

    def test_changelog_message(self):
        two, one = release.parse_changelog(SAMPLE)
        text = release.changelog_message(one, one.commit, [2, 1])
        self.assertIn("`7a1fe80`", text)
        self.assertIn("• First thing.", text)
        self.assertIn("Earlier builds: `&changelog <build>` · 2", text)
        text = release.changelog_message(two, None, [2])
        self.assertTrue(text.endswith("• Second thing. More about it."))

    def test_unknown_build(self):
        self.assertIn("Builds on file: 2, 1", release.unknown_build("9", [2, 1]))


if __name__ == "__main__":
    unittest.main()
