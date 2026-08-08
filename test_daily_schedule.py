"""Unit tests for daily_schedule.py: the daily-image-of-the-day scheduler's pure
DST/UTC time resolution, schedule validation, and retention-pruning logic.

This is the heart of the daily-image feature: the container runs UTC while the
schedule is wall-clock in a configurable bot timezone, so DST gaps/folds and
UTC/local day confusion are the dominant risk, and are exhaustively covered here
rather than eyeballed against a running bot. Mirrors test_macros.py's structure
and its habit of explanatory comments justifying every non-obvious bound.

2026 facts used throughout (asserted, not just assumed):
  - US spring-forward: Sun 2026-03-08, 02:00 -> 03:00 (America/New_York).
  - US fall-back:       Sun 2026-11-01, 02:00 -> 01:00 (America/New_York).
  - London spring-forward: Sun 2026-03-29, 01:00 -> 02:00 (Europe/London).
  - Sydney spring-forward (Southern Hemisphere, October): Sun 2026-10-04, 02:00 -> 03:00.
  - Sydney fall-back (Southern Hemisphere, April):        Sun 2026-04-05, 03:00 -> 02:00.
  - 2026-08-07 is a Friday.

Run from the repo root:  python -m unittest test_daily_schedule -v
"""

import copy
import os
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import release_image
from daily_schedule import (
    DAILY_IMAGE_RETENTION,
    MISS_WINDOW,
    MONTHS,
    WEEKDAYS,
    classify_slot_time,
    daily_image_filename,
    due_slots,
    format_announcement_date,
    get_zone,
    load_schedule,
    load_state,
    mark_fired,
    parse_bool,
    parse_channel_id,
    parse_daily_image_date,
    parse_slot_time,
    render_message,
    save_schedule,
    save_state,
    seconds_to_next_minute,
    seed_schedule,
    seed_source_for,
    select_images_to_prune,
    slot_instant,
    validate_schedule,
)

SEED_SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_schedule.json")

# Zones exercised throughout: NY has US DST rules, LON has EU rules on a
# different date, KOL has no DST at all (a fixed +5:30 offset), SYD is in the
# Southern Hemisphere so its "spring forward"/"fall back" fall on the opposite
# months from the Northern Hemisphere zones.
NY = ZoneInfo("America/New_York")
LON = ZoneInfo("Europe/London")
KOL = ZoneInfo("Asia/Kolkata")
SYD = ZoneInfo("Australia/Sydney")
UTC = timezone.utc


def _ny_offset(hours):
    """A fixed UTC offset timezone, used to build `now` values in "wall clock
    at a known offset" form the same way a hand-written test datetime would."""
    return timezone(timedelta(hours=hours))


