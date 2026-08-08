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
    UPDATABLE_FIELDS,
    WEEKDAYS,
    apply_slot_update,
    build_slot_entry,
    classify_slot_time,
    daily_image_filename,
    due_slots,
    find_generate_entry,
    find_slot,
    format_announcement_date,
    format_flag,
    format_schedule_lines,
    format_slot_detail,
    format_slot_summary,
    format_slot_time,
    get_zone,
    has_enabled_generate_slot,
    is_valid_slot_id,
    load_schedule,
    load_state,
    mark_fired,
    normalize_slot_id,
    parse_add_fields,
    parse_bool,
    parse_channel_id,
    parse_daily_image_date,
    parse_flag_value,
    parse_slot_time,
    render_message,
    save_schedule,
    save_state,
    schedule_file_is_corrupt,
    seconds_to_next_minute,
    seed_schedule,
    seed_source_for,
    select_images_to_prune,
    slot_entry_id,
    slot_instant,
    slot_is_enabled,
    toggle_slot,
    truncate_text,
    validate_schedule,
    validate_slot,
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


class FindGenerateEntryTest(unittest.TestCase):
    """&daily_image runs a due generate slot's work on demand (there is no catch-up),
    and needs that slot's message/magic settings so a manual run announces the image
    the same way the scheduled one would."""

    GEN = {"id": "morning", "time": "07:00", "type": "generate", "message": "M {date}", "magic": False}
    EDIT = {"id": "lunch", "time": "12:00", "type": "edit", "edit_prompt": "p", "message": "L"}

    def test_finds_the_generate_entry_among_edits(self):
        self.assertIs(find_generate_entry([self.EDIT, self.GEN, dict(self.EDIT, id="l2")]), self.GEN)

    def test_returns_the_original_dict_by_reference_not_a_copy(self):
        # The caller reads entry["message"]/entry.get("magic") off it; a copy would
        # work too, but returning the original keeps this consistent with due_slots'
        # documented by-reference contract.
        entries = [self.GEN]
        self.assertIs(find_generate_entry(entries), entries[0])

    def test_none_when_schedule_has_no_generate_slot(self):
        # The command falls back to its own default wording rather than raising.
        self.assertIsNone(find_generate_entry([self.EDIT]))

    def test_none_on_empty_and_non_list_schedules(self):
        for entries in ([], None, "not a list", {"id": "morning"}):
            with self.subTest(entries=entries):
                self.assertIsNone(find_generate_entry(entries))

    def test_first_accepted_generate_wins(self):
        # Several generate slots are legal (the retained filename is date-keyed, so
        # retention stays correct); first-wins mirrors validate_schedule's rule for
        # duplicate ids.
        second = dict(self.GEN, id="morning2", time="08:00")
        self.assertIs(find_generate_entry([self.GEN, second]), self.GEN)

    def test_invalid_generate_entry_is_skipped_for_the_next_valid_one(self):
        # A hand-corrupted generate slot must not be handed back half-formed -- the
        # command would then KeyError on entry["message"] at the worst moment.
        broken = {"id": "bad", "time": "7am", "type": "generate", "message": "m"}
        self.assertIs(find_generate_entry([broken, self.GEN]), self.GEN)

    def test_only_invalid_generate_entries_returns_none(self):
        no_message = {"id": "bad", "time": "07:00", "type": "generate"}
        self.assertIsNone(find_generate_entry([no_message]))

    def test_seed_schedule_has_a_generate_entry(self):
        # If the shipped seed ever lost its generate slot, &daily_image would silently
        # fall back to hardcoded wording instead of the schedule's.
        entry = find_generate_entry(load_schedule(SEED_SCHEDULE_FILE))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], "morning")

    def test_does_not_mutate_input(self):
        entries = [dict(self.EDIT), dict(self.GEN)]
        before = copy.deepcopy(entries)
        find_generate_entry(entries)
        self.assertEqual(entries, before)


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


# --- &daily_* command-surface pure logic ------------------------------------------
#
# Backs &daily_list/&daily_show/&daily_add/&daily_update/&daily_remove/&daily_toggle
# (bot_ross.py keeps thin wrappers -- see test_bot_ross_source.py's
# DailyCommandsValidateBeforeSaveTest for the AST-level checks on those wrappers,
# since bot_ross.py itself can never be imported under test).

class ParseFlagValueTest(unittest.TestCase):
    """Strict sibling of parse_bool: raises rather than falling back to a
    default, since a command typo must not silently write the OPPOSITE of
    what was asked."""

    def test_truthy_strings(self):
        for value in ("true", "True", " TRUE ", "1", "yes", "on"):
            with self.subTest(value=value):
                self.assertIs(parse_flag_value(value), True)

    def test_falsy_strings(self):
        for value in ("false", "0", "no", "off", "OFF", " false "):
            with self.subTest(value=value):
                self.assertIs(parse_flag_value(value), False)

    def test_unrecognized_values_raise_naming_the_value(self):
        for value in ("ture", "", "   ", None, "2", "yes please", 42):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as cm:
                    parse_flag_value(value)
                self.assertIn(repr(value), str(cm.exception))

    def test_strict_vs_lenient_split_is_deliberate(self):
        # parse_bool falls back to its default on the very same typo that
        # parse_flag_value must reject outright -- the two exist for
        # different failure modes (a bad env var vs. a bad command arg).
        self.assertIs(parse_bool("ture", True), True)
        with self.assertRaises(ValueError):
            parse_flag_value("ture")


class SlotIdTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_slot_id("  Morning "), "morning")
        self.assertEqual(normalize_slot_id("QUITTING_TIME"), "quitting_time")
        self.assertEqual(normalize_slot_id(None), "")

    def test_is_valid_slot_id_accepts(self):
        for name in ("morning", "quitting_time", "a", "x-1", "a" * 32):
            with self.subTest(name=name):
                self.assertTrue(is_valid_slot_id(name))

    def test_is_valid_slot_id_rejects(self):
        for name in ("", " ", "Morning", "has space", "a" * 33, "tea🖌", ";tok", 42):
            with self.subTest(name=name):
                self.assertFalse(is_valid_slot_id(name))

    def test_every_seed_id_is_addressable_by_command(self):
        for entry in load_schedule(SEED_SCHEDULE_FILE):
            with self.subTest(entry_id=entry["id"]):
                self.assertTrue(is_valid_slot_id(normalize_slot_id(entry["id"])))


class FormatSlotTimeTest(unittest.TestCase):
    def test_basic_cases(self):
        self.assertEqual(format_slot_time(7, 0), "07:00")
        self.assertEqual(format_slot_time(0, 0), "00:00")
        self.assertEqual(format_slot_time(23, 59), "23:59")
        self.assertEqual(format_slot_time(17, 5), "17:05")

    def test_round_trip_seed_times_stay_canonical(self):
        for time_str in ("07:00", "12:00", "17:00", "22:00"):
            with self.subTest(time_str=time_str):
                self.assertEqual(format_slot_time(*parse_slot_time(time_str)), time_str)

    def test_single_digit_hour_canonicalizes(self):
        # So the file can't accumulate mixed "7:00"/"07:00" formats across
        # repeated &daily_update edits.
        self.assertEqual(format_slot_time(*parse_slot_time("7:00")), "07:00")


class SlotLookupTest(unittest.TestCase):
    def setUp(self):
        self.seed = load_schedule(SEED_SCHEDULE_FILE)

    def test_find_by_reference(self):
        morning = next(e for e in self.seed if e["id"] == "morning")
        self.assertIs(find_slot(self.seed, "MORNING"), morning)

    def test_find_normalizes_whitespace(self):
        lunch = next(e for e in self.seed if e["id"] == "lunch")
        self.assertIs(find_slot(self.seed, "  lunch "), lunch)

    def test_not_found_and_non_list_entries(self):
        self.assertIsNone(find_slot(self.seed, "nope"))
        self.assertIsNone(find_slot(None, "x"))

    def test_broken_rows_stay_addressable_and_skippable(self):
        ok = {"id": "ok", "time": "07:00", "type": "generate", "message": "m"}
        entries = [42, None, {"no": "id"}, ok]
        self.assertIs(find_slot(entries, "ok"), ok)

    def test_slot_entry_id(self):
        self.assertEqual(slot_entry_id({"id": " Morning "}), "morning")
        self.assertIsNone(slot_entry_id(42))
        self.assertIsNone(slot_entry_id({"id": ""}))
        self.assertIsNone(slot_entry_id({"id": 7}))


class EnabledSchemaTest(unittest.TestCase):
    """validate_schedule's `enabled` rule -- must not disturb a schedule
    (including the shipped seed) that predates this field."""

    def test_seed_still_validates_with_no_enabled_key(self):
        entries = load_schedule(SEED_SCHEDULE_FILE)
        good, errors = validate_schedule(entries)
        self.assertEqual(good, entries)
        self.assertEqual(errors, [])
        for entry in entries:
            self.assertNotIn("enabled", entry)

    def test_bool_enabled_accepted_both_ways(self):
        for value in (True, False):
            with self.subTest(value=value):
                entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m", "enabled": value}
                good, errors = validate_schedule([entry])
                # A disabled slot is a VALID entry -- it's skipped at fire
                # time (due_slots), not rejected here.
                self.assertEqual(good, [entry])
                self.assertEqual(errors, [])

    def test_non_bool_enabled_rejected_naming_the_id(self):
        for bad_value in ("false", "true", 0, 1, None, []):
            with self.subTest(bad_value=bad_value):
                entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m", "enabled": bad_value}
                good, errors = validate_schedule([entry])
                self.assertEqual(good, [])
                self.assertEqual(len(errors), 1)
                self.assertIn("x", errors[0])
                self.assertIn("enabled", errors[0])

    def test_one_bad_enabled_row_does_not_take_down_the_schedule(self):
        valid = {"id": "ok", "time": "07:00", "type": "generate", "message": "m"}
        broken = {"id": "bad", "time": "07:00", "type": "generate", "message": "m", "enabled": "false"}
        good, errors = validate_schedule([valid, broken])
        self.assertEqual(good, [valid])
        self.assertEqual(len(errors), 1)

    def test_unknown_extra_keys_still_tolerated_alongside_enabled(self):
        entry = {
            "id": "x", "time": "07:00", "type": "generate", "message": "m",
            "enabled": True, "future_key": "x",
        }
        good, errors = validate_schedule([entry])
        self.assertEqual(good, [entry])
        self.assertEqual(errors, [])


class SlotIsEnabledTest(unittest.TestCase):
    def test_absent_means_enabled(self):
        # The entire backward-compat contract for the enabled schema change.
        self.assertIs(slot_is_enabled({}), True)

    def test_explicit_bool(self):
        self.assertIs(slot_is_enabled({"enabled": True}), True)
        self.assertIs(slot_is_enabled({"enabled": False}), False)

    def test_only_literal_false_disables(self):
        # Garbage values read as enabled here -- such rows are dropped by
        # validate_schedule before due_slots ever calls this, so the failure
        # direction is always "doesn't fire", never "fires anyway".
        self.assertIs(slot_is_enabled({"enabled": "false"}), True)
        self.assertIs(slot_is_enabled(42), True)


