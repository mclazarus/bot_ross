"""Pure, side-effect-free daily-image scheduling logic.

Kept separate from bot_ross.py (which ends in bot.run() at import) so it can be
imported and unit tested without Discord/OpenAI secrets, mirroring
release_image.py/macros.py. No function here ever calls datetime.now() -- "now"
is always a parameter -- which is the entire point of the module: the container
runs UTC while the schedule is wall-clock in a configurable bot timezone
(BOT_TIMEZONE, default America/New_York), and DST gaps/folds plus UTC/local day
confusion are exactly the kind of silent-shift bug that must be exhaustively
unit tested rather than eyeballed against a running bot. See test_daily_schedule.py.

Every morning the bot posts a deterministic "image of the day" (its prompt
derived from the date via release_image's mad-libs algorithm) to a configured
channel, then posts themed edits of that same retained image at fixed times
through the day. Retained base images are pruned to a fixed count so the data/
volume doesn't grow without bound.

Run tests: python -m unittest test_daily_schedule -v
"""

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import json_library

logger = logging.getLogger("bot_ross.daily_schedule")

DEFAULT_TIMEZONE = "America/New_York"
DAILY_IMAGE_RETENTION = 14                      # newest N daily base PNGs kept on disk
MISS_WINDOW = timedelta(minutes=10)             # how late a slot may fire; no catch-up beyond it

# Never strftime("%A"/"%B") for announcement text -- locale-dependent, so a
# container running under a non-English locale would silently change the
# announcement wording. These literal English tuples make the rendering
# locale-independent by construction. WEEKDAYS is indexed by date.weekday()
# (Monday = 0); MONTHS by date.month - 1.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


# --- Env parsing helpers -------------------------------------------------------------
#
# All three are deliberately never-raise: a typo'd env var must fall back to a
# documented default, not crash the bot at import/startup.

_TRUTHY_STRINGS = {"1", "true", "yes", "on"}
_FALSY_STRINGS = {"0", "false", "no", "off"}


def parse_bool(value, default):
    """Parse an env-style boolean string. None/empty/whitespace-only, or
    anything not recognized (e.g. a typo'd "ture"), falls back to `default`
    rather than crashing startup or silently disabling a feature."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in _TRUTHY_STRINGS:
        return True
    if normalized in _FALSY_STRINGS:
        return False
    return default


# Discord channel mentions paste as "<#123...>"; strip one such wrapper before
# validating the remainder is a plain positive integer.
_CHANNEL_MENTION_RE = re.compile(r"^<#(.*)>$")
_ASCII_DIGITS_RE = re.compile(r"[0-9]+")


def parse_channel_id(value):
    """Parse a Discord channel id out of an env var. Accepts a bare id, one
    surrounding whitespace run, or a single "<#...>" mention wrapper (users
    copy-paste those). Anything else -- non-digits, a non-positive value,
    None/empty -- returns None rather than raising, so a stray space or a
    pasted mention can never crash the bot at import (int(os.environ[...])
    would)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    match = _CHANNEL_MENTION_RE.match(text)
    if match:
        text = match.group(1)
    # fullmatch on [0-9]+ (not str.isdigit(), which also accepts non-ASCII
    # digit characters like superscripts or Arabic-Indic digits) enforces
    # ASCII digits only, per the contract.
    if not _ASCII_DIGITS_RE.fullmatch(text):
        return None
    channel_id = int(text)
    return channel_id if channel_id > 0 else None


def get_zone(name, fallback="UTC"):
    """Resolve an IANA timezone name to a ZoneInfo, without ever raising.

    Returns (zone, error): on a valid `name`, (ZoneInfo(name), None). On
    None/empty/unknown name, (ZoneInfo(fallback), error_string) where
    error_string names the bad value -- the tuple return lets bot_ross.py log
    its own loud warning while this module stays log-agnostic about policy.
    `fallback` itself must be valid ("UTC" always is); this function does not
    guard against a bad fallback."""
    if not name:
        return ZoneInfo(fallback), f"missing/empty timezone name (falling back to {fallback})"
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError) as e:
        return ZoneInfo(fallback), f"unknown timezone {name!r}: {e}"