class SlotInstantTest(unittest.TestCase):
    def test_summer_edt_offset(self):
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        self.assertEqual((instant.hour, instant.minute), (7, 0))
        self.assertEqual(instant.utcoffset(), timedelta(hours=-4))
        self.assertEqual(instant, datetime(2026, 8, 7, 11, 0, tzinfo=UTC))

    def test_winter_est_offset_same_wall_time_different_offset(self):
        # Same 07:00 wall time as the summer case, but EST (-5) instead of
        # EDT (-4) -- proves slot_instant isn't hardcoding an offset.
        instant = slot_instant(date(2026, 1, 7), 7, 0, NY)
        self.assertEqual((instant.hour, instant.minute), (7, 0))
        self.assertEqual(instant.utcoffset(), timedelta(hours=-5))
        self.assertEqual(instant, datetime(2026, 1, 7, 12, 0, tzinfo=UTC))

    def test_spring_forward_gap_pushes_forward(self):
        # 02:00 doesn't exist on 2026-03-08 (clocks jump 02:00 -> 03:00) --
        # the slot is normalized forward to 03:00 EDT, not silently dropped.
        instant = slot_instant(date(2026, 3, 8), 2, 0, NY)
        self.assertEqual((instant.hour, instant.minute), (3, 0))
        self.assertEqual(instant.utcoffset(), timedelta(hours=-4))
        self.assertEqual(instant, datetime(2026, 3, 8, 7, 0, tzinfo=UTC))

    def test_spring_forward_gap_interior_time(self):
        instant = slot_instant(date(2026, 3, 8), 2, 30, NY)
        self.assertEqual((instant.hour, instant.minute), (3, 30))
        self.assertEqual(instant, datetime(2026, 3, 8, 7, 30, tzinfo=UTC))

    def test_spring_forward_gap_edges_untouched(self):
        # 01:59 (just before the gap) and 03:00 (just after) are both normal,
        # unshifted wall times.
        before = slot_instant(date(2026, 3, 8), 1, 59, NY)
        self.assertEqual((before.hour, before.minute), (1, 59))
        self.assertEqual(before.utcoffset(), timedelta(hours=-5))

        after = slot_instant(date(2026, 3, 8), 3, 0, NY)
        self.assertEqual((after.hour, after.minute), (3, 0))
        self.assertEqual(after.utcoffset(), timedelta(hours=-4))

    def test_fall_back_fold_resolves_to_first_occurrence(self):
        # 01:30 happens twice on 2026-11-01 (EDT then EST). fold=0 means the
        # FIRST (EDT, -4) occurrence, not the second.
        instant = slot_instant(date(2026, 11, 1), 1, 30, NY)
        self.assertEqual(instant.utcoffset(), timedelta(hours=-4))
        # Compared via a same-tzinfo conversion rather than a bare ==: this
        # interpreter's datetime/zoneinfo pairing has a known quirk where a
        # fold=0 (ambiguous-time) instant compared directly against a
        # different-tzinfo datetime can spuriously report unequal even
        # though .timestamp()/subtraction/ordering all agree they're the
        # same instant -- converting both sides to UTC first sidesteps it.
        self.assertEqual(instant.astimezone(UTC), datetime(2026, 11, 1, 5, 30, tzinfo=UTC))

    def test_fall_back_fold_edges(self):
        before = slot_instant(date(2026, 11, 1), 0, 59, NY)
        self.assertEqual(before.utcoffset(), timedelta(hours=-4))
        after = slot_instant(date(2026, 11, 1), 2, 0, NY)
        self.assertEqual(after.utcoffset(), timedelta(hours=-5))

    def test_non_us_gap_london(self):
        # London's spring-forward is a different date than NY's (2026-03-29
        # vs 2026-03-08), proving the gap logic isn't NY-specific.
        instant = slot_instant(date(2026, 3, 29), 1, 30, LON)
        self.assertEqual((instant.hour, instant.minute), (2, 30))
        self.assertEqual(instant.utcoffset(), timedelta(hours=1))
        self.assertEqual(instant, datetime(2026, 3, 29, 1, 30, tzinfo=UTC))

    def test_no_dst_zone_kolkata_constant_offset(self):
        # Kolkata never observes DST -- the offset must be identical on a
        # summer-in-NY date and a winter-in-NY date.
        summer = slot_instant(date(2026, 8, 7), 7, 0, KOL)
        winter = slot_instant(date(2026, 1, 7), 7, 0, KOL)
        self.assertEqual(summer.utcoffset(), timedelta(hours=5, minutes=30))
        self.assertEqual(winter.utcoffset(), timedelta(hours=5, minutes=30))

    def test_southern_hemisphere_sydney(self):
        # Sydney's gap is in October (not March, unlike NY/London).
        gap = slot_instant(date(2026, 10, 4), 2, 30, SYD)
        self.assertEqual((gap.hour, gap.minute), (3, 30))
        self.assertEqual(gap.utcoffset(), timedelta(hours=11))

        # Sydney's fold is in April; fold=0 -> the FIRST occurrence, AEDT (+11).
        fold = slot_instant(date(2026, 4, 5), 2, 30, SYD)
        self.assertEqual(fold.utcoffset(), timedelta(hours=11))

    def test_result_always_aware(self):
        cases = [
            (date(2026, 8, 7), 7, 0, NY),
            (date(2026, 1, 7), 7, 0, NY),
            (date(2026, 3, 8), 2, 0, NY),
            (date(2026, 3, 8), 2, 30, NY),
            (date(2026, 11, 1), 1, 30, NY),
            (date(2026, 3, 29), 1, 30, LON),
            (date(2026, 8, 7), 7, 0, KOL),
            (date(2026, 10, 4), 2, 30, SYD),
            (date(2026, 4, 5), 2, 30, SYD),
        ]
        for day, hour, minute, zone in cases:
            with self.subTest(day=day, hour=hour, minute=minute, zone=str(zone)):
                instant = slot_instant(day, hour, minute, zone)
                self.assertIsNotNone(instant.tzinfo)
                self.assertIsNotNone(instant.utcoffset())


class ClassifySlotTimeTest(unittest.TestCase):
    def test_nonexistent_gap(self):
        self.assertEqual(classify_slot_time(date(2026, 3, 8), 2, 30, NY), "nonexistent")

    def test_ambiguous_fold(self):
        self.assertEqual(classify_slot_time(date(2026, 11, 1), 1, 30, NY), "ambiguous")

    def test_normal_times(self):
        self.assertEqual(classify_slot_time(date(2026, 8, 7), 12, 0, NY), "normal")
        # The edges of the gap day are themselves ordinary, unaffected times.
        self.assertEqual(classify_slot_time(date(2026, 3, 8), 1, 59, NY), "normal")
        self.assertEqual(classify_slot_time(date(2026, 3, 8), 3, 0, NY), "normal")

    def test_sydney_mirror(self):
        self.assertEqual(classify_slot_time(date(2026, 10, 4), 2, 30, SYD), "nonexistent")
        self.assertEqual(classify_slot_time(date(2026, 4, 5), 2, 30, SYD), "ambiguous")


# Shared fixtures for DueSlotsTest -- minimal valid "generate" entries (type
# doesn't affect due_slots' own logic; edit-vs-generate ordering is exercised
# with explicit type="edit" entries below where the spec calls for it).
SLOT_0700 = {"id": "morning", "time": "07:00", "type": "generate", "message": "m"}
SLOT_1200 = {"id": "lunch", "time": "12:00", "type": "generate", "message": "m"}