class DueSlotsEnabledTest(unittest.TestCase):
    """A disabled slot must never fire -- the highest-risk part of this change."""

    def test_disabled_slot_never_due_at_its_instant(self):
        entry = dict(SLOT_0700, enabled=False)
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        self.assertEqual(due_slots(instant, [entry], {}, NY), [])

    def test_disabled_slot_never_due_anywhere_in_the_miss_window(self):
        entry = dict(SLOT_0700, enabled=False)
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        for delta in (timedelta(0), timedelta(seconds=1), timedelta(minutes=1), timedelta(minutes=5), MISS_WINDOW):
            with self.subTest(delta=delta):
                self.assertEqual(due_slots(instant + delta, [entry], {}, NY), [])

    def test_backward_compat_absent_enabled_key_fires_as_before(self):
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        self.assertEqual(due_slots(instant, [SLOT_0700], {}, NY), [(SLOT_0700, date(2026, 8, 7))])

    def test_explicit_enabled_true_behaves_like_absent(self):
        entry = dict(SLOT_0700, enabled=True)
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        self.assertEqual(due_slots(instant, [entry], {}, NY), [(entry, date(2026, 8, 7))])

    def test_mixed_schedule_only_the_enabled_slot_fires(self):
        morning = dict(SLOT_0700, enabled=False)
        lunch = SLOT_1200
        lunch_instant = slot_instant(date(2026, 8, 7), 12, 0, NY)
        morning_instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        self.assertEqual(due_slots(lunch_instant, [morning, lunch], {}, NY), [(lunch, date(2026, 8, 7))])
        self.assertEqual(due_slots(morning_instant, [morning, lunch], {}, NY), [])

    def test_purity_disabled_entry_mutates_neither_entries_nor_state(self):
        entry = dict(SLOT_0700, enabled=False)
        entries = [entry]
        state = {}
        before_entries = copy.deepcopy(entries)
        before_state = copy.deepcopy(state)
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        due_slots(instant, entries, state, NY)
        self.assertEqual(entries, before_entries)
        self.assertEqual(state, before_state)

    def test_garbage_enabled_value_fails_safe_to_not_firing(self):
        # validate_schedule drops the entry outright (non-bool enabled) --
        # the failure direction is "doesn't fire", never "fires anyway".
        entry = dict(SLOT_0700, enabled="false")
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        self.assertEqual(due_slots(instant, [entry], {}, NY), [])

    def test_disabled_slot_near_the_local_midnight_boundary_never_fires(self):
        # now = 2026-08-08T04:03Z = local 00:03 EDT on the 8th, so local_date
        # is Aug 8 -- a 23:58 slot is a YESTERDAY-candidate-day match (mirrors
        # test_midnight_straddle_yesterdays_slot above), not a today-candidate
        # one. This specific straddle is what actually exercises the branch
        # the disabled skip needs to cover: the skip sits BEFORE due_slots'
        # candidate-day loop, so it must suppress the slot on the yesterday
        # candidate too, not just the (far more common, and separately
        # covered by every other test in this class) today candidate.
        entry = {"id": "late", "time": "23:58", "type": "generate", "message": "m", "enabled": False}
        now = datetime(2026, 8, 8, 4, 3, tzinfo=timezone.utc)
        self.assertEqual(due_slots(now, [entry], {}, NY), [])

        # Sibling assertion on the ENABLED twin: proves this really is the
        # yesterday-candidate branch (day == 2026-08-07, not 08-08) rather
        # than a case that happens to return [] for some other reason -- so a
        # future refactor that moved the disabled skip inside (or after) the
        # day loop, and thereby stopped covering this branch, can't leave this
        # test silently drifted back into an already-covered today-day case.
        enabled_twin = dict(entry, enabled=True)
        self.assertEqual(due_slots(now, [enabled_twin], {}, NY), [(enabled_twin, date(2026, 8, 7))])

    def test_re_enable_round_trip_fires_again(self):
        disabled = dict(SLOT_0700, enabled=False)
        instant = slot_instant(date(2026, 8, 7), 7, 0, NY)
        self.assertEqual(due_slots(instant, [disabled], {}, NY), [])
        enabled = dict(disabled, enabled=True)
        self.assertEqual(due_slots(instant, [enabled], {}, NY), [(enabled, date(2026, 8, 7))])


class ValidateSlotTest(unittest.TestCase):
    def test_valid_entry_returns_none(self):
        entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m"}
        self.assertIsNone(validate_slot(entry))

    def test_bad_time_returns_string_naming_time(self):
        entry = {"id": "x", "time": "25:00", "type": "generate", "message": "m"}
        error = validate_slot(entry)
        self.assertIsInstance(error, str)
        self.assertIn("time", error)

    def test_missing_edit_prompt_returns_string_naming_it(self):
        entry = {"id": "e", "time": "07:00", "type": "edit", "message": "m"}
        error = validate_slot(entry)
        self.assertIsInstance(error, str)
        self.assertIn("edit_prompt", error)

    def test_non_dict_input_returns_string_without_raising(self):
        self.assertIsInstance(validate_slot(42), str)
        self.assertIsInstance(validate_slot(None), str)

    def test_single_source_of_truth_against_validate_schedule(self):
        entries = [
            {"id": "ok", "time": "07:00", "type": "generate", "message": "m"},
            {"id": "bad_time", "time": "7am", "type": "generate", "message": "m"},
            {"id": "bad_type", "time": "07:00", "type": "paint", "message": "m"},
            {"id": "no_msg", "time": "07:00", "type": "generate"},
            {"id": "edit_no_prompt", "time": "07:00", "type": "edit", "message": "m"},
            {"id": "bad_magic", "time": "07:00", "type": "generate", "message": "m", "magic": "true"},
            {"id": "bad_enabled", "time": "07:00", "type": "generate", "message": "m", "enabled": "false"},
            {"id": "ok_edit", "time": "07:00", "type": "edit", "message": "m", "edit_prompt": "p"},
        ]
        for entry in entries:
            with self.subTest(entry_id=entry["id"]):
                good, errors = validate_schedule([entry])
                self.assertEqual(validate_slot(entry) is None, bool(good) and not errors)