# --- Schedule library I/O (delegates to json_library.py, same as macros/magic) ------

def load_schedule(path):
    """Read the daily schedule fresh from `path`. Not cached in memory.
    Returns [] (fails open) if the file is missing, malformed, not a list, or
    empty -- the same fails-open promise macros.py/magic_paint.py make."""
    return json_library.load_library(path, label="daily schedule")


def save_schedule(entries, path):
    """Write the daily schedule to `path`. Preserves unicode for readability."""
    json_library.save_library(entries, path)


def seed_schedule(working_path, default_path):
    """Deploy the bundled default schedule onto the working path if it isn't
    there yet, so hand edits to data/daily_schedule.json survive image
    rebuilds/redeploys."""
    json_library.seed_library(working_path, default_path, label="daily schedule")


# --- Schedule validation --------------------------------------------------------------

_SLOT_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def parse_slot_time(value):
    """Parse an "H:MM"/"HH:MM" 24-hour wall-clock string into (hour, minute).

    Single-digit hour ("7:00") is accepted since hand-edited configs will
    contain it; single-digit minute ("7:5") is NOT -- that's more likely a
    typo than a deliberately terse time. Raises ValueError (naming the
    offending value) on anything else: non-string, out-of-range hour/minute,
    a malformed shape, or an empty/None value."""
    if not isinstance(value, str):
        raise ValueError(f"slot time must be a string, got {value!r}")
    match = _SLOT_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"not a valid HH:MM slot time: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"slot time out of range: {value!r}")
    return hour, minute


def validate_schedule(entries):
    """Validate a loaded schedule, dropping any structurally broken entry
    rather than letting it take down the whole scheduler -- the same
    fails-open promise macros.py's per-entry tolerance makes.

    Returns (good_entries, errors): `good_entries` is a NEW list containing
    the original entry dicts (by reference) that passed every rule below;
    `errors` is a list of human-readable strings, one per rejected entry or
    structural problem. Never raises; never mutates `entries`.

    An entry is rejected (with one error naming its id, or its list index
    when the id itself is unusable) if: it isn't a dict; `id` is
    missing/not a string/blank after stripping; `id` duplicates an earlier
    ACCEPTED entry's id (first occurrence wins -- a duplicate whose earlier
    copy was itself rejected for another reason is not treated as a dup);
    `time` fails parse_slot_time; `type` isn't exactly "generate" or "edit";
    `message` is missing/not a string/blank; type == "edit" and
    `edit_prompt` is missing/not a string/blank; `magic` is present but not
    a bool (the literal True/False -- the string "true" is a reject).
    `magic` absent is fine (readers treat it as False); a stray
    `edit_prompt` on a "generate" entry is tolerated and ignored; unknown
    extra keys are tolerated (forward compatibility).
    """
    if not isinstance(entries, list):
        return [], ["daily schedule is not a list"]

    good = []
    errors = []
    accepted_ids = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entry at index {index} is not an object")
            continue

        raw_id = entry.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            errors.append(f"entry at index {index} has a missing/blank id")
            continue
        entry_id = raw_id.strip()

        if entry_id in accepted_ids:
            errors.append(f"entry id {entry_id!r} is a duplicate; keeping the first occurrence")
            continue

        try:
            parse_slot_time(entry.get("time"))
        except ValueError as e:
            errors.append(f"entry id {entry_id!r} has an invalid time: {e}")
            continue

        entry_type = entry.get("type")
        if entry_type not in ("generate", "edit"):
            errors.append(f"entry id {entry_id!r} has an unknown type: {entry_type!r}")
            continue

        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            errors.append(f"entry id {entry_id!r} is missing a message")
            continue

        if entry_type == "edit":
            edit_prompt = entry.get("edit_prompt")
            if not isinstance(edit_prompt, str) or not edit_prompt.strip():
                errors.append(f"entry id {entry_id!r} is type 'edit' but missing edit_prompt")
                continue

        if "magic" in entry and not isinstance(entry.get("magic"), bool):
            errors.append(f"entry id {entry_id!r} has a non-bool magic value: {entry.get('magic')!r}")
            continue

        accepted_ids.add(entry_id)
        good.append(entry)

    return good, errors