class DueSlotsTest(unittest.TestCase):
    def test_exact_hit(self):
        now = datetime(2026, 8, 7, 7, 0, 0, tzinfo=_ny_offset(-4))
        self.assertEqual(due_slots(now, [SLOT_0700], {}, NY), [(SLOT_0700, date(2026, 8, 7))])

    def test_window_interior(self):
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        now = instant + timedelta(minutes=9, seconds=59)
        self.assertEqual(due_slots(now, [SLOT_0700], {}, NY), [(SLOT_0700, date(2026, 8, 7))])

    def test_window_edge_inclusive(self):
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        now = instant + MISS_WINDOW
        self.assertEqual(due_slots(now, [SLOT_0700], {}, NY), [(SLOT_0700, date(2026, 8, 7))])

    def test_one_second_past_window_not_due(self):
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        now = instant + MISS_WINDOW + timedelta(seconds=1)
        self.assertEqual(due_slots(now, [SLOT_0700], {}, NY), [])

    def test_one_second_early_not_due(self):
        # A slot never fires before its instant, even by one second.
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        now = instant - timedelta(seconds=1)
        self.assertEqual(due_slots(now, [SLOT_0700], {}, NY), [])

    def test_no_catch_up(self):
        # Two hours late is lost forever, by design, even with empty state.
        now = datetime(2026, 8, 7, 14, 0, tzinfo=_ny_offset(-4))
        self.assertEqual(due_slots(now, [SLOT_1200], {}, NY), [])

    def test_already_fired_today(self):
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        state = {"morning": "2026-08-07"}
        self.assertEqual(due_slots(instant, [SLOT_0700], state, NY), [])

    def test_fired_yesterday_only_does_not_block_today(self):
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        state = {"morning": "2026-08-06"}
        self.assertEqual(due_slots(instant, [SLOT_0700], state, NY), [(SLOT_0700, date(2026, 8, 7))])

    def test_naive_now_raises(self):
        # A naive `now` would silently be treated as local time by
        # astimezone() and shift every slot -- the exact bug this module
        # exists to prevent, so it must fail loudly instead.
        with self.assertRaises(ValueError) as cm:
            due_slots(datetime(2026, 8, 7, 7, 0), [SLOT_0700], {}, NY)
        self.assertIn("naive", str(cm.exception).lower())

    def test_utc_container_local_day_behind_utc_day(self):
        # now = 2026-08-08T00:30Z = 20:30 EDT on the 7th. A 20:25 slot is due
        # for the LOCAL date (2026-08-07), not the UTC date (2026-08-08).
        slot = {"id": "evening", "time": "20:25", "type": "generate", "message": "m"}
        now = datetime(2026, 8, 8, 0, 30, tzinfo=UTC)
        self.assertEqual(due_slots(now, [slot], {}, NY), [(slot, date(2026, 8, 7))])

    def test_same_instant_two_representations_identical_result(self):
        slot = {"id": "evening", "time": "20:25", "type": "generate", "message": "m"}
        now_utc = datetime(2026, 8, 8, 0, 30, tzinfo=UTC)
        self.assertEqual(
            due_slots(now_utc, [slot], {}, NY),
            due_slots(now_utc.astimezone(NY), [slot], {}, NY),
        )

    def test_midnight_straddle_yesterdays_slot(self):
        # now = 2026-08-08T04:03Z = local 00:03 on the 8th. A 23:58 slot is
        # due for YESTERDAY's day key (2026-08-07).
        slot = {"id": "late", "time": "23:58", "type": "generate", "message": "m"}
        now = datetime(2026, 8, 8, 4, 3, tzinfo=UTC)
        self.assertEqual(due_slots(now, [slot], {}, NY), [(slot, date(2026, 8, 7))])

    def test_midnight_straddle_window_edge(self):
        slot = {"id": "late", "time": "23:58", "type": "generate", "message": "m"}
        at_edge = datetime(2026, 8, 8, 4, 8, 0, tzinfo=UTC)
        self.assertEqual(due_slots(at_edge, [slot], {}, NY), [(slot, date(2026, 8, 7))])
        past_edge = datetime(2026, 8, 8, 4, 8, 1, tzinfo=UTC)
        self.assertEqual(due_slots(past_edge, [slot], {}, NY), [])

    def test_spring_forward_firing(self):
        slot = {"id": "gap", "time": "02:00", "type": "generate", "message": "m"}
        # The normalized instant (03:00 EDT, since 02:00 doesn't exist).
        at_instant = datetime(2026, 3, 8, 3, 0, tzinfo=_ny_offset(-4))
        self.assertEqual(due_slots(at_instant, [slot], {}, NY), [(slot, date(2026, 3, 8))])
        at_edge = datetime(2026, 3, 8, 3, 10, tzinfo=_ny_offset(-4))
        self.assertEqual(due_slots(at_edge, [slot], {}, NY), [(slot, date(2026, 3, 8))])
        past_edge = datetime(2026, 3, 8, 3, 10, 1, tzinfo=_ny_offset(-4))
        self.assertEqual(due_slots(past_edge, [slot], {}, NY), [])
        # Before the normalized instant (still pre-transition EST) -- not due.
        before = datetime(2026, 3, 8, 1, 59, tzinfo=_ny_offset(-5))
        self.assertEqual(due_slots(before, [slot], {}, NY), [])

    def test_fall_back_single_fire_three_ways(self):
        slot = {"id": "fall", "time": "01:30", "type": "generate", "message": "m"}
        # (a) The first 01:30 (EDT) is due against empty state.
        first_occurrence = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        self.assertEqual(due_slots(first_occurrence, [slot], {}, NY), [(slot, date(2026, 11, 1))])

        # (b) Once marked fired, the same instant is no longer due.
        state = mark_fired({}, "fall", date(2026, 11, 1))
        self.assertEqual(due_slots(first_occurrence, [slot], state, NY), [])

        # (c) Even with EMPTY state, the second 01:30 (an hour later in UTC
        # terms) is not due -- the MISS_WINDOW guard alone prevents the
        # double-fire, independent of whether the state file survived.
        second_occurrence = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        self.assertEqual(second_occurrence - first_occurrence, timedelta(hours=1))
        self.assertGreater(second_occurrence - first_occurrence, MISS_WINDOW)
        self.assertEqual(due_slots(second_occurrence, [slot], {}, NY), [])

    def test_fall_back_single_fire_now_expressed_in_bot_zone(self):
        # Same property as (c) above, but written against a `now` that is
        # ALREADY expressed in `zone` rather than UTC -- the docstring/contract
        # explicitly permits "an aware datetime in any timezone", including
        # the bot's own zone. This is NOT redundant with (c): per PEP 495,
        # subtracting a same-zone-attached `now` from a same-zone-attached
        # `instant` is an INTRA-zone subtraction, which ignores `fold`
        # entirely. If due_slots ever subtracted `now` from `instant` without
        # first normalizing `now` to a fixed offset (e.g. UTC), this exact
        # call would wrongly compute delta 0 for the second occurrence and
        # double-fire -- passing (c) above the whole time, since that variant
        # only ever exercises UTC-represented `now` values. Regressed once;
        # see the due_slots UTC-normalization comment for the fix.
        slot = {"id": "fall", "time": "01:30", "type": "generate", "message": "m"}
        second_occurrence_utc = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        second_occurrence_ny = second_occurrence_utc.astimezone(NY)
        self.assertEqual(due_slots(second_occurrence_ny, [slot], {}, NY), [])

    def test_kolkata_mirror_local_day_ahead_of_utc_day(self):
        # now = 2026-08-07T20:00Z = 01:30 IST on the 8th. A 01:25 slot is due
        # for the LOCAL date (2026-08-08), which is AHEAD of the UTC date.
        slot = {"id": "kol", "time": "01:25", "type": "generate", "message": "m"}
        now = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)
        self.assertEqual(due_slots(now, [slot], {}, KOL), [(slot, date(2026, 8, 8))])

    def test_ordering_generate_before_edit_regardless_of_list_order(self):
        edit_slot = {"id": "edit_slot", "time": "07:05", "type": "edit",
                     "edit_prompt": "e", "message": "m"}
        now = datetime(2026, 8, 7, 7, 6, tzinfo=_ny_offset(-4))
        result = due_slots(now, [edit_slot, SLOT_0700], {}, NY)
        self.assertEqual([entry["id"] for entry, _day in result], ["morning", "edit_slot"])

    def test_ordering_tie_keeps_entries_order(self):
        a = {"id": "a", "time": "07:00", "type": "generate", "message": "m"}
        b = {"id": "b", "time": "07:00", "type": "generate", "message": "m"}
        now = datetime(2026, 8, 7, 7, 0, tzinfo=_ny_offset(-4))
        result = due_slots(now, [a, b], {}, NY)
        self.assertEqual([entry["id"] for entry, _day in result], ["a", "b"])

    def test_defensive_skip_of_invalid_entry(self):
        bad = {"id": "bad", "time": "7am", "type": "generate", "message": "x"}
        now = datetime(2026, 8, 7, 7, 0, tzinfo=_ny_offset(-4))
        result = due_slots(now, [bad, SLOT_0700], {}, NY)
        self.assertEqual([entry["id"] for entry, _day in result], ["morning"])

    def test_purity_does_not_mutate_entries_or_state(self):
        entries = [copy.deepcopy(SLOT_0700)]
        state = {}
        entries_before = copy.deepcopy(entries)
        state_before = copy.deepcopy(state)
        now = datetime(2026, 8, 7, 7, 0, 0, tzinfo=_ny_offset(-4))
        result = due_slots(now, entries, state, NY)
        self.assertEqual(entries, entries_before)
        self.assertEqual(state, state_before)
        # By-reference contract: the returned entry IS the input dict.
        self.assertIs(result[0][0], entries[0])

    def test_state_keys_are_independent_per_entry(self):
        state = {"lunch": "2026-08-07"}
        now = datetime(2026, 8, 7, 7, 0, 0, tzinfo=_ny_offset(-4))
        self.assertEqual(due_slots(now, [SLOT_0700], state, NY), [(SLOT_0700, date(2026, 8, 7))])