class BuildSlotEntryTest(unittest.TestCase):
    def test_generate_entry_exact_shape(self):
        entry, error = build_slot_entry("teatime", "15:30", "generate", "Tea time!", author="kev", added="2026-08-08")
        self.assertIsNone(error)
        self.assertEqual(entry, {
            "id": "teatime", "time": "15:30", "type": "generate", "message": "Tea time!",
            "magic": False, "enabled": True, "author": "kev", "added": "2026-08-08",
        })

    def test_edit_entry_has_prompt_and_validates(self):
        entry, error = build_slot_entry(
            "teatime", "15:30", "edit", "Tea time!", edit_prompt="everyone stops for tea",
        )
        self.assertIsNone(error)
        self.assertEqual(entry["edit_prompt"], "everyone stops for tea")
        self.assertIsNone(validate_slot(entry))

    def test_single_digit_hour_canonicalized_and_bad_minute_rejected(self):
        entry, error = build_slot_entry("t", "7:30", "generate", "m")
        self.assertIsNone(error)
        self.assertEqual(entry["time"], "07:30")

        entry, error = build_slot_entry("t", "7:5", "generate", "m")
        self.assertIsNone(entry)
        self.assertIsNotNone(error)

    def test_edit_without_prompt_rejected(self):
        entry, error = build_slot_entry("t", "07:00", "edit", "m")
        self.assertIsNone(entry)
        self.assertEqual(
            error,
            "an 'edit' slot needs an edit prompt — add ` :: <edit prompt>` after the message.",
        )

    def test_generate_with_prompt_rejected(self):
        # Stricter than validate_schedule's file-level tolerance for a stray
        # edit_prompt on a generate entry -- at add time it's much more
        # likely a mistaken ` :: ` than a deliberate no-op field.
        entry, error = build_slot_entry("t", "07:00", "generate", "m", edit_prompt="p")
        self.assertIsNone(entry)
        self.assertEqual(
            error,
            "a 'generate' slot doesn't take an edit prompt — drop the ` :: ...` part.",
        )

    def test_various_invalid_inputs(self):
        cases = [
            ("Tea!", "07:00", "generate", "m", None),
            ("a" * 33, "07:00", "generate", "m", None),
            ("t", "07:00", "paint", "m", None),
            ("t", "07:00", "generate", "   ", None),
            ("t", "25:00", "generate", "m", None),
        ]
        for sid, t, ty, msg, ep in cases:
            with self.subTest(sid=sid, t=t, ty=ty, msg=msg):
                entry, error = build_slot_entry(sid, t, ty, msg, edit_prompt=ep)
                self.assertIsNone(entry)
                self.assertIsNotNone(error)

    def test_type_case_insensitive_and_text_stripped(self):
        entry, error = build_slot_entry("t", "07:00", "EDIT", "  Tea time!  ", edit_prompt="  drink tea  ")
        self.assertIsNone(error)
        self.assertEqual(entry["type"], "edit")
        self.assertEqual(entry["message"], "Tea time!")
        self.assertEqual(entry["edit_prompt"], "drink tea")

    def test_no_author_added_when_omitted(self):
        entry, error = build_slot_entry("t", "07:00", "generate", "m")
        self.assertIsNone(error)
        self.assertNotIn("author", entry)
        self.assertNotIn("added", entry)

    def test_every_success_case_validates(self):
        cases = [
            ("morningish", "07:00", "generate", None),
            ("teatime", "15:30", "edit", "p"),
        ]
        for sid, t, ty, ep in cases:
            with self.subTest(sid=sid):
                entry, error = build_slot_entry(sid, t, ty, "message text", edit_prompt=ep)
                self.assertIsNone(error)
                self.assertIsNone(validate_slot(entry))


SEED_LUNCH = {
    "id": "lunch", "time": "12:00", "type": "edit",
    "edit_prompt": "It's lunchtime!", "message": "Lunch break!", "magic": True,
}