def find_generate_entry(entries):
    """Return the schedule's "generate" slot -- the one that paints the day's base
    image -- or None if the schedule has no generate entry at all.

    Used by the manual &daily_image command, which does a due generate slot's work
    on demand (there is deliberately no catch-up, so a slot missed while the bot was
    down is simply lost). It needs that entry only for its `message` and `magic`
    settings, so the manual run announces the image exactly the way the scheduled
    one would rather than inventing its own wording.

    Entries are validated first, so a hand-corrupted generate entry is skipped
    rather than handed back half-formed -- the caller can then fall back to its own
    default message instead of raising on a missing key.

    If a schedule somehow defines several generate slots (allowed -- the filename is
    date-keyed, so retention stays correct), the FIRST accepted one wins, matching
    validate_schedule's first-wins rule for duplicate ids.
    """
    good, _errors = validate_schedule(entries)
    for entry in good:
        if entry["type"] == "generate":
            return entry
    return None


# --- Time-of-slot resolution (the DST heart) ------------------------------------------

def slot_instant(day, hour, minute, zone):
    """Resolve a slot's wall-clock time on `day` (a date) in `zone` to an
    aware, DST-normalized instant.

    Builds datetime(day, hour, minute, tzinfo=zone, fold=0) and normalizes it
    THROUGH UTC (dt.astimezone(UTC).astimezone(zone)) rather than trusting the
    naive construction directly. Consequences, all deliberate and all tested:

      - A normal wall time maps to itself.
      - A NONEXISTENT wall time (a spring-forward gap, e.g. 02:30 on a
        "spring forward at 2am" day) is pushed forward by the size of the
        gap: fold=0 resolves the naive construction using the pre-transition
        (standard) offset per PEP 495, and the UTC round-trip then renders
        that instant using the zone's actual (post-gap) offset -- so e.g. a
        02:00 slot ends up posting at 03:00 EDT, not silently never firing.
      - An AMBIGUOUS wall time (a fall-back fold) resolves to the FIRST
        occurrence (fold=0 = the earlier UTC offset).

    Naive arithmetic here would silently produce a wall time that never
    happens (or happens twice), so a slot could just stop firing twice a
    year -- this normalization is the whole reason this module exists.
    """
    naive = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    return naive.astimezone(timezone.utc).astimezone(zone)


def classify_slot_time(day, hour, minute, zone):
    """Diagnostic/logging aid: classify a wall-clock slot time as "normal",
    "ambiguous" (a fall-back fold -- ends up firing at the FIRST occurrence),
    or "nonexistent" (a spring-forward gap -- ends up firing pushed forward
    by the gap). Compares the fold=0 and fold=1 UTC offsets of the same naive
    construction: equal -> normal; fold=1 offset greater -> nonexistent (the
    wall clock jumped forward across this time); fold=1 offset smaller ->
    ambiguous (the wall clock repeated this time)."""
    fold0 = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    fold1 = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=1)
    offset0, offset1 = fold0.utcoffset(), fold1.utcoffset()
    if offset0 == offset1:
        return "normal"
    if offset1 > offset0:
        return "nonexistent"
    return "ambiguous"