class MarkFiredTest(unittest.TestCase):
    def test_adds_new_key(self):
        original = {}
        result = mark_fired(original, "morning", date(2026, 8, 7))
        self.assertEqual(result, {"morning": "2026-08-07"})
        self.assertEqual(original, {})  # input untouched

    def test_overwrite_existing_key(self):
        original = {"morning": "2026-08-06"}
        result = mark_fired(original, "morning", date(2026, 8, 7))
        self.assertEqual(result, {"morning": "2026-08-07"})
        self.assertEqual(original, {"morning": "2026-08-06"})  # input untouched

    def test_preserves_sibling_keys_and_returns_new_object(self):
        original = {"lunch": "2026-08-07"}
        result = mark_fired(original, "morning", date(2026, 8, 7))
        self.assertEqual(result, {"lunch": "2026-08-07", "morning": "2026-08-07"})
        self.assertIsNot(result, original)


class SecondsToNextMinuteTest(unittest.TestCase):
    def test_at_boundary_is_a_full_minute(self):
        # At exactly :00.000000, sleeping a full 60s (not 0) avoids a busy loop.
        self.assertEqual(seconds_to_next_minute(datetime(2026, 8, 7, 7, 0, 0, 0)), 60.0)

    def test_mid_minute(self):
        self.assertEqual(seconds_to_next_minute(datetime(2026, 8, 7, 7, 0, 30, 0)), 30.0)

    def test_near_boundary_clamps_up_to_one_second(self):
        # Raw value here is 0.5s; clamping up to 1.0 prevents a near-zero
        # sleep from busy-looping / double-ticking the same minute.
        self.assertEqual(seconds_to_next_minute(datetime(2026, 8, 7, 7, 0, 59, 500000)), 1.0)

    def test_just_under_boundary_clamps_up(self):
        self.assertEqual(seconds_to_next_minute(datetime(2026, 8, 7, 7, 0, 59, 999999)), 1.0)

    def test_randomized_sweep_stays_in_bounds(self):
        rng = random.Random(20260807)  # seeded for reproducibility
        for _ in range(500):
            second = rng.randint(0, 59)
            microsecond = rng.randint(0, 999999)
            value = seconds_to_next_minute(datetime(2026, 8, 7, 7, 0, second, microsecond))
            self.assertGreaterEqual(value, 1.0)
            self.assertLessEqual(value, 60.0)

    def test_aware_input_accepted_and_matches_naive_twin(self):
        naive = datetime(2026, 8, 7, 7, 0, 30, 0)
        aware = datetime(2026, 8, 7, 7, 0, 30, 0, tzinfo=NY)
        self.assertEqual(seconds_to_next_minute(naive), seconds_to_next_minute(aware))