class ApplySlotUpdateTest(unittest.TestCase):
    def test_time_update_is_pure(self):
        new, error = apply_slot_update(SEED_LUNCH, "time", " 12:30 ")
        self.assertIsNone(error)
        self.assertEqual(new["time"], "12:30")
        self.assertEqual(SEED_LUNCH["time"], "12:00")  # input untouched

    def test_message_stored_verbatim_no_operator_is_special(self):
        text = "Lunch | dinner :: not really ;rhe --res 1x1"
        new, error = apply_slot_update(SEED_LUNCH, "message", text)
        self.assertIsNone(error)
        self.assertEqual(new["message"], text)

    def test_edit_prompt_stripped_and_stored(self):
        new, error = apply_slot_update(SEED_LUNCH, "edit_prompt", "  new prompt  ")
        self.assertIsNone(error)
        self.assertEqual(new["edit_prompt"], "new prompt")

    def test_magic_off_is_a_real_bool(self):
        new, error = apply_slot_update(SEED_LUNCH, "magic", "off")
        self.assertIsNone(error)
        self.assertIs(new["magic"], False)  # assertIs -- the string "False" must fail this

    def test_enabled_no_is_a_real_bool(self):
        new, error = apply_slot_update(SEED_LUNCH, "enabled", "no")
        self.assertIsNone(error)
        self.assertIs(new["enabled"], False)

    def test_field_name_stripped_and_lowercased(self):
        new, error = apply_slot_update(SEED_LUNCH, " Time ", "13:00")
        self.assertIsNone(error)
        self.assertEqual(new["time"], "13:00")

    def test_unknown_field_rejected(self):
        new, error = apply_slot_update(SEED_LUNCH, "colour", "blue")
        self.assertIsNone(new)
        self.assertEqual(
            error,
            f"unknown field 'colour' — pick one of: {', '.join(UPDATABLE_FIELDS)}.",
        )

    def test_id_field_rejected_with_dedicated_message(self):
        # Guards the data/daily_state.json key-orphaning re-fire risk: a
        # rename would leave the fired-state key pointing at an id that no
        # longer exists, and a slot still inside MISS_WINDOW could then fire
        # a second time under its new id the same day.
        new, error = apply_slot_update(SEED_LUNCH, "id", "brunch")
        self.assertIsNone(new)
        self.assertIn("id can't be changed", error)
        self.assertIn("lunch", error)

    def test_bad_values_name_the_offending_value(self):
        cases = [("time", "7am"), ("magic", "ture"), ("type", "paint")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                new, error = apply_slot_update(SEED_LUNCH, field, value)
                self.assertIsNone(new)
                self.assertIn(value, error)

    def test_blank_message_rejected(self):
        new, error = apply_slot_update(SEED_LUNCH, "message", "   ")
        self.assertIsNone(new)
        self.assertEqual(error, "the message can't be blank.")

    def test_provenance_set_only_when_both_supplied(self):
        # The fixture MUST already carry author/added -- SEED_LUNCH (which
        # never had them) can only prove they're absent from the INPUT, which
        # is true either way and says nothing about whether an update
        # preserves or overwrites them. Building a fixture WITH creation
        # provenance and asserting on the RESULT is what actually pins "an
        # update sets editor/edited while leaving author/added untouched" --
        # the same edited-vs-created split &magic_update/&macro_update make.
        with_provenance = dict(SEED_LUNCH, author="original", added="2026-01-01")
        new, error = apply_slot_update(with_provenance, "time", "12:30", editor="kev", edited="2026-08-08")
        self.assertIsNone(error)
        self.assertEqual(new["author"], "original")
        self.assertEqual(new["added"], "2026-01-01")
        self.assertEqual(new["editor"], "kev")
        self.assertEqual(new["edited"], "2026-08-08")
        # Input untouched by the update, provenance included.
        self.assertEqual(with_provenance["author"], "original")
        self.assertNotIn("editor", with_provenance)

        # Neither editor/edited key appears at all when omitted entirely.
        new2, error2 = apply_slot_update(with_provenance, "time", "12:30")
        self.assertIsNone(error2)
        self.assertEqual(new2["author"], "original")
        self.assertNotIn("editor", new2)
        self.assertNotIn("edited", new2)

        # apply_slot_update requires BOTH editor and edited -- supplying only
        # one is the same as supplying neither (no partial provenance write).
        new3, error3 = apply_slot_update(with_provenance, "time", "12:30", editor="kev")
        self.assertIsNone(error3)
        self.assertNotIn("editor", new3)
        self.assertNotIn("edited", new3)

        new4, error4 = apply_slot_update(with_provenance, "time", "12:30", edited="2026-08-08")
        self.assertIsNone(error4)
        self.assertNotIn("editor", new4)
        self.assertNotIn("edited", new4)

    def test_invariant_matrix_every_single_field_update_stays_valid_except_one(self):
        # For a fully-valid entry, changing any ONE field to a reasonable
        # value must keep it valid -- EXCEPT type generate->edit on an entry
        # with no edit_prompt, which is the one case §1.4's write-invariant
        # exists to catch (apply_slot_update itself doesn't refuse it --
        # that's the caller's job in bot_ross.py -- but the resulting entry
        # IS rejectable, which is what the caller checks before saving).
        base = {"id": "x", "time": "07:00", "type": "generate", "message": "m"}
        self.assertIsNone(validate_slot(base))

        for field, value in (("time", "08:00"), ("message", "new message"), ("magic", "on"), ("enabled", "off")):
            with self.subTest(field=field):
                new, error = apply_slot_update(base, field, value)
                self.assertIsNone(error)
                self.assertIsNone(validate_slot(new))

        new, error = apply_slot_update(base, "type", "edit")
        self.assertIsNone(error)                    # apply_slot_update itself doesn't refuse
        self.assertIsNotNone(validate_slot(new))     # but the result is invalid -- caller must catch it

        # Changing `time` cannot cause a same-day re-fire: data/daily_state.json
        # is keyed by id + local day, so state["x"] == today already blocks a
        # second fire that day regardless of what time is now stored.


class ApplySlotUpdateErrorTruncationTest(unittest.TestCase):
    """apply_slot_update's rejection messages echo the caller-supplied `field`/
    `value` back into the error string, and bot_ross.py sends that string to
    Discord verbatim (`f"Couldn't update `{sid}`: {error}"`). These are REJECT
    paths -- nothing is written -- but an untruncated echo of a long enough
    `field`/`value` can itself exceed Discord's 2000-char message cap, so the
    write's rejection reply silently fails to send (discord.HTTPException) and
    the user gets no feedback at all: the exact silent-failure mode
    truncate_text was introduced (on the success path) to prevent. Regression:
    `apply_slot_update(SEED_LUNCH, "type", "x" * 1975)` produced a
    56-plus-1975 == 2031-char error string before this fix.
    """

    LONG = "x" * 1975  # comfortably past any reply prefix bot_ross.py could add

    def test_unknown_field_error_is_bounded(self):
        new, error = apply_slot_update(SEED_LUNCH, self.LONG, "blue")
        self.assertIsNone(new)
        # truncate_text's default limit is 60 chars + "…"; the rest of the
        # message (the "unknown field ... — pick one of: ..." scaffolding) is
        # itself short and fixed, so the whole string must stay well under
        # Discord's 2000-char cap regardless of how long `field` was.
        self.assertLess(len(error), 200)
        self.assertIn(truncate_text(self.LONG), error)
        self.assertNotIn(self.LONG, error)  # the untruncated 1975-char value must not appear

    def test_bad_type_value_error_is_bounded(self):
        new, error = apply_slot_update(SEED_LUNCH, "type", self.LONG)
        self.assertIsNone(new)
        self.assertLess(len(error), 200)
        self.assertIn(truncate_text(self.LONG), error)
        self.assertNotIn(self.LONG, error)

    def test_bad_bool_value_error_is_bounded(self):
        new, error = apply_slot_update(SEED_LUNCH, "magic", self.LONG)
        self.assertIsNone(new)
        self.assertLess(len(error), 200)
        self.assertIn(truncate_text(self.LONG), error)
        self.assertNotIn(self.LONG, error)

    def test_short_values_are_unaffected(self):
        # Pins that truncate_text is a no-op below its limit -- this is what
        # keeps test_bad_values_name_the_offending_value's assertIn(value,
        # error) passing unchanged for "7am"/"ture"/"paint".
        new, error = apply_slot_update(SEED_LUNCH, "colour", "blue")
        self.assertEqual(
            error,
            f"unknown field 'colour' — pick one of: {', '.join(UPDATABLE_FIELDS)}.",
        )


class ToggleSlotTest(unittest.TestCase):
    def test_first_toggle_disables_absent_key(self):
        entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m"}
        new = toggle_slot(entry)
        self.assertIs(new["enabled"], False)

    def test_toggle_re_enables_an_explicitly_disabled_entry(self):
        entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m", "enabled": False}
        new = toggle_slot(entry)
        self.assertIs(new["enabled"], True)

    def test_purity(self):
        entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m"}
        new = toggle_slot(entry)
        self.assertNotIn("enabled", entry)
        self.assertIsNot(new, entry)

    def test_provenance_recorded_when_supplied(self):
        entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m"}
        new = toggle_slot(entry, editor="kev", edited="2026-08-08")
        self.assertEqual(new["editor"], "kev")
        self.assertEqual(new["edited"], "2026-08-08")

    def test_double_toggle_returns_to_start_with_key_now_explicit(self):
        entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m"}
        twice = toggle_slot(toggle_slot(entry))
        self.assertIs(twice["enabled"], True)
        self.assertIn("enabled", twice)

    def test_broken_row_can_still_be_toggled(self):
        # A slot broken in some other field must always still be switchable
        # off -- toggle_slot doesn't validate, it just flips the flag.
        broken = {"id": "x", "time": "7am", "type": "generate", "message": "m"}
        new = toggle_slot(broken)
        self.assertIs(new["enabled"], False)


class ParseAddFieldsTest(unittest.TestCase):
    def test_no_separator_message_only(self):
        self.assertEqual(parse_add_fields("Tea time!"), ("Tea time!", None, None))

    def test_with_separator(self):
        self.assertEqual(
            parse_add_fields("Tea time! :: everyone stops for tea"),
            ("Tea time!", "everyone stops for tea", None),
        )

    def test_separator_used_twice_errors(self):
        message, prompt, error = parse_add_fields("a :: b :: c")
        self.assertIsNone(message)
        self.assertIsNone(prompt)
        self.assertIn("::", error)

    def test_unspaced_separator_never_silently_absorbed(self):
        # Any occurrence of "::" not bounded by whitespace on both sides is a
        # hard error, never silently folded into the message text.
        for text in ("a::b", "a ::b", "a:: b", "a ::"):
            with self.subTest(text=text):
                message, prompt, error = parse_add_fields(text)
                self.assertIsNone(message)
                self.assertIn("spaces", error)

    def test_blank_inputs(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                message, prompt, error = parse_add_fields(value)
                self.assertIsNone(message)
                self.assertEqual(error, "the message can't be blank.")

    def test_blank_edit_prompt_after_separator(self):
        message, prompt, error = parse_add_fields("msg ::   ")
        self.assertIsNone(message)
        self.assertEqual(error, "the edit prompt after ` :: ` can't be blank.")

    def test_pipe_is_not_a_separator(self):
        self.assertEqual(parse_add_fields("Lunch | dinner"), ("Lunch | dinner", None, None))

    def test_edges_stripped_interior_untouched(self):
        self.assertEqual(
            parse_add_fields("  Tea time!   ::   drink tea  "),
            ("Tea time!", "drink tea", None),
        )


class FormatSlotDisplayTest(unittest.TestCase):
    def setUp(self):
        self.seed = load_schedule(SEED_SCHEDULE_FILE)
        self.morning = next(e for e in self.seed if e["id"] == "morning")
        self.lunch = next(e for e in self.seed if e["id"] == "lunch")

    def test_summary_morning(self):
        self.assertEqual(
            format_slot_summary(self.morning),
            "`morning` — 07:00 generate — It's the image of the day for {date} (magic off, enabled)",
        )

    def test_summary_lunch(self):
        self.assertEqual(
            format_slot_summary(self.lunch),
            "`lunch` — 12:00 edit — Lunch break! (magic on, enabled)",
        )

    def test_disabled_entry_summary_bolds_disabled(self):
        entry = dict(self.lunch, enabled=False)
        self.assertTrue(format_slot_summary(entry).endswith("(magic on, **disabled**)"))

    def test_long_message_truncates_at_60_chars(self):
        entry = dict(self.lunch, message="x" * 100)
        summary = format_slot_summary(entry)
        self.assertIn("x" * 60 + "…", summary)
        self.assertNotIn("x" * 61, summary)

    def test_detail_lunch_exact(self):
        expected = (
            "`lunch` — 12:00 edit\n"
            "Message: Lunch break!\n"
            "Edit prompt: It's lunchtime!\n"
            "Magic: on | Enabled: on\n"
            "Author: built-in | Added: —"
        )
        self.assertEqual(format_slot_detail(self.lunch), expected)

    def test_detail_includes_editor_line_only_when_set(self):
        without_editor = format_slot_detail(self.lunch)
        self.assertNotIn("Last edited by", without_editor)
        with_editor = format_slot_detail(dict(self.lunch, editor="kevin", edited="2026-08-08"))
        self.assertIn("Last edited by: kevin on 2026-08-08", with_editor)

    def test_never_raises_on_ragged_input(self):
        for entry in ({}, {"id": "x"}, {"id": "x", "type": "edit"}, 42):
            with self.subTest(entry=entry):
                format_slot_summary(entry)
                format_slot_detail(entry)

    def test_format_schedule_lines_marks_broken_and_unaddressable_rows(self):
        broken = {"id": "broken", "time": "25:00", "type": "generate", "message": "m"}
        lines = format_schedule_lines([self.morning, broken, 42])
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], format_slot_summary(self.morning))
        self.assertTrue(lines[1].startswith("⚠️"))
        self.assertIn("broken", lines[1])
        self.assertEqual(
            lines[2],
            "⚠️ entry #3 in the file has no usable id — fix it by hand in data/daily_schedule.json",
        )

    def test_format_schedule_lines_preserves_file_order(self):
        lines = format_schedule_lines(self.seed)
        self.assertEqual(len(lines), 4)
        for entry, line in zip(self.seed, lines):
            self.assertTrue(line.startswith(f"`{entry['id']}`"))