def due_slots(now, entries, state, zone):
    """Compute which schedule entries are due to fire right now.

    `now` must be an AWARE datetime in any timezone (the container passes
    UTC); a naive `now` raises ValueError immediately, since a naive
    datetime here would silently be treated as local time by
    now.astimezone() and shift every slot by the zone's UTC offset -- the
    exact bug this whole module exists to prevent, so this fails loudly
    instead of quietly.

    `entries` is re-validated defensively via validate_schedule (an entry
    that wouldn't survive validation is silently skipped), so a hand-
    corrupted schedule can never raise out of the scheduler's per-minute
    tick even if the caller forgot to validate first.

    `state` maps slot id -> ISO date string of the last local day that slot
    fired (non-string values simply never compare equal, so they fail
    open rather than raise). `zone` is the bot's ZoneInfo; it cannot be
    inferred from `now` because `now` is (typically) UTC, not local.

    For each accepted entry, both today's and yesterday's local date are
    considered as the candidate "day" (yesterday matters because a
    late-evening slot evaluated just after local midnight belongs to
    YESTERDAY's day key). A candidate is due iff
    0 <= now - slot_instant(day, ...) <= MISS_WINDOW (both ends inclusive)
    and the slot hasn't already fired for that day per `state`. MISS_WINDOW
    is well under 24h, so at most one candidate day can match per entry.

    There is deliberately NO catch-up: an instant more than MISS_WINDOW in
    the past is never due, regardless of state -- this alone (even with an
    empty/lost state file) prevents a fall-back-fold slot from firing twice,
    since the second occurrence of an ambiguous time is a full UTC-offset-
    delta past the first occurrence's (normalized) instant.

    Returns a list of (entry, day) pairs sorted by ascending instant (ties
    keep `entries`' original order, since Python's sort is stable) -- so if
    a redeploy left both a generate slot and an edit slot due in the same
    tick, the generate always resolves first regardless of list order,
    and the edit finds its base image.

    Pure: mutates neither `entries` nor `state`. Returned entries are the
    original dicts, by reference (not copies).
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"due_slots requires an aware `now`, got naive: {now!r}")

    # Normalize `now` to a fixed-offset (UTC) representation before doing ANY
    # subtraction against it. The docstring/contract above promises `now` may
    # be "an aware datetime in any timezone" -- including `zone` itself. If we
    # subtracted a `zone`-attached `now` from a `zone`-attached `instant`
    # directly, Python performs intra-zone subtraction, which per PEP 495
    # IGNORES `fold` entirely: both occurrences of an ambiguous fall-back wall
    # time would compare equal to their instant's local representation, so
    # the second (fold=1) occurrence would compute delta 0 instead of the
    # true 1-hour gap -- a double-fire. Converting to UTC first forces every
    # subtraction below to be inter-zone (fold-aware), regardless of what
    # timezone the caller's `now` happened to be expressed in.
    now = now.astimezone(timezone.utc)

    good, _errors = validate_schedule(entries)
    local_date = now.astimezone(zone).date()

    candidates = []  # (instant, entry, day), later sorted by instant only
    for entry in good:
        hour, minute = parse_slot_time(entry["time"])
        for day in (local_date, local_date - timedelta(days=1)):
            instant = slot_instant(day, hour, minute, zone)
            delta = now - instant
            if timedelta(0) <= delta <= MISS_WINDOW and state.get(entry["id"]) != day.isoformat():
                candidates.append((instant, entry, day))

    candidates.sort(key=lambda c: c[0])
    return [(entry, day) for _instant, entry, day in candidates]


def mark_fired(state, slot_id, day):
    """Return a NEW dict: a shallow copy of `state` with slot_id marked fired
    for `day`. Never mutates `state` -- aliasing the in-memory state with the
    about-to-be-persisted state would be an easy bug to introduce here."""
    new_state = dict(state)
    new_state[slot_id] = day.isoformat()
    return new_state


def seconds_to_next_minute(now):
    """Seconds until the next wall-clock minute boundary, clamped to
    [1.0, 60.0]. Accepts naive or aware `now` (only .second/.microsecond are
    read). At exactly :00.000000 this is 60.0 (a full minute, not 0 --
    firing immediately at the boundary would busy-loop). At :59.9x the raw
    value is under a second and clamps up to 1.0 -- the 10-minute due
    window makes overshooting the boundary by under a second harmless, but a
    near-zero sleep would just busy-loop/double-tick the same minute."""
    raw = 60 - now.second - now.microsecond / 1_000_000
    return max(1.0, min(60.0, raw))


# --- Daily content helpers -------------------------------------------------------------

def seed_source_for(day):
    """The `source` string fed to release_image.build_release_prompt for a
    given local day: exactly day.isoformat() (zero-padded YYYY-MM-DD). This
    format must be stable FOREVER -- changing it would change every future
    daily prompt (release_image folds the raw source string into its hash)."""
    return day.isoformat()


def format_announcement_date(day):
    """Render a date as "Friday, August 7, 2026" -- unpadded day-of-month,
    weekday/month names from the literal WEEKDAYS/MONTHS tuples (never
    strftime, which is locale-dependent -- see the module-level comment)."""
    weekday = WEEKDAYS[day.weekday()]
    month = MONTHS[day.month - 1]
    return f"{weekday}, {month} {day.day}, {day.year}"


def render_message(template, day):
    """Substitute "{date}" in `template` with format_announcement_date(day).

    Deliberately str.replace, NEVER str.format: a hand-edited
    data/daily_schedule.json message containing a stray "{" or an unrelated
    "{typo}" would make str.format raise (KeyError/ValueError) every single
    day at slot time. str.replace is total -- it can't raise on odd input,
    it just leaves anything that isn't literally "{date}" alone."""
    return template.replace("{date}", format_announcement_date(day))