class SeedSourceTest(unittest.TestCase):
    def test_basic_format(self):
        self.assertEqual(seed_source_for(date(2026, 8, 7)), "2026-08-07")

    def test_zero_padding(self):
        # An unpadded variant would permanently change every future daily prompt.
        self.assertEqual(seed_source_for(date(2026, 1, 2)), "2026-01-02")

    def test_cross_module_determinism(self):
        source = seed_source_for(date(2026, 8, 7))
        results = {release_image.build_release_prompt(source) for _ in range(50)}
        self.assertEqual(len(results), 1)

    def test_cross_module_distinctness_for_adjacent_days(self):
        # Adjacent days must get different prompts with overwhelming
        # probability; if this specific pair ever collided, the test data
        # would need a new pair -- there is no reason to expect a collision.
        today = release_image.build_release_prompt(seed_source_for(date(2026, 8, 7)))
        tomorrow = release_image.build_release_prompt(seed_source_for(date(2026, 8, 8)))
        self.assertNotEqual(today, tomorrow)


class FormatAnnouncementDateTest(unittest.TestCase):
    def test_single_digit_day_not_zero_padded(self):
        self.assertEqual(format_announcement_date(date(2026, 8, 7)), "Friday, August 7, 2026")

    def test_leap_day(self):
        self.assertEqual(format_announcement_date(date(2028, 2, 29)), "Tuesday, February 29, 2028")

    def test_january(self):
        self.assertEqual(format_announcement_date(date(2026, 1, 2)), "Friday, January 2, 2026")

    def test_weekday_mapping_sweep(self):
        # Seven consecutive dates starting Monday 2026-08-03 must render the
        # WEEKDAYS tuple in order -- pins date.weekday() indexing. Because the
        # names come from a literal tuple (never strftime("%A")), this also
        # doubles as the locale-independence assertion: no strftime output
        # under a non-English locale could satisfy this exact sequence.
        start = date(2026, 8, 3)
        for offset, expected_weekday in enumerate(WEEKDAYS):
            day = start + timedelta(days=offset)
            rendered = format_announcement_date(day)
            self.assertTrue(rendered.startswith(expected_weekday + ","), rendered)


class RenderMessageTest(unittest.TestCase):
    def test_substitutes_date_placeholder(self):
        result = render_message("It's the image of the day for {date}", date(2026, 8, 7))
        self.assertEqual(result, "It's the image of the day for Friday, August 7, 2026")

    def test_no_placeholder_passes_through(self):
        self.assertEqual(render_message("Lunch break!", date(2026, 8, 7)), "Lunch break!")

    def test_hostile_braces_are_inert(self):
        # This is WHY render_message uses str.replace and not str.format: a
        # hand-edited message with a stray '{'/'}' or an unrelated '{typo}'
        # would make str.format raise every single day at slot time.
        template = "100% {of} the {date} } {"
        expected = "100% {of} the Friday, August 7, 2026 } {"
        self.assertEqual(render_message(template, date(2026, 8, 7)), expected)


class ParseSlotTimeTest(unittest.TestCase):
    def test_valid_forms(self):
        self.assertEqual(parse_slot_time("07:00"), (7, 0))
        self.assertEqual(parse_slot_time("23:59"), (23, 59))
        self.assertEqual(parse_slot_time("00:00"), (0, 0))
        self.assertEqual(parse_slot_time("7:00"), (7, 0))  # single-digit hour tolerated
        self.assertEqual(parse_slot_time(" 07:00 "), (7, 0))  # surrounding whitespace stripped

    def test_invalid_values_raise(self):
        for bad in ("7am", "25:00", "12:60", "07:5", "0700", "7", "", "24:00", "-1:30", None, 700):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_slot_time(bad)


