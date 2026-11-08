"""Secret magic mixins: five rotating mixins written each night from that day's prompts.

Pure, like magic_paint.py: no Discord, OpenAI or bot side effects. bot_ross.py binds
these functions to its data/ paths, its clock, and the chat-completions call.

- The prompt log (data/prompt_log.json) is a list of
  {"day": "YYYY-MM-DD", "at": iso, "user": name, "command": name, "prompt": text},
  appended for every person-typed image prompt, after macro expansion and before magic
  (so mixins never feed back into themselves). Entries older than LOG_KEEP_DAYS are
  pruned on every append.
- The secret store (data/secret_magic.json) is
  {"slots": [{"id", "text", "created", "prompt_count"}], "last_run": day, "generated": n}.
  At most MAX_SLOTS; a new mixin replaces the oldest once full.
- The nightly job runs at RUN_HOUR:RUN_MINUTE in the bot's zone, at most once a day,
  within daily_schedule.MISS_WINDOW of that time (no catch-up, same as daily slots),
  and only writes a mixin when the day has at least MIN_PROMPTS logged prompts.
"""

import json
import os
from datetime import date, datetime, timedelta

import daily_schedule

MAX_SLOTS = 5
MIN_PROMPTS = 5
LOG_KEEP_DAYS = 7
RUN_HOUR, RUN_MINUTE = 23, 30
MAX_PROMPTS_IN_REQUEST = 200      # keep the chat request bounded on a very busy day
MAX_PROMPT_CHARS = 400            # per logged prompt, in the chat request
MAX_MIXIN_CHARS = 400


# --- JSON files (fail open: a missing or corrupt file reads as empty) -----------------

def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(obj, path):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# --- Prompt log ------------------------------------------------------------------------

def load_log(path):
    entries = _load(path, [])
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("prompt"), str)] \
        if isinstance(entries, list) else []


def prune_log(entries, today, keep_days=LOG_KEEP_DAYS):
    cutoff = (today - timedelta(days=keep_days - 1)).isoformat()
    return [e for e in entries if str(e.get("day", "")) >= cutoff]


def append_prompt(path, day, at, user, command, prompt):
    """Append one prompt and prune old days. Blank prompts are ignored."""
    prompt = (prompt or "").strip()
    if not prompt:
        return
    entries = prune_log(load_log(path), day)
    entries.append({"day": day.isoformat(), "at": at, "user": user, "command": command, "prompt": prompt})
    _save(entries, path)


def prompts_for(entries, day):
    d = day.isoformat()
    return [e["prompt"] for e in entries if e.get("day") == d]


# --- Secret store ---------------------------------------------------------------------

def load_store(path):
    store = _load(path, {})
    if not isinstance(store, dict):
        store = {}
    slots = [s for s in store.get("slots", []) if isinstance(s, dict) and isinstance(s.get("text"), str)]
    return {"slots": slots, "last_run": store.get("last_run"), "generated": int(store.get("generated", 0) or 0)}


def save_store(store, path):
    _save(store, path)


def add_secret(store, text, created, prompt_count, max_slots=MAX_SLOTS):
    """Return a new store with `text` added, dropping the oldest slots past `max_slots`.

    `created` is an ISO timestamp; slots are kept oldest first. Ids are
    `secret-<YYYYMMDD>` with a numeric suffix if that day already has one."""
    slots = list(store.get("slots", []))
    base = "secret-" + created[:10].replace("-", "")
    taken = {s.get("id") for s in slots}
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}-{n}", n + 1
    slots.append({"id": sid, "text": text, "created": created, "prompt_count": prompt_count})
    slots.sort(key=lambda s: s.get("created", ""))
    return {**store, "slots": slots[-max_slots:], "generated": int(store.get("generated", 0) or 0) + 1}


def magic_entries(store):
    """The secret slots in the shape magic_paint's selection expects ({"id", "text"})."""
    return [{"id": s["id"], "text": s["text"]} for s in store.get("slots", []) if s.get("text")]


# --- Nightly job ----------------------------------------------------------------------

def run_instant(day, zone):
    return daily_schedule.slot_instant(day, RUN_HOUR, RUN_MINUTE, zone)


def is_due(now, zone, last_run):
    """True when `now` (aware) is within MISS_WINDOW after today's run time and the job
    hasn't run today. Returns (due, day)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    day = now.astimezone(zone).date()
    delta = now - run_instant(day, zone)
    due = timedelta(0) <= delta <= daily_schedule.MISS_WINDOW and last_run != day.isoformat()
    return due, day


def build_messages(prompts, examples):
    """Chat messages asking for ONE new secret mixin, in the style of `examples` (the
    human-written mixin texts), inspired by the themes of the day's `prompts`."""
    shown = [p[:MAX_PROMPT_CHARS] for p in prompts[-MAX_PROMPTS_IN_REQUEST:]]
    example_lines = "\n".join(f"- {t}" for t in examples) or "- (none)"
    prompt_lines = "\n".join(f"- {p}" for p in shown)
    system = (
        "You write 'magic mixins' for Bot Ross, a Discord art bot. A mixin is one sentence "
        "appended to someone's image prompt that slips a small, funny recurring background "
        "gag into the picture. Match the voice and length of the examples. Draw on the themes, "
        "characters and running jokes in today's prompts so the gag feels like the server's "
        "current mood, but don't copy any prompt. It must work as a background detail in any "
        "picture. No real people, brands or franchise characters; nothing violent, sexual, "
        "hateful or politically charged; no legible text beyond a word or two. Reply with "
        "the sentence only."
    )
    user = (
        f"Existing mixins (examples of the style):\n{example_lines}\n\n"
        f"Today's {len(shown)} prompts from the server:\n{prompt_lines}\n\n"
        "Write one new mixin."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def clean_mixin(text):
    """Normalize the model's reply to one line; None if it's unusable."""
    if not isinstance(text, str):
        return None
    line = " ".join(text.split()).strip().strip('"').strip("“”").strip()
    if line.lower().startswith("mixin:"):
        line = line[6:].strip()
    if not line or len(line) > MAX_MIXIN_CHARS:
        return None
    return line