class HasEnabledGenerateSlotTest(unittest.TestCase):
    def setUp(self):
        self.seed = load_schedule(SEED_SCHEDULE_FILE)

    def test_seed_has_an_enabled_generate_slot(self):
        self.assertTrue(has_enabled_generate_slot(self.seed))

    def test_false_when_morning_disabled(self):
        entries = [dict(e, enabled=False) if e["id"] == "morning" else e for e in self.seed]
        self.assertFalse(has_enabled_generate_slot(entries))

    def test_false_when_no_generate_slot_at_all(self):
        entries = [e for e in self.seed if e["type"] != "generate"]
        self.assertFalse(has_enabled_generate_slot(entries))

    def test_false_on_empty_schedule(self):
        self.assertFalse(has_enabled_generate_slot([]))

    def test_false_when_the_only_generate_entry_fails_validation(self):
        broken = {"id": "bad", "time": "7am", "type": "generate", "message": "m"}
        self.assertFalse(has_enabled_generate_slot([broken]))

    def test_find_generate_entry_still_returns_a_disabled_slot(self):
        # find_generate_entry backs the MANUAL &daily_image command, which
        # must keep using the slot's wording/magic even when the SCHEDULED
        # slot is off -- pins that find_generate_entry is unaffected by
        # `enabled` on purpose (it is not "fixed" by accident).
        entries = [dict(e, enabled=False) if e["id"] == "morning" else e for e in self.seed]
        entry = find_generate_entry(entries)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], "morning")