class ValidateScheduleTest(unittest.TestCase):
    def test_seed_content_all_valid(self):
        entries = load_schedule(SEED_SCHEDULE_FILE)
        good, errors = validate_schedule(entries)
        self.assertEqual(good, entries)
        self.assertEqual(errors, [])

    def test_bad_time_dropped_and_reported(self):
        for bad_time in ("25:00", "7am", "12:60"):
            with self.subTest(bad_time=bad_time):
                good_entry = {"id": "ok", "time": "07:00", "type": "generate", "message": "m"}
                bad_entry = {"id": "x", "time": bad_time, "type": "generate", "message": "m"}
                good, errors = validate_schedule([good_entry, bad_entry])
                self.assertEqual(good, [good_entry])
                self.assertEqual(len(errors), 1)
                self.assertIn("x", errors[0])

    def test_unknown_type_dropped(self):
        entry = {"id": "x", "time": "07:00", "type": "paint", "message": "m"}
        good, errors = validate_schedule([entry])
        self.assertEqual(good, [])
        self.assertEqual(len(errors), 1)

    def test_blank_whitespace_missing_and_non_string_id_dropped(self):
        base = {"time": "07:00", "type": "generate", "message": "m"}
        cases = [
            dict(base, id=""),
            dict(base, id="  "),
            dict(base),  # missing id entirely
            dict(base, id=42),
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                good, errors = validate_schedule([entry])
                self.assertEqual(good, [])
                self.assertEqual(len(errors), 1)

    def test_duplicate_id_first_kept_second_dropped(self):
        first = {"id": "lunch", "time": "12:00", "type": "generate", "message": "first"}
        second = {"id": "lunch", "time": "13:00", "type": "generate", "message": "second"}
        good, errors = validate_schedule([first, second])
        self.assertEqual(good, [first])
        self.assertEqual(len(errors), 1)
        self.assertIn("lunch", errors[0])

    def test_edit_without_edit_prompt_dropped(self):
        for edit_prompt in (None, ""):
            entry = {"id": "e", "time": "07:00", "type": "edit", "message": "m"}
            if edit_prompt is not None:
                entry["edit_prompt"] = edit_prompt
            with self.subTest(edit_prompt=edit_prompt):
                good, errors = validate_schedule([entry])
                self.assertEqual(good, [])
                self.assertEqual(len(errors), 1)

    def test_missing_or_blank_message_dropped_both_types(self):
        for entry_type, extra in (("generate", {}), ("edit", {"edit_prompt": "p"})):
            for message in (None, ""):
                entry = {"id": "e", "time": "07:00", "type": entry_type}
                entry.update(extra)
                if message is not None:
                    entry["message"] = message
                with self.subTest(entry_type=entry_type, message=message):
                    good, errors = validate_schedule([entry])
                    self.assertEqual(good, [])
                    self.assertEqual(len(errors), 1)

    def test_magic_must_be_a_real_bool(self):
        stringy = {"id": "e", "time": "07:00", "type": "generate", "message": "m", "magic": "true"}
        good, errors = validate_schedule([stringy])
        self.assertEqual(good, [])
        self.assertEqual(len(errors), 1)

        absent = {"id": "e", "time": "07:00", "type": "generate", "message": "m"}
        good, errors = validate_schedule([absent])
        self.assertEqual(good, [absent])
        self.assertEqual(errors, [])

    def test_non_dict_entry_dropped_neighbors_survive(self):
        # The property under test is specifically that an entry AFTER a bad
        # (non-dict) row survives -- i.e. the loop uses `continue`, not
        # `break`/an early `return [], [...]`, when it hits a bad row. Giving
        # the trailing entry a distinct id (not a duplicate of good_entry's)
        # is essential: with a duplicate id, a regression to "bail out on the
        # first non-dict row" would still leave len(good) == 1 by accident
        # (the duplicate would have been dropped anyway), silently passing
        # this test without ever exercising the neighbor-survival property.
        good_entry = {"id": "ok", "time": "07:00", "type": "generate", "message": "m"}
        after = {"id": "ok2", "time": "08:00", "type": "generate", "message": "m2"}
        good, errors = validate_schedule([good_entry, "hello", after])
        self.assertEqual(good, [good_entry, after])
        self.assertEqual(len(errors), 1)

        # Separately, cover the duplicate-id-after-a-non-dict-row case (this
        # is what the original version of this test actually measured): the
        # non-dict row contributes one error, and the id-duplicate of
        # good_entry contributes a second, independent error.
        good, errors = validate_schedule([good_entry, "hello", good_entry.copy()])
        self.assertEqual(good, [good_entry])
        self.assertEqual(len(errors), 2)

    def test_not_a_list_input(self):
        # A hand-corrupted schedule (a dict at the top level, or a missing
        # file that loaded as None) must not crash validate_schedule.
        good, errors = validate_schedule({"id": "x"})
        self.assertEqual(good, [])
        self.assertEqual(len(errors), 1)

        good, errors = validate_schedule(None)
        self.assertEqual(good, [])
        self.assertEqual(len(errors), 1)

    def test_tolerances_stray_edit_prompt_and_unknown_key(self):
        with_stray_edit_prompt = {
            "id": "g", "time": "07:00", "type": "generate", "message": "m", "edit_prompt": "ignored",
        }
        with_unknown_key = {
            "id": "u", "time": "07:00", "type": "generate", "message": "m", "color": "blue",
        }
        good, errors = validate_schedule([with_stray_edit_prompt, with_unknown_key])
        self.assertEqual(good, [with_stray_edit_prompt, with_unknown_key])
        self.assertEqual(errors, [])


class ScheduleIoTest(unittest.TestCase):
    def test_save_load_round_trip_preserves_unicode(self):
        entries = [{"id": "brush", "text": "🖌️ magic,"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            save_schedule(entries, path)
            self.assertEqual(load_schedule(path), entries)
            with open(path, encoding="utf-8") as f:
                self.assertIn("🖌️", f.read())

    def test_load_missing_file_fails_open(self):
        self.assertEqual(load_schedule("/no/such/daily_schedule.json"), [])

    def test_load_malformed_json_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            with open(path, "w") as f:
                f.write("{ not valid json")
            self.assertEqual(load_schedule(path), [])

    def test_seed_copies_when_absent_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            working = os.path.join(d, "daily_schedule.json")
            seed_schedule(working, SEED_SCHEDULE_FILE)
            self.assertEqual(load_schedule(working), load_schedule(SEED_SCHEDULE_FILE))

            # User edits survive a second seed call -- the property that
            # makes hand edits to data/daily_schedule.json survive redeploys.
            modified = [{"id": "custom", "time": "09:00", "type": "generate", "message": "hi"}]
            save_schedule(modified, working)
            seed_schedule(working, SEED_SCHEDULE_FILE)
            self.assertEqual(load_schedule(working), modified)


class StateIoTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_state.json")
            save_state({"morning": "2026-08-07"}, path)
            self.assertEqual(load_state(path), {"morning": "2026-08-07"})

    def test_missing_file_fails_open(self):
        self.assertEqual(load_state("/no/such/daily_state.json"), {})

    def test_malformed_json_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_state.json")
            with open(path, "w") as f:
                f.write("{ nope")
            # A corrupt write must never wedge the scheduler forever -- the
            # worst case after failing open to {} is one slot re-firing once
            # inside its 10-minute MISS_WINDOW, which is an acceptable cost.
            self.assertEqual(load_state(path), {})

    def test_json_list_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_state.json")
            with open(path, "w") as f:
                f.write("[1,2]")
            self.assertEqual(load_state(path), {})

    def test_json_string_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_state.json")
            with open(path, "w") as f:
                f.write('"hi"')
            self.assertEqual(load_state(path), {})

    def test_valid_empty_object_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_state.json")
            with open(path, "w") as f:
                f.write("{}")
            self.assertEqual(load_state(path), {})


class FilenameTest(unittest.TestCase):
    def test_basic_and_padded(self):
        self.assertEqual(daily_image_filename(date(2026, 8, 7)), "daily_image_2026_08_07.png")
        self.assertEqual(daily_image_filename(date(2026, 1, 2)), "daily_image_2026_01_02.png")

    def test_round_trip(self):
        for day in (date(2026, 8, 7), date(2026, 1, 2), date(2028, 2, 29)):
            with self.subTest(day=day):
                self.assertEqual(parse_daily_image_date(daily_image_filename(day)), day)

    def test_strict_rejects(self):
        bad_names = [
            ".gitkeep",
            "daily_image_2026_08_07.png.tmp",
            "daily_image_2026_8_7.png",       # unpadded
            "daily_image_2026_13_45.png",     # month 13
            "daily_image_2026_02_30.png",     # Feb 30 doesn't exist
            "daily_image_2026_08_07.PNG",     # case-sensitive extension
            "xdaily_image_2026_08_07.png",    # prefix junk
            "daily_image_2026_08_07.pngx",    # suffix junk
            "",
            None,
            "daily_image_20260807.png",       # missing underscores
        ]
        for name in bad_names:
            with self.subTest(name=name):
                self.assertIsNone(parse_daily_image_date(name))


def _names_for(dates):
    return [daily_image_filename(d) for d in dates]


def _consecutive_dates(start, count):
    return [start + timedelta(days=i) for i in range(count)]


class SelectImagesToPruneTest(unittest.TestCase):
    def test_retention_constant_is_14(self):
        self.assertEqual(DAILY_IMAGE_RETENTION, 14)

    def test_exactly_retention_count_prunes_nothing(self):
        dates = _consecutive_dates(date(2026, 8, 1), 14)
        self.assertEqual(select_images_to_prune(_names_for(dates), keep=14), [])

    def test_one_over_prunes_single_oldest(self):
        dates = _consecutive_dates(date(2026, 8, 1), 15)
        result = select_images_to_prune(_names_for(dates), keep=14)
        self.assertEqual(result, [daily_image_filename(dates[0])])

    def test_twenty_prunes_six_oldest(self):
        dates = _consecutive_dates(date(2026, 8, 1), 20)
        result = select_images_to_prune(_names_for(dates), keep=14)
        expected_pruned = set(_names_for(dates[:6]))
        self.assertEqual(set(result), expected_pruned)
        newest_14 = set(_names_for(dates[6:]))
        self.assertEqual(set(result) & newest_14, set())

    def test_order_independence_via_shuffle(self):
        # For strictly zero-padded valid names, lexical and chronological
        # order coincide by construction -- so this shuffle sweep, combined
        # with the strict-reject cases in FilenameTest, is what actually
        # pins the "sort by parsed date, not input position/string" contract
        # rather than a coincidentally-correct lexical sort.
        dates = _consecutive_dates(date(2026, 8, 1), 20)
        names = _names_for(dates)
        rng = random.Random(20260807)
        first_result = None
        for _ in range(20):
            shuffled = names[:]
            rng.shuffle(shuffled)
            result = sorted(select_images_to_prune(shuffled, keep=14))
            if first_result is None:
                first_result = result
            else:
                self.assertEqual(result, first_result)

    def test_cross_year_ordering(self):
        dates = _consecutive_dates(date(2025, 12, 25), 12)  # -> 2026-01-05
        result = select_images_to_prune(_names_for(dates), keep=10)
        self.assertEqual(set(result), set(_names_for(dates[:2])))

    def test_non_matching_names_never_pruned(self):
        dates = _consecutive_dates(date(2026, 8, 1), 20)
        intruders = [
            ".gitkeep",
            "daily_image_2026_8_7.png",
            "subdir",
            "daily_image_2026_08_07.png.tmp",
        ]
        names = _names_for(dates) + intruders
        for keep in (0, 14):
            with self.subTest(keep=keep):
                result = select_images_to_prune(names, keep=keep)
                for intruder in intruders:
                    self.assertNotIn(intruder, result)
                self.assertTrue(all(parse_daily_image_date(n) is not None for n in result))

    def test_keep_zero_returns_every_parseable_name(self):
        dates = _consecutive_dates(date(2026, 8, 1), 5)
        self.assertEqual(set(select_images_to_prune(_names_for(dates), keep=0)), set(_names_for(dates)))

    def test_keep_greater_than_count_returns_empty(self):
        dates = _consecutive_dates(date(2026, 8, 1), 5)
        self.assertEqual(select_images_to_prune(_names_for(dates), keep=100), [])

    def test_negative_keep_behaves_like_zero(self):
        dates = _consecutive_dates(date(2026, 8, 1), 5)
        self.assertEqual(
            set(select_images_to_prune(_names_for(dates), keep=-1)),
            set(select_images_to_prune(_names_for(dates), keep=0)),
        )

    def test_empty_input_returns_empty(self):
        self.assertEqual(select_images_to_prune([], keep=14), [])

    def test_duplicate_names_no_crash_and_newest_kept_never_pruned(self):
        dates = _consecutive_dates(date(2026, 8, 1), 20)
        names = _names_for(dates) + [daily_image_filename(dates[0])]  # duplicate of the oldest
        result = select_images_to_prune(names, keep=14)
        # Result must be a subset of the input...
        self.assertTrue(set(result).issubset(set(names)))
        # ...and none of the newest 14 distinct dates' filenames may appear,
        # regardless of the duplicate skewing a naive positional count.
        newest_14 = set(_names_for(dates[6:]))
        self.assertEqual(set(result) & newest_14, set())

    def test_input_not_mutated(self):
        dates = _consecutive_dates(date(2026, 8, 1), 20)
        names = _names_for(dates)
        before = copy.deepcopy(names)
        select_images_to_prune(names, keep=14)
        self.assertEqual(names, before)


class ParseBoolTest(unittest.TestCase):
    def test_truthy_strings(self):
        for value in ("1", "true", "True", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(parse_bool(value, False))

    def test_falsy_strings(self):
        for value in ("0", "false", "False", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(parse_bool(value, True))

    def test_none_and_blank_fall_back_to_default(self):
        self.assertTrue(parse_bool(None, True))
        self.assertFalse(parse_bool(None, False))
        self.assertTrue(parse_bool("", True))
        self.assertFalse(parse_bool("  ", False))

    def test_unrecognized_string_falls_back_to_default(self):
        # A misconfiguration falls back to the documented default rather
        # than crashing or guessing what the operator meant.
        self.assertTrue(parse_bool("banana", True))
        self.assertFalse(parse_bool("banana", False))


class ParseChannelIdTest(unittest.TestCase):
    def test_plain_id(self):
        self.assertEqual(parse_channel_id("123456789012345678"), 123456789012345678)
        self.assertIsInstance(parse_channel_id("123456789012345678"), int)

    def test_whitespace_and_mention_wrapper(self):
        self.assertEqual(parse_channel_id(" 123 "), 123)
        self.assertEqual(parse_channel_id("<#123456789012345678>"), 123456789012345678)

    def test_invalid_values_return_none(self):
        for bad in (None, "", "abc", "12a3", "-5", "0", "<#>"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_channel_id(bad))


class GetZoneTest(unittest.TestCase):
    def test_valid_name(self):
        zone, error = get_zone("America/New_York")
        self.assertEqual(zone.key, "America/New_York")
        self.assertIsNone(error)

    def test_unknown_name_falls_back_to_utc_with_error(self):
        zone, error = get_zone("Mars/Olympus_Mons")
        self.assertEqual(zone.key, "UTC")
        self.assertTrue(error)
        self.assertIn("Mars/Olympus_Mons", error)

    def test_none_and_empty_fall_back_with_error(self):
        for bad in (None, ""):
            with self.subTest(bad=bad):
                zone, error = get_zone(bad)
                self.assertEqual(zone.key, "UTC")
                self.assertIsNotNone(error)

    def test_custom_fallback_honored(self):
        zone, error = get_zone("Nope/Nope", fallback="Asia/Kolkata")
        self.assertEqual(zone.key, "Asia/Kolkata")
        self.assertIsNotNone(error)


class SeedScheduleDataTest(unittest.TestCase):
    """The shipped seed schedule must be structurally sound, mirroring
    test_macros.py's SeedLibraryDataTest."""

    def setUp(self):
        self.entries = load_schedule(SEED_SCHEDULE_FILE)

    def test_exactly_four_entries_in_order(self):
        self.assertEqual(len(self.entries), 4)
        self.assertEqual(
            [e["id"] for e in self.entries],
            ["morning", "lunch", "quitting_time", "goodnight"],
        )

    def test_times_and_they_all_parse(self):
        expected = {"morning": "07:00", "lunch": "12:00", "quitting_time": "17:00", "goodnight": "22:00"}
        for entry in self.entries:
            self.assertEqual(entry["time"], expected[entry["id"]])
            parse_slot_time(entry["time"])  # must not raise

    def test_morning_is_generate_without_magic(self):
        morning = next(e for e in self.entries if e["id"] == "morning")
        self.assertEqual(morning["type"], "generate")
        self.assertIs(morning["magic"], False)  # identity check: the bool False, not a falsy string

    def test_other_three_are_edit_with_magic(self):
        for entry in self.entries:
            if entry["id"] == "morning":
                continue
            with self.subTest(entry_id=entry["id"]):
                self.assertEqual(entry["type"], "edit")
                self.assertIs(entry["magic"], True)

    def test_edit_entries_have_prompt_and_message(self):
        for entry in self.entries:
            if entry["type"] != "edit":
                continue
            with self.subTest(entry_id=entry["id"]):
                self.assertTrue(entry.get("edit_prompt"))
                self.assertTrue(entry.get("message"))

    def test_morning_message_contains_date_placeholder(self):
        morning = next(e for e in self.entries if e["id"] == "morning")
        self.assertIn("{date}", morning["message"])

    def test_seed_validates_cleanly(self):
        good, errors = validate_schedule(self.entries)
        self.assertEqual(len(good), 4)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
