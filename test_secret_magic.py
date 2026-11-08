"""Tests for secret_magic.py: the prompt log, slot rotation, the nightly gate, and the request.

Run: python -m unittest test_secret_magic
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import secret_magic as sm

NY = ZoneInfo("America/New_York")


class PromptLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "prompt_log.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_and_read_back_by_day(self):
        d = date(2026, 11, 7)
        sm.append_prompt(self.path, d, "2026-11-07T10:00:00-05:00", "kev", "paint", "a fox")
        sm.append_prompt(self.path, d, "2026-11-07T11:00:00-05:00", "kev", "remix", "  ")  # blank: ignored
        sm.append_prompt(self.path, d + timedelta(days=1), "x", "kev", "paint", "tomorrow")
        entries = sm.load_log(self.path)
        self.assertEqual(sm.prompts_for(entries, d), ["a fox"])
        self.assertEqual(sm.prompts_for(entries, d + timedelta(days=1)), ["tomorrow"])

    def test_old_days_are_pruned_on_append(self):
        old = date(2026, 10, 1)
        sm.append_prompt(self.path, old, "x", "kev", "paint", "ancient")
        today = old + timedelta(days=sm.LOG_KEEP_DAYS)
        sm.append_prompt(self.path, today, "x", "kev", "paint", "fresh")
        self.assertEqual([e["prompt"] for e in sm.load_log(self.path)], ["fresh"])

    def test_keeps_exactly_keep_days(self):
        today = date(2026, 11, 7)
        entries = [{"day": (today - timedelta(days=k)).isoformat(), "prompt": str(k)} for k in range(10)]
        kept = sm.prune_log(entries, today)
        self.assertEqual(len(kept), sm.LOG_KEEP_DAYS)

    def test_corrupt_log_reads_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(sm.load_log(self.path), [])


class StoreRotationTest(unittest.TestCase):
    def test_rotates_out_the_oldest_after_five(self):
        store = sm.load_store("/nonexistent/secret.json")
        self.assertEqual(store, {"slots": [], "last_run": None, "generated": 0})
        for k in range(7):
            store = sm.add_secret(store, f"gag {k}", f"2026-11-{k + 1:02d}T23:30:00-05:00", 5 + k)
        self.assertEqual([s["text"] for s in store["slots"]], [f"gag {k}" for k in range(2, 7)])
        self.assertEqual(store["generated"], 7)
        self.assertEqual(store["slots"][-1]["id"], "secret-20261107")

    def test_same_day_ids_stay_unique(self):
        store = sm.add_secret({"slots": []}, "a", "2026-11-07T23:30:00", 5)
        store = sm.add_secret(store, "b", "2026-11-07T23:31:00", 5)
        self.assertEqual([s["id"] for s in store["slots"]], ["secret-20261107", "secret-20261107-2"])

    def test_magic_entries_shape(self):
        store = sm.add_secret({"slots": []}, "a gag", "2026-11-07T23:30:00", 5)
        self.assertEqual(sm.magic_entries(store), [{"id": "secret-20261107", "text": "a gag"}])

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "secret.json")
            store = sm.add_secret(sm.load_store(path), "a gag", "2026-11-07T23:30:00", 6)
            store["last_run"] = "2026-11-07"
            sm.save_store(store, path)
            self.assertEqual(sm.load_store(path), store)


class IsDueTest(unittest.TestCase):
    def at(self, h, m, day=date(2026, 11, 7)):
        return datetime(day.year, day.month, day.day, h, m, tzinfo=NY).astimezone(timezone.utc)

    def test_due_inside_the_window_once_a_day(self):
        self.assertEqual(sm.is_due(self.at(23, 30), NY, None), (True, date(2026, 11, 7)))
        self.assertTrue(sm.is_due(self.at(23, 40), NY, None)[0])
        self.assertFalse(sm.is_due(self.at(23, 30), NY, "2026-11-07")[0])

    def test_not_due_before_or_long_after(self):
        self.assertFalse(sm.is_due(self.at(23, 29), NY, None)[0])
        self.assertFalse(sm.is_due(self.at(23, 41), NY, None)[0])
        self.assertFalse(sm.is_due(self.at(12, 0), NY, None)[0])

    def test_naive_now_is_refused(self):
        with self.assertRaises(ValueError):
            sm.is_due(datetime(2026, 11, 7, 23, 30), NY, None)


class RequestTest(unittest.TestCase):
    def test_messages_carry_examples_and_prompts(self):
        msgs = sm.build_messages(["a fox", "a capybara in a beanie"], ["In the background, a cat."])
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn("In the background, a cat.", msgs[1]["content"])
        self.assertIn("a capybara in a beanie", msgs[1]["content"])
        self.assertIn("Today's 2 prompts", msgs[1]["content"])

    def test_request_is_bounded(self):
        prompts = [("p%d " % k) * 300 for k in range(500)]
        user = sm.build_messages(prompts, [])[1]["content"]
        self.assertIn(f"Today's {sm.MAX_PROMPTS_IN_REQUEST} prompts", user)
        self.assertLess(len(user), sm.MAX_PROMPTS_IN_REQUEST * (sm.MAX_PROMPT_CHARS + 4) + 1000)

    def test_clean_mixin(self):
        self.assertEqual(sm.clean_mixin('  "In the background,\n a duck waves."  '),
                         "In the background, a duck waves.")
        self.assertEqual(sm.clean_mixin("Mixin: Off to the side, a goose."), "Off to the side, a goose.")
        self.assertIsNone(sm.clean_mixin(""))
        self.assertIsNone(sm.clean_mixin("x" * 401))
        self.assertIsNone(sm.clean_mixin(None))


if __name__ == "__main__":
    unittest.main()