class ScheduleWriteRoundTripTest(unittest.TestCase):
    def test_build_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            entries = load_schedule(SEED_SCHEDULE_FILE)
            new_entry, error = build_slot_entry(
                "teatime", "15:30", "generate", "Tea time!", author="kev", added="2026-08-08",
            )
            self.assertIsNone(error)
            save_schedule(entries + [new_entry], path)

            loaded = load_schedule(path)
            good, errors = validate_schedule(loaded)
            self.assertEqual(len(good), 5)
            self.assertEqual(errors, [])
            self.assertEqual(next(e for e in loaded if e["id"] == "teatime"), new_entry)

    def test_enabled_persists_as_a_real_json_bool(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            entry = {"id": "x", "time": "07:00", "type": "generate", "message": "m"}
            save_schedule([entry], path)
            updated, error = apply_slot_update(load_schedule(path)[0], "enabled", "off")
            self.assertIsNone(error)
            save_schedule([updated], path)
            with open(path, encoding="utf-8") as f:
                raw = f.read()
            # A JSON bool, not the string "false" -- a string would be
            # rejected by validate_schedule on the very next scheduler tick.
            self.assertIn('"enabled": false', raw)
            self.assertNotIn('"enabled": "false"', raw)

    def test_unicode_survives_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            entry = {"id": "x", "time": "22:00", "type": "generate", "message": "Good night ♥️"}
            save_schedule([entry], path)
            loaded = load_schedule(path)
            self.assertEqual(loaded[0]["message"], "Good night ♥️")
            with open(path, encoding="utf-8") as f:
                self.assertIn("♥️", f.read())

    def test_end_to_end_disable_and_re_enable_gates_due_slots(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            save_schedule([{"id": "morning", "time": "07:00", "type": "generate", "message": "m"}], path)
            instant = slot_instant(date(2026, 8, 7), 7, 0, NY)

            disabled, error = apply_slot_update(load_schedule(path)[0], "enabled", "off")
            self.assertIsNone(error)
            save_schedule([disabled], path)
            self.assertEqual(due_slots(instant, load_schedule(path), {}, NY), [])

            enabled, error = apply_slot_update(load_schedule(path)[0], "enabled", "on")
            self.assertIsNone(error)
            save_schedule([enabled], path)
            due = due_slots(instant, load_schedule(path), {}, NY)
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0][1], date(2026, 8, 7))

    def test_nothing_writes_the_repo_root_seed(self):
        with open(SEED_SCHEDULE_FILE, "rb") as f:
            before = f.read()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            entries = load_schedule(SEED_SCHEDULE_FILE)
            seed_schedule(path, SEED_SCHEDULE_FILE)
            new_entry, _ = build_slot_entry("teatime", "15:30", "generate", "Tea time!")
            save_schedule(entries + [new_entry], path)
            updated, _ = apply_slot_update(load_schedule(path)[0], "enabled", "off")
            save_schedule([updated] + load_schedule(path)[1:], path)
        with open(SEED_SCHEDULE_FILE, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)