# --- Retention ---------------------------------------------------------------------

def daily_image_filename(day):
    """The retained-base-image filename for a local day, e.g.
    "daily_image_2026_08_07.png" (always zero-padded)."""
    return f"daily_image_{day.year:04d}_{day.month:02d}_{day.day:02d}.png"


_DAILY_IMAGE_RE = re.compile(r"daily_image_(\d{4})_(\d{2})_(\d{2})\.png")


def parse_daily_image_date(name):
    """Inverse of daily_image_filename: parse a filename back to a date, or
    None if it doesn't strictly match (case-sensitive, lowercase ".png"
    only, no surrounding slop, zero-padded fields, a calendar-valid date).
    Never raises -- a structurally matching but calendar-invalid date (month
    13, Feb 30) also returns None. Strict parsing means only files this bot
    provably wrote are ever pruning candidates -- a loose match could delete
    .gitkeep, a temp file, or an unrelated user file that merely resembles
    the pattern."""
    if not isinstance(name, str):
        return None
    match = _DAILY_IMAGE_RE.fullmatch(name)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def select_images_to_prune(names, keep=DAILY_IMAGE_RETENTION):
    """Select which of `names` to DELETE, keeping the `keep` newest daily
    base images.

    Only names that parse_daily_image_date accepts are ever candidates;
    anything else (a subdirectory, .gitkeep, an unpadded/mis-cased/otherwise
    malformed name) is never returned, no matter what. `keep <= 0` returns
    every parseable name; `keep >=` the number of distinct dates present
    returns []; empty input returns [].

    Candidates are deduplicated BY DATE before selection (a defensive-input
    case: a corrupted directory listing could report the same filename
    twice). This matters for correctness, not just tidiness -- if a
    duplicate weren't collapsed first, a naive "sort everything, then keep
    the first N entries" could let one date eat two slots in the kept
    bucket, pushing a genuinely-newest date across the boundary into the
    pruned list. Deduplicating by date first means duplicates can never
    shift who counts as one of the `keep` newest.

    Selection is by the PARSED date, never the filename string -- for
    strictly zero-padded valid names the two orders happen to coincide,
    which is exactly why this must not be allowed to quietly regress to a
    lexical sort if the filename format ever changes (a non-zero-padded or
    differently-shaped name would sort wrong lexically). Never mutates
    `names`; the result is independent of the input's ordering.
    """
    by_date = {}
    for name in names:
        parsed = parse_daily_image_date(name)
        if parsed is not None and parsed not in by_date:
            by_date[parsed] = name

    if not by_date:
        return []

    newest_first = sorted(by_date.items(), key=lambda pair: pair[0], reverse=True)
    if keep <= 0:
        return [name for _day, name in newest_first]
    if keep >= len(newest_first):
        return []
    return [name for _day, name in newest_first[keep:]]


# --- Fired-state I/O (its own tiny format -- a dict, not a list, so NOT json_library) --

def load_state(path):
    """Read the fired-state dict fresh from `path`. Not cached in memory.
    Returns {} (fails open) if the file is missing, unreadable, malformed
    JSON, or JSON that isn't an object (a list, a string, ...) -- worst case
    after a corrupt write is one slot re-firing once inside its 10-minute
    window, never a permanently wedged scheduler."""
    try:
        with open(path, "r") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load daily state from {path}: {e}")
        return {}
    if not isinstance(state, dict):
        logger.error(f"Daily state {path} is not a JSON object; ignoring.")
        return {}
    return state


def save_state(state, path):
    """Write the fired-state dict to `path`, overwriting in place."""
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