class SeedScheduleIntegrityTest(unittest.TestCase):
    """Guards the specific promise this change must not disturb: the shipped
    seed's four entries stay exactly as they are, with no `enabled` key
    added to any of them."""

    def setUp(self):
        self.entries = load_schedule(SEED_SCHEDULE_FILE)

    def test_exactly_four_entries_matching_fields(self):
        self.assertEqual(len(self.entries), 4)
        self.assertEqual([e["id"] for e in self.entries], ["morning", "lunch", "quitting_time", "goodnight"])
        self.assertEqual([e["time"] for e in self.entries], ["07:00", "12:00", "17:00", "22:00"])
        self.assertEqual([e["type"] for e in self.entries], ["generate", "edit", "edit", "edit"])
        self.assertEqual([e["magic"] for e in self.entries], [False, True, True, True])

    def test_no_entry_has_an_enabled_key(self):
        for entry in self.entries:
            with self.subTest(entry_id=entry["id"]):
                self.assertNotIn("enabled", entry)

    def test_every_entry_valid_and_enabled_by_default(self):
        for entry in self.entries:
            with self.subTest(entry_id=entry["id"]):
                self.assertIsNone(validate_slot(entry))
                self.assertIs(slot_is_enabled(entry), True)


class TruncateTextTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(truncate_text("hello"), "hello")

    def test_exactly_at_limit_unchanged(self):
        # Boundary: exactly `limit` characters must NOT get an ellipsis --
        # only text strictly longer than the limit is truncated.
        self.assertEqual(truncate_text("x" * 60), "x" * 60)

    def test_one_over_limit_truncates(self):
        self.assertEqual(truncate_text("x" * 61), "x" * 60 + "…")

    def test_matches_format_slot_summary_preview_rule(self):
        # This is the exact property finding #2's fix depends on: format_slot_summary's
        # message preview and truncate_text must produce IDENTICAL output, since
        # format_slot_summary now delegates to truncate_text instead of duplicating
        # the 60-char rule inline.
        long_message = "y" * 100
        entry = {"id": "x", "time": "07:00", "type": "generate", "message": long_message}
        self.assertIn(truncate_text(long_message), format_slot_summary(entry))

    def test_custom_limit(self):
        self.assertEqual(truncate_text("abcdef", limit=3), "abc…")
        self.assertEqual(truncate_text("abc", limit=3), "abc")

    def test_non_str_input_returns_empty_string_not_raise(self):
        for bad in (None, 42, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(truncate_text(bad), "")

    def test_trailing_whitespace_before_truncation_point_is_stripped(self):
        # Mirrors format_slot_summary's original inline rule: message[:60].rstrip() + "…"
        # -- a truncation that lands mid-word-boundary-plus-space shouldn't leave a
        # dangling space before the ellipsis.
        text = "x" * 59 + "   more text that gets cut off"
        result = truncate_text(text)
        self.assertTrue(result.endswith("…"))
        self.assertNotIn(" …", result)


class ScheduleFileIsCorruptTest(unittest.TestCase):
    """Backs &daily_list/&daily_add's guard against silently treating a
    syntax-broken data/daily_schedule.json as an empty (safe-to-overwrite)
    schedule -- load_schedule fails open to [] in both cases, so this is the
    one place that tells them apart."""

    def test_missing_file_is_not_corrupt(self):
        self.assertFalse(schedule_file_is_corrupt("/no/such/daily_schedule.json"))

    def test_empty_file_is_not_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            with open(path, "w") as f:
                f.write("")
            self.assertFalse(schedule_file_is_corrupt(path))

    def test_whitespace_only_file_is_not_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            with open(path, "w") as f:
                f.write("   \n  ")
            self.assertFalse(schedule_file_is_corrupt(path))

    def test_valid_empty_list_is_not_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            with open(path, "w") as f:
                f.write("[]")
            self.assertFalse(schedule_file_is_corrupt(path))

    def test_valid_populated_list_is_not_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            save_schedule(load_schedule(SEED_SCHEDULE_FILE), path)
            self.assertFalse(schedule_file_is_corrupt(path))

    def test_syntax_broken_json_is_corrupt(self):
        # The exact real-world case: one hand-edit trailing comma.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            with open(path, "w") as f:
                f.write('[{"id":"morning", "time":"07:00",}]')
            self.assertTrue(schedule_file_is_corrupt(path))

    def test_valid_json_that_is_not_a_list_is_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            with open(path, "w") as f:
                f.write('{"id": "morning"}')
            self.assertTrue(schedule_file_is_corrupt(path))

    def test_load_schedule_agreement_on_the_corrupt_case(self):
        # Ties the two functions together: whenever schedule_file_is_corrupt
        # is True, load_schedule must have failed open to [] -- that's the
        # exact ambiguity this function exists to break.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily_schedule.json")
            with open(path, "w") as f:
                f.write("{ not valid json")
            self.assertTrue(schedule_file_is_corrupt(path))
            self.assertEqual(load_schedule(path), [])


if __name__ == "__main__":
    unittest.main()
