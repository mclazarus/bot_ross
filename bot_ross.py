import asyncio
import aiohttp
import time
import signal
import discord
import os
import json
import types
from datetime import datetime, date, timezone
from discord.ext import commands
import openai
import random
import logging
import coloredlogs
import base64
import io
import string
import re
import release_image
import magic_paint
import macros
import image_size
import daily_schedule
import pipe_chain
import message_links
from magic_paint import parse_magic_rate, format_magic_rate

logger = logging.getLogger("bot_ross")

# Import-safe defaults: load_config() reassigns all of these from the environment
# at startup (see main()); under test the module imports with these values and
# tests override individual globals directly.
OPENAI_API_KEY = None
DISCORD_BOT_TOKEN = None
LIMIT = 100
IMAGE_MODEL = 'gpt-image-2-low'
IMAGE_MODERATION = 'low'
MEME_MODEL = 'gpt-5.4-mini'
MAGIC_PAINT_RATE = 0.05
DRAIN_TIMEOUT = 300.0
BOT_TIMEZONE = daily_schedule.DEFAULT_TIMEZONE
BOT_ZONE, _ = daily_schedule.get_zone(BOT_TIMEZONE)   # never raises; America/New_York
DAILY_IMAGE_ENABLED = True
_raw_daily_channel = None
DAILY_IMAGE_CHANNEL_ID = None
DAILY_CHANNEL_MISCONFIGURED = False

DATA_FILE = "data/request_data.json"


def _require(env, name):
    """Return env[name] if it's a non-empty, non-whitespace-only string;
    otherwise exit with a legible message. run.sh runs the container
    --restart=unless-stopped, so a startup failure is an infinite crash-loop --
    each iteration should print one legible line naming the missing variable
    instead of a bare KeyError traceback. Empty/whitespace-only counts as
    missing too: `docker --env-file` turns a line `OPENAI_API_KEY=` into an
    empty string, which today boots "successfully" and then 401s on the
    first paint with no hint why."""
    value = env.get(name)
    if value is None or not str(value).strip():
        raise SystemExit(
            f"Required environment variable {name} is missing or empty (see env.example)."
        )
    return value


def setup_logging():
    logging.basicConfig(level=logging.INFO)
    coloredlogs.install(level='INFO', logger=logger, milliseconds=True)


def load_config(env=None):
    """Parse configuration out of `env` (any mapping; env=None means
    os.environ) and reassign the module-scope config globals. Config stays
    module globals, not a Config object: &magic_rate mutates MAGIC_PAINT_RATE
    via `global` at runtime, and ~40 call sites (including lambdas, which
    close over the global rather than capturing it) read these names at call
    time, so reassignment is picked up with zero call-site changes."""
    global OPENAI_API_KEY, DISCORD_BOT_TOKEN, LIMIT, IMAGE_MODEL, IMAGE_MODERATION, \
        MEME_MODEL, MAGIC_PAINT_RATE, DRAIN_TIMEOUT, BOT_TIMEZONE, BOT_ZONE, \
        DAILY_IMAGE_ENABLED, _raw_daily_channel, DAILY_IMAGE_CHANNEL_ID, \
        DAILY_CHANNEL_MISCONFIGURED

    if env is None:
        env = os.environ

    # Load OpenAI API key and Discord bot token from environment variables
    OPENAI_API_KEY = _require(env, 'OPENAI_API_KEY')
    DISCORD_BOT_TOKEN = _require(env, 'DISCORD_BOT_TOKEN')
    # transitional: get_meme_prompt still authenticates via the v0.27 SDK global; removed in C4
    openai.api_key = OPENAI_API_KEY

    # Configuration
    LIMIT            = int(env.get('API_LIMIT', 100))
    IMAGE_MODEL      = env.get('IMAGE_MODEL', 'gpt-image-2-low')
    IMAGE_MODERATION = env.get('IMAGE_MODERATION', 'low')
    MEME_MODEL       = env.get('MEME_MODEL', 'gpt-5.4-mini')

    try:
        MAGIC_PAINT_RATE = float(env.get('MAGIC_PAINT_RATE', 0.05))
        if not (0.0 <= MAGIC_PAINT_RATE <= 1.0):
            raise ValueError
    except (TypeError, ValueError):
        MAGIC_PAINT_RATE = 0.05

    # On SIGTERM/SIGINT the bot stops accepting new commands and waits up to DRAIN_TIMEOUT
    # seconds for in-flight image generations to finish before closing (see
    # _graceful_shutdown). Raised from 60 to 300: a pipe chain now brackets its WHOLE
    # run (up to 5 sequential image calls) with active_requests, and a 5-segment chain
    # routinely takes well over a minute -- run.sh's STOP_TIMEOUT must stay above this.
    try:
        DRAIN_TIMEOUT = float(env.get('DRAIN_TIMEOUT', 300))
        if DRAIN_TIMEOUT < 0:
            raise ValueError
    except (TypeError, ValueError):
        DRAIN_TIMEOUT = 300.0

    # The bot's single wall-clock timezone for the daily-image scheduler (see
    # _daily_scheduler_loop below) -- all slot times in daily_schedule.json are wall-clock
    # in THIS zone, regardless of the container's own (UTC) clock. A bad/unknown
    # BOT_TIMEZONE falls back to UTC rather than crashing at import; get_zone() reports
    # the problem back as a string so we can still log it loudly here.
    BOT_TIMEZONE = env.get('BOT_TIMEZONE', daily_schedule.DEFAULT_TIMEZONE)
    BOT_ZONE, _tz_error = daily_schedule.get_zone(BOT_TIMEZONE)
    if _tz_error:
        logger.warning(f"BOT_TIMEZONE problem, falling back to UTC: {_tz_error}")

    # DAILY_IMAGE_CHANNEL_ID unset (None) disables the scheduler entirely, same as
    # DAILY_IMAGE_ENABLED=false -- see on_ready. Both are parsed leniently (never raise) so a
    # typo'd env var can't crash the bot at import.
    DAILY_IMAGE_ENABLED = daily_schedule.parse_bool(env.get('DAILY_IMAGE_ENABLED'), True)
    _raw_daily_channel = env.get('DAILY_IMAGE_CHANNEL_ID')
    DAILY_IMAGE_CHANNEL_ID = daily_schedule.parse_channel_id(_raw_daily_channel)
    # Distinguish "not configured" from "configured but unparseable". Both yield None, but
    # reporting them the same way is how a set-but-malformed channel id got read as
    # "no channel configured" for a full day -- see the inline-comment check below.
    DAILY_CHANNEL_MISCONFIGURED = DAILY_IMAGE_CHANNEL_ID is None and bool(
        (_raw_daily_channel or "").strip()
    )

    # `docker run --env-file` takes everything after the first "=" as the value, comment
    # included, so a .env written with trailing `# ...` comments silently poisons every
    # value it touches. Each parser above then falls back to its default without saying
    # why. Check the raw values once at startup and name the variable, so this shows up as
    # one obvious log line instead of a scheduler that quietly never runs.
    for _name in ('BOT_TIMEZONE', 'DAILY_IMAGE_CHANNEL_ID', 'DAILY_IMAGE_ENABLED',
                  'DRAIN_TIMEOUT', 'API_LIMIT', 'IMAGE_MODEL', 'IMAGE_MODERATION',
                  'MEME_MODEL', 'MAGIC_PAINT_RATE'):
        if daily_schedule.looks_like_inline_comment(env.get(_name)):
            logger.warning(
                f"{_name} looks like it contains an inline `# comment` -- docker --env-file "
                f"does not strip those, so the comment is part of the value and this "
                f"setting is being ignored. Put comments on their own line in .env."
            )


# The working library lives on the persistent data/ volume so user-added mixins survive
# redeploys; DEFAULT_MAGIC_PROMPTS_FILE is the seed baked into the image (see _seed_magic_library).
MAGIC_PROMPTS_FILE = "data/magic_prompts.json"
DEFAULT_MAGIC_PROMPTS_FILE = "magic_prompts.json"

# Same two-copy seed/working-volume pattern as the magic library, for ;macro expansions.
MACROS_FILE = "data/macros.json"
DEFAULT_MACROS_FILE = "macros.json"

# Same two-copy seed/working-volume pattern again, for the daily-image schedule. The
# fired-state file and the retained-image directory are volume-only -- never shipped,
# never seeded (see daily_schedule.py's module docstring and _seed_daily_schedule below).
DAILY_SCHEDULE_FILE = "data/daily_schedule.json"
DEFAULT_DAILY_SCHEDULE_FILE = "daily_schedule.json"
DAILY_STATE_FILE = "data/daily_state.json"
DAILY_IMAGES_DIR = "data/daily_images"

MODEL_CONFIGS = {
    "gpt-image-2": {
        "model": "gpt-image-2",
        "params": {"size": "1024x1024", "quality": "high"},
        "has_revised_prompt": False,
        "supports_moderation": True,
        "supports_edit": True,
    },
    "gpt-image-2-medium": {
        "model": "gpt-image-2",
        "params": {"size": "1024x1024", "quality": "medium"},
        "has_revised_prompt": False,
        "supports_moderation": True,
        "supports_edit": True,
    },
    "gpt-image-2-low": {
        "model": "gpt-image-2",
        "params": {"size": "1024x1024", "quality": "low"},
        "has_revised_prompt": False,
        "supports_moderation": True,
        "supports_edit": True,
    },
    "dall-e-3": {
        "model": "dall-e-3",
        "params": {"size": "1024x1024", "quality": "hd", "style": "vivid", "response_format": "b64_json"},
        "has_revised_prompt": True,
        "supports_moderation": False,
        "supports_edit": False,
    },
}


def format_duration(seconds):
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    elif seconds < 90:
        return f"{seconds:.1f}s"
    else:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def format_uptime(seconds):
    """Human-readable uptime as `Dd Hh Mm Ss`, dropping leading zero units.
    e.g. 3h 15m 42s when under a day, 15m 42s when under an hour, 42s under a minute."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# Thin wrappers binding magic_paint's pure logic to this module's file paths and the
# live (runtime-mutable) MAGIC_PAINT_RATE. The rate calculation itself lives in
# magic_paint.py so it can be unit tested (see test_magic_paint.py).
def _load_magic_library():
    return magic_paint.load_magic_library(MAGIC_PROMPTS_FILE)


def _apply_random_magic_entry(prompt):
    return magic_paint.apply_random_magic_entry(prompt, path=MAGIC_PROMPTS_FILE)


def _save_magic_library(entries):
    magic_paint.save_magic_library(entries, MAGIC_PROMPTS_FILE)


def _seed_magic_library():
    magic_paint.seed_magic_library(MAGIC_PROMPTS_FILE, DEFAULT_MAGIC_PROMPTS_FILE)


def maybe_apply_magic_paint(prompt):
    """Random-roll magic paint at the live MAGIC_PAINT_RATE. Only reads the library if the roll succeeds."""
    return magic_paint.maybe_apply_magic_paint(prompt, MAGIC_PAINT_RATE, path=MAGIC_PROMPTS_FILE)


# Thin wrappers binding macros.py's pure logic to this module's file paths, mirroring
# the magic-library wrappers above. The macro-expansion logic itself lives in macros.py
# so it can be unit tested (see test_macros.py).
def _load_macro_library():
    return macros.load_macro_library(MACROS_FILE)


def _save_macro_library(entries):
    macros.save_macro_library(entries, MACROS_FILE)


def _seed_macro_library():
    macros.seed_macro_library(MACROS_FILE, DEFAULT_MACROS_FILE)


# Thin wrapper binding daily_schedule.py's seed logic to this module's file paths,
# mirroring the magic/macro seed wrappers above.
def _seed_daily_schedule():
    daily_schedule.seed_schedule(DAILY_SCHEDULE_FILE, DEFAULT_DAILY_SCHEDULE_FILE)


# Thin wrappers binding daily_schedule.py's I/O to DAILY_SCHEDULE_FILE, used by the
# &daily_* management commands below. The scheduler tick (_run_due_daily_slots) and
# &daily_image call daily_schedule.load_schedule(DAILY_SCHEDULE_FILE) directly instead
# (test_bot_ross_source.py's test_scheduler_tick_reloads_the_schedule_fresh asserts
# that exact attribute-call shape) -- so if you "tidy up" those two call sites to use
# _load_daily_schedule() instead, update that test alongside it. Either way, nothing
# here is cached: every caller reads/writes the working copy on the data/ volume
# fresh, so a &daily_* edit takes effect on the very next scheduler tick (at most
# ~60s later) with no restart required.
def _load_daily_schedule():
    return daily_schedule.load_schedule(DAILY_SCHEDULE_FILE)


def _save_daily_schedule(entries):
    daily_schedule.save_schedule(entries, DAILY_SCHEDULE_FILE)


def format_rate_change_time(iso_str):
    """Render a stored rate-change timestamp as 'YYYY-MM-DD HH:MM:SS ±HHMM'.
    Legacy naive timestamps recorded before timezones were tracked render without the offset."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    rendered = dt.strftime("%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is not None:
        rendered += dt.strftime(" %z")
    return rendered


def _record_rate_change(user, rate):
    """Append a rate-change record to the persisted history (keeps the last 10) and log it."""
    data = load_data()
    history = data.get('magic_rate_history', [])
    history.append({"user": user, "rate": rate, "time": datetime.now().astimezone().isoformat()})
    data['magic_rate_history'] = history[-10:]
    save_data(data)
    logger.info(f"Magic rate changed to {format_magic_rate(rate)} ({rate}) by {user}")


def _bump_stat(key, amount=1):
    """load / increment `key` (default 0) by `amount` / save. The single chokepoint
    every simple persisted counter in this module funnels through -- 'magic',
    'pipes', and 'pipe_segments' all use it (see _bump_magic_counter and
    _run_chain)."""
    data = load_data()
    data[key] = data.get(key, 0) + amount
    save_data(data)


def _bump_magic_counter():
    """Increment the persisted 'magic' counter. Extracted out of send_quote so the
    daily scheduler and quiet pipe segments -- neither of which has a quote message
    to attach the 🖌️ tell to -- can bump it directly when a magic roll succeeds;
    without this extraction, the counter would silently stop counting them."""
    _bump_stat('magic')


async def send_quote(ctx, magic=False):
    quote = get_random_bob_ross_quote()
    if magic:
        quote += " 🖌️"
        _bump_magic_counter()
    await ctx.send(quote)


async def send_long(ctx, text):
    """Send text to Discord, chunking on newlines to stay under the 2000-char message limit."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 1900:
            if chunk:
                await ctx.send(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        await ctx.send(chunk)


async def expand_prompt_macros(ctx, prompt):
    """Expand every ';token' in `prompt` via the macro library (data/macros.json).

    The single chokepoint every prompt-bearing command calls, and always the FIRST
    step -- before magic paint -- so an expanded macro's text is itself eligible to
    pick up a magic mixin appended after it. Whenever any ';token' was present, the
    fully expanded prompt is echoed back on an 'expanded prompt: ...' line -- this is
    the post-macro, PRE-magic-paint prompt, so it deliberately never reveals a magic
    mixin. An unresolved token is swapped for a joke fallback so the prompt stays
    usable, and every miss is also called out on a leading 🎲 line.
    Increments the persisted 'macros'/'macro_misses' counters (shown in &stats) by
    however many tokens actually hit/missed on this call -- if the prompt had no
    ';tokens' at all, nothing is written to disk and nothing is sent."""
    prompt, hits, misses = macros.expand_macros(prompt, path=MACROS_FILE)
    if hits or misses:
        lines = []
        if misses:
            tokens = ", ".join(f"`;{m}`" for m in misses)
            lines.append(f"🎲 {tokens} (macro not found, good luck)")
        lines.append(f"expanded prompt: {prompt}")
        await send_long(ctx, "\n".join(lines))
        data = load_data()
        data['macros'] = data.get('macros', 0) + len(hits)
        data['macro_misses'] = data.get('macro_misses', 0) + len(misses)
        save_data(data)
    return prompt


intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.presences = True
intents.message_content = True
bot = commands.Bot(command_prefix='&', intents=intents)

start_time = datetime.now()

# Graceful-drain state. Mutated only on the single asyncio loop thread, so no locking.
# `active_requests` counts in-flight do_the_art() calls; `draining` blocks new commands
# once a shutdown signal has been received. `_signals_installed` guards handler setup
# against on_ready firing again on reconnect.
active_requests = 0
draining = False
_signals_installed = False

# The daily-image scheduler's background task, and the last set of schedule-validation
# errors we logged (so a persistently bad data/daily_schedule.json warns once per
# distinct problem, not once a minute forever -- see _run_due_daily_slots).
_daily_task = None
_last_schedule_errors = []


async def _graceful_shutdown(sig_name):
    """Stop accepting new commands, wait (up to DRAIN_TIMEOUT) for in-flight generations
    to finish, then close the bot so bot.run() returns and the process exits cleanly.
    Installed as the SIGTERM (docker stop) and SIGINT (Ctrl+C) handler; a second signal
    while already draining is a no-op (docker stop's own timeout is the hard backstop)."""
    global draining
    if draining:
        return
    draining = True
    logger.info(f"{sig_name}: draining {active_requests} active request(s), up to {DRAIN_TIMEOUT:g}s")
    deadline = time.monotonic() + DRAIN_TIMEOUT
    while active_requests > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    if active_requests > 0:
        logger.warning(f"Drain timed out with {active_requests} request(s) still running; closing anyway.")
    else:
        logger.info("Drain complete; closing.")
    await bot.close()


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f)


def get_current_month():
    # BEHAVIOR CHANGE: this used to be datetime.now().strftime(...) (the container's
    # own, UTC, clock). Moving it to BOT_ZONE is the correct reading of "a single
    # global timezone" for the daily scheduler, but it also moves the monthly
    # spend-limit boundary by up to 5 hours on one day a month -- a deliberate,
    # explicitly-called-out behavior change (see CLAUDE.md), not an incidental one.
    return datetime.now(BOT_ZONE).strftime("%Y-%m")


@bot.event
async def on_ready():
    global _signals_installed, _daily_task
    logger.info(f'{bot.user.name} has connected to Discord!')
    # discord.py installs no SIGTERM handler, so as PID 1 in Docker the process would
    # ignore `docker stop` until it SIGKILLs. Install real loop handlers so we drain
    # instead. Idempotent, but guard anyway since on_ready refires on reconnect.
    if not _signals_installed:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(_graceful_shutdown(s.name))
            )
        _signals_installed = True

    # Start the daily-image scheduler, idempotently -- on_ready can refire on
    # reconnect, so guard against starting a second loop the same way _signals_installed
    # guards the signal handlers above.
    if not DAILY_IMAGE_ENABLED:
        logger.info("Daily image scheduler disabled (DAILY_IMAGE_ENABLED is false).")
    elif DAILY_CHANNEL_MISCONFIGURED:
        # Set but unparseable is a MISCONFIGURATION, not a choice to leave the feature
        # off -- warn, and say what the value actually was, rather than reporting it as
        # "not set" (which reads as "working as configured" and hides the real problem).
        logger.warning(
            f"Daily image scheduler disabled: DAILY_IMAGE_CHANNEL_ID is set but isn't a "
            f"channel id: {_raw_daily_channel!r}. Expected digits only (a `<#123>` mention "
            f"also works); note that docker --env-file keeps trailing `# comments` as part "
            f"of the value."
        )
    elif DAILY_IMAGE_CHANNEL_ID is None:
        logger.info("Daily image scheduler disabled: no DAILY_IMAGE_CHANNEL_ID set.")
    elif _daily_task is None or _daily_task.done():
        _daily_task = asyncio.create_task(_daily_scheduler_loop())
        _daily_task.add_done_callback(_log_daily_task_result)


@bot.check
async def _reject_while_draining(ctx):
    """Global check: once draining, refuse new commands with a friendly note instead of
    starting work we'd have to abandon at close()."""
    if draining:
        await ctx.send("🧹 Bot Ross is wrapping up and restarting for an update — try again in a moment.")
        return False
    return True


@bot.event
async def on_command_error(ctx, error):
    # Swallow the drain refusal (CheckFailure) and unknown commands so they don't spam
    # "Ignoring exception in command" in the logs; log anything genuinely unexpected.
    if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
        return
    logger.error(f"Command error in {ctx.command}: {error}")


@bot.command(name='ping', help='Check for bot liveness and latency. (ms)')
async def ping(ctx):
    await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')


@bot.command(name='meme', help='Create an image based on a GPT generated prompt takes suggestions. monthly limit')
async def meme(ctx, *, prompt=None):
    if prompt:
        await ctx.send(f"Generating meme prompt based on: {prompt}")
    else:
        await ctx.send(f"Generating meme prompt based on GPTs wildest imagination.")
    gpt_prompt = await get_meme_prompt(prompt)
    await ctx.send(f"Generated prompt: {gpt_prompt}")
    if await do_the_art(ctx, gpt_prompt, "meme", IMAGE_MODEL):
        data = load_data()
        if 'memes' not in data:
            data['memes'] = 0
        data['memes'] += 1
        save_data(data)


async def _prep_generation_size(ctx, raw):
    """Parse --square/--landscape/--portrait/--res out of a generation command's raw
    prompt text. Called FIRST, before macro expansion or magic paint, so the flags
    never reach the image prompt.

    Returns (cleaned_prompt, size) on success. Returns (None, None) after already
    sending the user an error message, for two failure cases: an invalid --res value,
    or nothing left to paint once the flags are stripped out. Along the way it may
    also send an informational (non-error) message: a note when --res overrides an
    orientation flag given in the same command, and a coercion notice when the
    resolved size differs from what was literally requested (silent otherwise --
    orientation presets and an already-valid --res never trigger this notice).
    """
    text, orientation, res_raw = image_size.parse_size_flags(raw)

    res_wh = None
    if res_raw is not None:
        try:
            res_wh = image_size.parse_resolution(res_raw)
        except ValueError:
            await ctx.send(
                f"`--res {res_raw}` isn't a size I understand — use `WIDTHxHEIGHT`, "
                f"e.g. `--res 1920x1080`."
            )
            return None, None

    prompt = text.strip()
    if not prompt:
        await ctx.send("...I need something to paint besides the size flags.")
        return None, None

    size, requested = image_size.resolve_generation_size(orientation, res_wh)
    if res_wh and orientation:
        await ctx.send(f"(`--res` overrides `--{orientation}`)")
    if requested and requested != size:
        await ctx.send(f"Using `{size}` (adjusted from `{requested}` to fit the size limits).")

    return prompt, size


# --- Per-command "once" bodies ---------------------------------------------------
#
# Each of these is a command's current body, moved verbatim (same message strings,
# same ordering, same early returns) so it can run either as today's single command
# OR as segment 1 of a pipe chain (see _piped/_run_chain below) with zero behavior
# difference for the un-piped case. `magic_mode` is one of "roll" (the existing
# probabilistic maybe_apply_magic_paint), "none", or "always" (the existing
# guaranteed _apply_random_magic_entry, &xpaint's gag) -- it's threaded through
# rather than hardcoded so _paint_once covers &paint/&hpaint/&mpaint/&lpaint/&xpaint,
# whose only real difference is which magic mode and model config they use.

async def _paint_once(ctx, raw, request_type, model, magic_mode):
    """&paint/&hpaint/&mpaint/&lpaint/&xpaint's shared body: parse size flags ->
    macro-expand -> apply magic per magic_mode -> post the requester's quote ->
    generate."""
    prompt, size = await _prep_generation_size(ctx, raw)
    if prompt is None:
        return False
    prompt = await expand_prompt_macros(ctx, prompt)
    if magic_mode == "roll":
        prompt, magic = maybe_apply_magic_paint(prompt)
        await send_quote(ctx, magic)
    elif magic_mode == "always":
        prompt = _apply_random_magic_entry(prompt)
        await send_quote(ctx, magic=True)
    else:
        await ctx.send(get_random_bob_ross_quote())
    return await do_the_art(ctx, prompt, request_type, model, size=size)


async def _dpaint_once(ctx, raw):
    """&dpaint's body, verbatim. dall-e-3 isn't wired to image_size, so there's
    deliberately no size-flag parsing here (unlike _paint_once)."""
    prompt = await expand_prompt_macros(ctx, raw)
    await ctx.send(get_random_bob_ross_quote())
    return await do_the_art(ctx, prompt, "dpaint", "dall-e-3")


async def _resolve_linked_images(ctx, links):
    """Resolve pasted Discord message links (see message_links.py) to their image
    attachments for &remix. Returns (linked_attachments, note_lines).

    SECURITY: message_links.classify_link only compares ids found in the pasted
    URL text, which is attacker-controlled -- it is a cheap pre-filter, nothing
    more. The URL's guild segment and channel segment are independent fields a
    user can set to anything, and Discord routes a message by channel id, not by
    whatever guild id happened to be in the link, so a link can *claim* the
    current server while actually pointing at a channel in a different one. The
    two checks below are the real authorization, and both run against the
    RESOLVED objects, never the URL:
      (a) the resolved channel's `guild.id` equals `ctx.guild.id`.
      (b) the INVOKING user (`ctx.author`, not the bot) has `view_channel` AND
          `read_message_history` on that resolved channel.
    Skipping (b) would turn &remix into an image-exfiltration tool: the bot's own
    permissions are frequently broader than a given requester's, so without this
    check anyone could paste a link into a private channel they can't see and
    have the bot fetch its image for them anyway.

    Every failure is soft and bucketed into one of six fixed notices (S1-S6, sent
    as a single joined message by the caller) -- S3 deliberately does NOT say
    *why* a link failed (channel not found vs. no bot access vs. no requester
    access vs. deleted message all look identical to the channel), so &remix
    can't be used as an oracle to probe which private channels/messages exist.
    The real reason is logged (never posted) for each S3-bucketed link.
    """
    if not links:
        return [], []

    current_guild_id = ctx.guild.id if ctx.guild else None
    if current_guild_id is None:
        # Outside a guild the checks above can't run at all -- refuse every link
        # rather than silently let permission checking be a no-op.
        logger.info(f"{ctx.author.name}: &remix invoked outside a guild, skipping {len(links)} message link(s)")
        return [], [f"Message links only work in a server channel — skipping {len(links)} link(s)."]

    ok_links = []
    outside_count = 0
    for link in links:
        # classify_link is the cheap pre-filter (see its docstring) -- "dm" and
        # "cross_guild" are combined into one S2 bucket here; the authoritative
        # guild check still runs again below against the RESOLVED channel.
        if message_links.classify_link(link, current_guild_id) == "ok":
            ok_links.append(link)
        else:
            outside_count += 1

    # The cap is applied here, BEFORE any network work, per spec -- but the S5
    # notice line itself is deferred and assembled (along with S2-S4/S6) after the
    # resolution loop below, via message_links.format_skip_notes, so the final
    # note order is S2 -> S3 -> S4 -> S5 -> S6 regardless of the order these
    # counts are computed in.
    kept, dropped = message_links.limit_links(ok_links)

    linked_attachments = []
    unfetchable = 0
    no_image = 0
    truncated = False
    for link in kept:
        try:
            channel = bot.get_channel(link.channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(link.channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(f"&remix link {link.raw}: couldn't resolve channel {link.channel_id}: {e!r}")
                    unfetchable += 1
                    continue

            # Authoritative check (a) -- re-derive the guild from the RESOLVED
            # channel object, never from the URL (see the docstring above).
            guild = getattr(channel, "guild", None)
            same_guild = guild is not None and guild.id == ctx.guild.id
            if not same_guild:
                logger.warning(f"&remix link {link.raw}: resolved channel {link.channel_id}'s guild != invoking guild")
                unfetchable += 1
                continue

            # Authoritative check (b) -- the INVOKING user's own permissions on
            # the resolved channel, not the bot's. view_channel is required
            # alongside read_message_history because Discord's own semantics
            # make history unreadable without view, and checking both fails
            # closed if either is denied via a permission override.
            perms = channel.permissions_for(ctx.author)
            if not (perms.view_channel and perms.read_message_history):
                logger.warning(
                    f"&remix link {link.raw}: {ctx.author.name} lacks view_channel/"
                    f"read_message_history on channel {link.channel_id}"
                )
                unfetchable += 1
                continue

            # discord.py's Thread.permissions_for ignores private-thread
            # membership entirely (it just delegates to the parent channel), so
            # the check above is not authoritative for a private thread -- see
            # message_links.needs_thread_membership_check's docstring. Close that
            # gap with an explicit membership fetch before trusting `perms`.
            is_private_thread = isinstance(channel, discord.Thread) and channel.is_private()
            if message_links.needs_thread_membership_check(is_private_thread, perms.manage_threads):
                try:
                    await channel.fetch_member(ctx.author.id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(
                        f"&remix link {link.raw}: {ctx.author.name} is not a member of "
                        f"private thread {link.channel_id}: {e!r}"
                    )
                    unfetchable += 1
                    continue

            try:
                msg = await channel.fetch_message(link.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f"&remix link {link.raw}: couldn't fetch message {link.message_id}: {e!r}")
                unfetchable += 1
                continue

            images = [a for a in msg.attachments if (a.content_type or "").startswith("image/")]
            if not images:
                no_image += 1
                continue
            for a in images:
                if len(linked_attachments) >= message_links.MAX_LINKS:
                    truncated = True
                    break
                linked_attachments.append(a)
        except Exception as e:
            # One bad link must never crash &remix -- bucket it the same as any
            # other unresolvable link; S3 deliberately doesn't distinguish why.
            logger.warning(f"&remix link {link.raw}: unexpected error resolving it: {e!r}")
            unfetchable += 1

    notes = message_links.format_skip_notes(
        outside_guild=outside_count,
        unfetchable=unfetchable,
        no_image=no_image,
        kept=len(kept),
        over_cap=len(dropped),
        truncated=truncated,
    )

    logger.info(
        f"{ctx.author.name}: &remix message links found={len(links)} kept={len(kept)} "
        f"images={len(linked_attachments)} outside_guild={outside_count} "
        f"over_cap={len(dropped)} unfetchable={unfetchable} no_image={no_image}"
    )
    return linked_attachments, notes


async def _remix_once(ctx, raw):
    """&remix's body -- flags are parsed here rather than via the shared
    _prep_generation_size because remix's size resolution differs by which path it
    ends up on below (edit vs. generation-fallback), and it must still work with no
    prompt at all (image-only remix, `raw` is None).

    Pasted Discord message links are stripped out of `raw` FIRST, before size
    flags/macros/magic ever see the text (see message_links.strip_message_links) --
    their images are resolved and appended to `attachments` LAST, after own
    attachments and reply-image attachments, so a directly attached image stays
    attachments[0] and keeps driving the edit size below (a pasted link must never
    silently change the output size of a remix whose subject was the user's own
    attachment). See _resolve_linked_images for the two authoritative security
    checks a linked image must pass before its bytes are ever fetched."""
    links = []
    if raw:
        raw, links = message_links.strip_message_links(raw)
        raw = raw or None

    prompt = raw
    orientation, res_wh = None, None
    if prompt:
        text, orientation, res_raw = image_size.parse_size_flags(prompt)
        if res_raw is not None:
            try:
                res_wh = image_size.parse_resolution(res_raw)
            except ValueError:
                await ctx.send(
                    f"`--res {res_raw}` isn't a size I understand — use `WIDTHxHEIGHT`, "
                    f"e.g. `--res 1920x1080`."
                )
                return False
        # Stripping flags can empty the prompt (e.g. "&remix --landscape" on an
        # attached image) -- fall back to None so the default "reinterpret this
        # image" path below still runs, while the parsed size flags are still honored.
        prompt = text.strip() or None

    attachments = [a for a in ctx.message.attachments if (a.content_type or "").startswith("image/")]
    skipped = len(ctx.message.attachments) - len(attachments)
    if skipped:
        await ctx.send(f"Skipping {skipped} attachment(s) that aren't images.")

    if ctx.message.reference:
        try:
            ref_msg = ctx.message.reference.resolved
            if not isinstance(ref_msg, discord.Message):
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            attachments += [a for a in ref_msg.attachments if (a.content_type or "").startswith("image/")]
        except (discord.NotFound, discord.HTTPException):
            pass

    # Linked images are resolved (and all their network fetching completed) here,
    # BEFORE send_quote runs on either branch below -- the requester isn't left
    # staring at a quote while &remix is still off fetching messages.
    linked, notes = await _resolve_linked_images(ctx, links)
    if notes:
        await ctx.send("\n".join(notes))
    attachments += linked

    if not attachments:
        if not prompt:
            await ctx.send("We need a happy little image to work with before we can remix anything. Attach one, or reply to a message that has one!")
            return False
        prompt = await expand_prompt_macros(ctx, prompt)
        prompt, magic = maybe_apply_magic_paint(prompt)
        size, requested = image_size.resolve_generation_size(orientation, res_wh)
        if res_wh and orientation:
            await ctx.send(f"(`--res` overrides `--{orientation}`)")
        if requested and requested != size:
            await ctx.send(f"Using `{size}` (adjusted from `{requested}` to fit the size limits).")
        await send_quote(ctx, magic)
        return await do_the_art(ctx, prompt, "remix", IMAGE_MODEL, size=size)

    size, requested = image_size.resolve_edit_size(
        orientation, res_wh, attachments[0].width, attachments[0].height
    )
    if res_wh and orientation:
        await ctx.send(f"(`--res` overrides `--{orientation}`)")
    if requested and requested != size:
        await ctx.send(f"Using `{size}` (adjusted from `{requested}` to fit the size limits).")
    logger.info(
        f"Remix size: {attachments[0].width}x{attachments[0].height} -> {size} "
        f"({image_size.describe_edit_size(size)})"
    )

    images = [(await a.read(), a.content_type) for a in attachments]
    if prompt:
        prompt = await expand_prompt_macros(ctx, prompt)
        prompt, magic = maybe_apply_magic_paint(prompt)
    else:
        prompt = _apply_random_magic_entry("creatively reinterpret this image")
        magic = True

    await send_quote(ctx, magic)
    return await do_the_art(ctx, prompt, "remix", IMAGE_MODEL, images=images, size=size)


async def _release_image_once(ctx, raw):
    """&release_image's body, verbatim, including its empty-args and
    unknown-version early returns."""
    if not raw or not raw.strip():
        await ctx.send("Give me a git hash or any text to immortalize as a release image...")
        return False
    source, version, georgify = release_image.parse_release_args(raw)
    if not source:
        await ctx.send("...I need something to hash besides the flags.")
        return False
    try:
        prompt, seed, ver = release_image.build_release_prompt(source, version, georgify)
    except KeyError:
        available = ", ".join(sorted(release_image.RELEASE_ALGORITHMS, key=lambda v: int(v)))
        await ctx.send(f"Unknown algorithm version. Available: {available}")
        return False
    # Release images are deliberately NOT subject to magic paint, so send a plain quote.
    await send_quote(ctx)
    george = " | 🥸 George mode" if georgify else ""
    await ctx.send(f"Release image for `{source}` | seed {seed} | algo v{ver}{george}\n**Prompt**: {prompt}")
    return await do_the_art(ctx, prompt, "release_image", IMAGE_MODEL)


# --- Pipe-chain runner -------------------------------------------------------------
#
# Shared by all eight piped commands. Ordering guarantee, preserved throughout: pipe
# splitting is the FIRST thing done to the raw prompt (before size flags, macros, or
# magic paint ever see it), and each individual segment is then processed in the same
# size-flags -> macros -> magic order a plain single command always has been.

async def _piped(ctx, raw, first_runner, request_type, model, magic_mode):
    """Entry point for every pipe-capable command. Splits `raw` on '|' before
    anything else touches it -- splitting after macro expansion would let a library
    macro whose text happens to contain '|' inject extra pipeline steps; splitting
    first makes that impossible, since macro/magic substitution only ever happens
    WITHIN an already-split segment.

    `raw is None` only ever happens for &remix (a bare image-only remix, no text at
    all) -- there's nothing to split, so it runs first_runner directly, once, exactly
    like today.
    """
    if raw is None:
        await first_runner(ctx, None)
        return

    segments, dropped, error = pipe_chain.split_pipeline(raw)
    if error == "too_many":
        # Load-bearing: this must be the ONLY message sent for this invocation --
        # no dropped note, no quote, nothing else.
        await ctx.send(pipe_chain.TOO_MANY_MESSAGE)
        return
    if error == "empty":
        await ctx.send("...I need something to paint besides the pipes.")
        return
    if dropped:
        await ctx.send(pipe_chain.dropped_note(dropped))

    if len(segments) == 1:
        # No real chain: with no '|' at all, segments[0] is `raw` verbatim, so this
        # is byte-for-byte the pre-pipes code path for the overwhelming majority of
        # invocations.
        await first_runner(ctx, segments[0])
        return

    # A real (>=2 segment) chain: bracket the WHOLE run with active_requests so a
    # shutdown drain waits for every segment, not just whichever one happens to be
    # running when the signal arrives -- do_the_art's own bracket only covers one
    # segment at a time, leaving a gap BETWEEN segments where active_requests could
    # read 0 mid-chain and let a drain close the bot early. Nesting with do_the_art's
    # own increment is harmless (the counter simply reaches 2 mid-segment).
    global active_requests
    active_requests += 1
    try:
        await _run_chain(ctx, segments, first_runner, request_type, model, magic_mode)
    finally:
        active_requests -= 1


async def _run_chain(ctx, segments, first_runner, request_type, model, magic_mode):
    """Run a real chain: segment 1 via `first_runner` (identical to today's un-piped
    command), every later segment as an edit of the previous segment's output via
    _pipe_edit_once. Aborts with "Chain stopped at step N of M" on the first failure
    (prior images stay posted, and the pipes/pipe_segments counters reflect only what
    actually completed); posts "Chain complete" and bumps `pipes` only if every step
    succeeds. Deliberately does NOT check `draining` between segments -- the
    active_requests bracket in _piped is what the raised DRAIN_TIMEOUT exists to
    cover for a chain already in flight."""
    total = len(segments)  # the post-drop count, so "step N of M" numbering is honest

    result = await first_runner(ctx, segments[0])
    if not result:
        await ctx.send(pipe_chain.chain_stopped_message(1, total))
        return
    _bump_stat('pipe_segments')
    anchor = result.message
    prev_bytes = result.image_bytes

    for step, text in enumerate(segments[1:], start=2):
        result = await _pipe_edit_once(ctx, text, prev_bytes, anchor, model, magic_mode)
        if not result:
            await ctx.send(pipe_chain.chain_stopped_message(step, total))
            return
        _bump_stat('pipe_segments')
        prev_bytes = result.image_bytes
        # `anchor` is deliberately never reassigned -- every chained image replies
        # to the FIRST image (a fixed anchor), so the chain stays traversable
        # backward from any link, not just from its immediate predecessor.

    _bump_stat('pipes')
    await ctx.send(pipe_chain.chain_complete_message(total))


async def _pipe_edit_once(ctx, raw, prev_bytes, anchor, model, magic_mode):
    """Every non-first pipe segment is an edit of the previous segment's output,
    regardless of which command started the chain -- &dpaint's later segments still
    edit via gpt-image-2 (do_the_art already routes edits through get_edit_model,
    which falls back off dall-e-3 since it has no edit endpoint). Same per-segment
    order as a single command: size flags -> macros -> magic."""
    text, orientation, res_raw = image_size.parse_size_flags(raw)

    res_wh = None
    if res_raw is not None:
        try:
            res_wh = image_size.parse_resolution(res_raw)
        except ValueError:
            await ctx.send(
                f"`--res {res_raw}` isn't a size I understand — use `WIDTHxHEIGHT`, "
                f"e.g. `--res 1920x1080`."
            )
            return False

    prompt = text.strip()
    if prompt:
        # Non-empty segment text is an explicit, deliberate instruction the
        # requester typed -- macro echo/🎲-miss lines stay enabled even on a quiet
        # segment, same transparency rationale as expand_prompt_macros' docstring.
        prompt = await expand_prompt_macros(ctx, prompt)
    else:
        # A segment that's flags-only (e.g. "... | --portrait") -- the same literal
        # fallback &remix uses for an image with no guiding text at all. Not run
        # through macro expansion; there's no user text to expand.
        prompt = "creatively reinterpret this image"

    magic = False
    if magic_mode == "roll":
        prompt, magic = maybe_apply_magic_paint(prompt)
    elif magic_mode == "always":
        prompt = _apply_random_magic_entry(prompt)
        magic = True
    # magic_mode == "none": never applies magic, matching &hpaint/&mpaint/&lpaint/
    # &dpaint/&release_image's segment-1 stance carried through to their chain edits.

    # Size off the PREVIOUS segment's own PNG output, not anything threaded forward
    # from segment 1 -- a first-segment &remix that resolved to AUTO has no size to
    # thread, but the output PNG's own header always does.
    dims = image_size.png_dimensions(prev_bytes) or (None, None)
    size, requested = image_size.resolve_edit_size(orientation, res_wh, dims[0], dims[1])
    if res_wh and orientation:
        await ctx.send(f"(`--res` overrides `--{orientation}`)")
    if requested and requested != size:
        await ctx.send(f"Using `{size}` (adjusted from `{requested}` to fit the size limits).")

    result = await do_the_art(
        ctx, prompt, "pipe", model, images=[(prev_bytes, "image/png")], size=size,
        reply_to=anchor, content=("🖌️" if magic else None), quiet=True,
    )
    if result and magic:
        # Count at reveal time, same semantic send_quote uses: on a quiet segment
        # the 🖌️ content IS the reveal, and it only exists once the send succeeds.
        _bump_magic_counter()
    return result


@bot.command(name='paint', help='Paint a picture based on a prompt. Flags: --landscape/--portrait/--square, --res WxH. monthly limit Chain follow-up edits with | (up to 5 steps).')
async def paint(ctx, *, prompt):
    await _piped(ctx, prompt, lambda c, t: _paint_once(c, t, "paint", IMAGE_MODEL, "roll"),
                 "paint", IMAGE_MODEL, "roll")


@bot.command(name='hpaint', help='Paint a high quality picture with gpt-image-2. Flags: --landscape/--portrait/--square, --res WxH. monthly limit Chain follow-up edits with | (up to 5 steps).')
async def hpaint(ctx, *, prompt):
    await _piped(ctx, prompt, lambda c, t: _paint_once(c, t, "hpaint", "gpt-image-2", "none"),
                 "hpaint", "gpt-image-2", "none")


@bot.command(name='mpaint', help='Paint a medium quality picture with gpt-image-2. Flags: --landscape/--portrait/--square, --res WxH. monthly limit Chain follow-up edits with | (up to 5 steps).')
async def mpaint(ctx, *, prompt):
    await _piped(ctx, prompt, lambda c, t: _paint_once(c, t, "mpaint", "gpt-image-2-medium", "none"),
                 "mpaint", "gpt-image-2-medium", "none")


@bot.command(name='lpaint', help='Paint a low quality picture with gpt-image-2. Flags: --landscape/--portrait/--square, --res WxH. monthly limit Chain follow-up edits with | (up to 5 steps).')
async def lpaint(ctx, *, prompt):
    await _piped(ctx, prompt, lambda c, t: _paint_once(c, t, "lpaint", "gpt-image-2-low", "none"),
                 "lpaint", "gpt-image-2-low", "none")


@bot.command(name='dpaint', help='Paint with DALL-E 3. monthly limit Chain follow-up edits with | (up to 5 steps).')
async def dpaint(ctx, *, prompt):
    await _piped(ctx, prompt, _dpaint_once, "dpaint", "dall-e-3", "none")


# Hidden always-on variant of &paint. Named xpaint (not mpaint) since &mpaint is
# already the medium-quality command. Not listed in help; the addition is never revealed.
@bot.command(name='xpaint', help='Paint a picture, with a little extra magic. Flags: --landscape/--portrait/--square, --res WxH. Chain follow-up edits with | (up to 5 steps).', hidden=True)
async def xpaint(ctx, *, prompt):
    await _piped(ctx, prompt, lambda c, t: _paint_once(c, t, "xpaint", IMAGE_MODEL, "always"),
                 "xpaint", IMAGE_MODEL, "always")


@bot.command(name='remix', help='Remix an image with a prompt. Attach an image, reply to one, paste a message link from this server, or any mix — and add a prompt to guide the transformation. Flags: --landscape/--portrait/--square, --res WxH (coerced to a valid size, same as &paint). Falls back to painting if no image is found. Monthly limit applies. Chain follow-up edits with | (up to 5 steps).')
async def remix(ctx, *, prompt=None):
    await _piped(ctx, prompt, _remix_once, "remix", IMAGE_MODEL, "roll")


@bot.command(name='release_image', help='Generate a deterministic release avatar from a git hash (or any text) — same input always yields the same prompt. Flags: --george, --vN. Monthly limit applies. Chain follow-up edits with | (up to 5 steps).')
async def release_image_cmd(ctx, *, args=None):
    await _piped(ctx, args, _release_image_once, "release_image", IMAGE_MODEL, "none")


@bot.command(name='magic_list', help='List the magic mixin ids and authors. Use &magic_show to read a prompt, &magic_update to change it.')
async def magic_list(ctx):
    entries = _load_magic_library()
    if not entries:
        await ctx.send("The magic library is empty.")
        return
    lines = ["Use `&magic_show <id>` to see a mixin's prompt, `&magic_update <id> <text>` to change it."]
    for entry in entries:
        lines.append(f"`{entry.get('id', '?')}` (by {entry.get('author', 'built-in')})")
    await send_long(ctx, "\n".join(lines))


@bot.command(name='magic_show', help='Show the full text of a magic mixin by id (see &magic_list).')
async def magic_show(ctx, entry_id=None):
    if not entry_id:
        await ctx.send("Which one? `&magic_show <id>` — see `&magic_list` for ids.")
        return
    entries = _load_magic_library()
    entry = next((e for e in entries if e.get("id") == entry_id), None)
    if not entry:
        await ctx.send(f"No entry with id `{entry_id}`.")
        return
    lines = [
        f"`{entry.get('id')}` — {entry.get('text', '')}",
        f"Author: {entry.get('author', 'built-in')} | Added: {entry.get('added', '—')}",
    ]
    if entry.get("editor"):
        lines.append(f"Last edited by: {entry['editor']} on {entry.get('edited', '—')}")
    await send_long(ctx, "\n".join(lines))


@bot.command(name='magic_update', help="Update a magic mixin's text in place by id (see &magic_list). Records you as editor.")
async def magic_update(ctx, entry_id=None, *, text=None):
    if not entry_id or not text or not text.strip():
        await ctx.send("Usage: `&magic_update <id> <new text>` — see `&magic_list` for ids.")
        return
    text = text.strip()
    entries = _load_magic_library()
    entry = next((e for e in entries if e.get("id") == entry_id), None)
    if not entry:
        await ctx.send(f"No entry with id `{entry_id}`.")
        return
    entry["text"] = text
    entry["editor"] = ctx.author.name
    entry["edited"] = date.today().isoformat()
    _save_magic_library(entries)
    await ctx.send(f"Updated magic mixin `{entry_id}`.")


@bot.command(name='magic_add', help='Add a magic mixin. The text is appended to prompts when magic fires.')
async def magic_add(ctx, *, text=None):
    if not text or not text.strip():
        await ctx.send("Give me some happy little text to add, like `&magic_add In the background, a squirrel juggles acorns.`")
        return
    text = text.strip()
    entries = _load_magic_library()
    existing_ids = {e.get("id") for e in entries}
    new_id = magic_paint.slugify_magic_id(text, existing_ids)
    entries.append({
        "id": new_id,
        "text": text,
        "author": ctx.author.name,
        "added": date.today().isoformat(),
    })
    _save_magic_library(entries)
    await ctx.send(f"Added magic mixin `{new_id}`. Remove it with `&magic_remove {new_id}`.")


@bot.command(name='magic_remove', help='Remove a magic mixin by id (see &magic_list).')
async def magic_remove(ctx, entry_id=None):
    if not entry_id:
        await ctx.send("Which one? `&magic_remove <id>` — see `&magic_list` for ids.")
        return
    entries = _load_magic_library()
    remaining = [e for e in entries if e.get("id") != entry_id]
    if len(remaining) == len(entries):
        await ctx.send(f"No entry with id `{entry_id}`.")
        return
    _save_magic_library(remaining)
    note = " The library is now empty — magic paint will do nothing until you add more." if not remaining else ""
    await ctx.send(f"Removed magic mixin `{entry_id}`.{note}")


@bot.command(name='magic_rate', help='Show or set the magic paint rate. e.g. &magic_rate, &magic_rate 10, &magic_rate .1, &magic_rate 10%, &magic_rate .1%')
async def magic_rate(ctx, value=None):
    global MAGIC_PAINT_RATE
    if value is None:
        data = load_data()
        history = data.get('magic_rate_history', [])
        msg = f"Magic rate: {format_magic_rate(MAGIC_PAINT_RATE)}"
        if history:
            last = history[-1]
            when = format_rate_change_time(last['time'])
            msg += f"\nLast changed by {last['user']} on {when}"
            if len(history) > 1:
                recent = "\n".join(
                    f"  {format_rate_change_time(h['time'])} — {format_magic_rate(h['rate'])} by {h['user']}"
                    for h in history[-5:]
                )
                msg += f"\nRecent changes:\n{recent}"
        await ctx.send(msg)
        return
    try:
        rate = parse_magic_rate(value)
    except ValueError:
        await ctx.send("Couldn't read that rate. Try `10`, `.1`, `10%`, or `.1%` (values map to a 0–100% chance).")
        return
    MAGIC_PAINT_RATE = rate
    _record_rate_change(ctx.author.name, rate)
    await ctx.send(f"Magic rate set to {format_magic_rate(rate)}.")


@bot.command(name='macro_list', help='List the ;macro ids and authors. Use &macro_show to read one, &macro_update to change it.')
async def macro_list(ctx):
    entries = _load_macro_library()
    if not entries:
        await ctx.send("The macro library is empty.")
        return
    lines = ["Use `&macro_show <id>` to see a macro's full text, `&macro_update <id> <text>` to change it."]
    for entry in entries:
        if not isinstance(entry, dict):
            continue  # tolerate a hand-corrupted library row rather than crash the listing
        text = entry.get('text', '') or ''
        preview = text if len(text) <= 60 else text[:60].rstrip() + "…"
        lines.append(f"`;{entry.get('id', '?')}` — {preview} (by {entry.get('author', 'built-in')})")
    await send_long(ctx, "\n".join(lines))


@bot.command(name='macro_show', help='Show the full text of a ;macro by id (see &macro_list).')
async def macro_show(ctx, entry_id=None):
    if not entry_id:
        await ctx.send("Which one? `&macro_show <id>` — see `&macro_list` for ids.")
        return
    entry_id = macros.normalize_macro_id(entry_id)
    entries = _load_macro_library()
    entry = next((e for e in entries if macros.entry_id(e) == entry_id), None)
    if not entry:
        await ctx.send(f"No macro with id `;{entry_id}`.")
        return
    lines = [
        f"`;{entry.get('id')}` — {entry.get('text', '')}",
        f"Author: {entry.get('author', 'built-in')} | Added: {entry.get('added', '—')}",
    ]
    if entry.get("editor"):
        lines.append(f"Last edited by: {entry['editor']} on {entry.get('edited', '—')}")
    await send_long(ctx, "\n".join(lines))


@bot.command(name='macro_add', help='Add a ;macro. &macro_add <id> <text> — the id is what you type as ;<id> in a prompt.')
async def macro_add(ctx, macro_id=None, *, text=None):
    if not macro_id or not text or not text.strip():
        await ctx.send("Usage: `&macro_add <id> <text>`, e.g. `&macro_add lasso a cowboy twirling a glowing lasso,`")
        return
    normalized_id = macros.normalize_macro_id(macro_id)
    if not macros.is_valid_macro_id(normalized_id):
        await ctx.send("Macro ids must be 1-32 characters: lowercase letters, digits, `_`, or `-`.")
        return
    text = text.strip()
    entries = _load_macro_library()
    if any(macros.entry_id(e) == normalized_id for e in entries):
        await ctx.send(f"`;{normalized_id}` already exists. Use `&macro_update {normalized_id} <text>` to change it.")
        return
    entries.append({
        "id": normalized_id,
        "text": text,
        "author": ctx.author.name,
        "added": date.today().isoformat(),
    })
    _save_macro_library(entries)
    await ctx.send(f"Added macro `;{normalized_id}`. Remove it with `&macro_remove {normalized_id}`.")


@bot.command(name='macro_update', help="Update a ;macro's text in place by id (see &macro_list). Records you as editor.")
async def macro_update(ctx, entry_id=None, *, text=None):
    if not entry_id or not text or not text.strip():
        await ctx.send("Usage: `&macro_update <id> <new text>` — see `&macro_list` for ids.")
        return
    normalized_id = macros.normalize_macro_id(entry_id)
    text = text.strip()
    entries = _load_macro_library()
    entry = next((e for e in entries if macros.entry_id(e) == normalized_id), None)
    if not entry:
        await ctx.send(f"No macro with id `;{normalized_id}`.")
        return
    entry["text"] = text
    entry["editor"] = ctx.author.name
    entry["edited"] = date.today().isoformat()
    _save_macro_library(entries)
    await ctx.send(f"Updated macro `;{normalized_id}`.")


@bot.command(name='macro_remove', help='Remove a ;macro by id (see &macro_list).')
async def macro_remove(ctx, entry_id=None):
    if not entry_id:
        await ctx.send("Which one? `&macro_remove <id>` — see `&macro_list` for ids.")
        return
    normalized_id = macros.normalize_macro_id(entry_id)
    entries = _load_macro_library()
    remaining = [e for e in entries if macros.entry_id(e) != normalized_id]
    if len(remaining) == len(entries):
        await ctx.send(f"No macro with id `;{normalized_id}`.")
        return
    _save_macro_library(remaining)
    note = " The macro library is now empty — ;tokens will always miss until you add more." if not remaining else ""
    await ctx.send(f"Removed macro `;{normalized_id}`.{note}")


async def do_the_art(ctx, prompt, request_type, model, images=None, size=None,
                      reply_to=None, content=None, quiet=False):
    # `size` (see image_size.py) is now forwarded on BOTH paths below: fetch_image_edit
    # (images given -- &remix with an attachment, or a pipe chain's edit segments) and
    # fetch_image (generation -- &paint/&hpaint/&mpaint/&lpaint/&xpaint honoring
    # --res/--landscape/--portrait/--square). None keeps each path's own model-config
    # default size. &dpaint/&meme/&release_image never pass size, so they stay
    # unaffected.
    #
    # `reply_to` (a discord.Message, or None) threads the posted image as a Discord
    # reply to that message -- used by pipe chains so every segment past the first
    # replies to the FIRST image, making the chain traversable backward from any
    # link. `content` is optional text for the image message itself -- used by a
    # quiet pipe segment to surface a bare 🖌️ magic tell with no quote message to
    # attach it to.
    #
    # `quiet` is used by the daily scheduler (request_type "daily_image"/"daily_edit")
    # and by pipe-chain edit segments (request_type "pipe"): it suppresses the
    # "Generated in ... | Monthly requests: ..." trailer, and swaps the exception-path
    # message for a prompt-free one -- a quiet caller's prompt may carry a hidden
    # magic mixin, and the normal failure line echoes the full prompt, which would
    # spoil the gag. The over-limit message and the "Revised prompt" message are NOT
    # gated by `quiet` (the daily/pipe models are gpt-image-2, which never has a
    # revised_prompt anyway).
    #
    # Every image command funnels through here, so bracketing the whole body with the
    # active-request counter is what lets a shutdown drain in-flight work (see
    # _graceful_shutdown); the finally guarantees the count is released on every path.
    global active_requests
    active_requests += 1
    try:
        logger.info(f"Received {request_type} request from {ctx.author.name} using {model} to paint: {prompt}")
        current_month = get_current_month()
        data = load_data()
        if over_limit(data):
            await ctx.send("Monthly limit reached. Please wait until next month to make more paint requests.")
            return False

        # Under quiet=True (the scheduler, or a pipe segment), the prompt must never
        # be posted -- not even indirectly as the attachment filename or alt text,
        # since it may carry a hidden magic mixin the announcement's bare tell is
        # meant to keep secret. So quiet callers get a neutral, prompt-free filename
        # and drop the alt-text description entirely unless OpenAI itself supplied a
        # revised_prompt (which never happens for the gpt-image-2 family the
        # scheduler/pipes use, but is honored here for correctness).
        # Neutral/feature-agnostic name (not "daily_image_...") -- quiet=True covers
        # both the daily scheduler and chained pipe segments.
        file_name = generate_file_name(prompt) if not quiet else f"painting_{int(time.time())}.png"

        try:
            t0 = time.monotonic()
            if images:
                response = await fetch_image_edit(prompt, get_edit_model(model), images, size=size)
            else:
                response = await fetch_image(prompt, model, size=size)
            elapsed = time.monotonic() - t0
            image_data = base64.b64decode(response['image'])
            if quiet:
                description = response['revised_prompt'][:1024] if response['revised_prompt'] else None
            else:
                description = (response['revised_prompt'] or prompt)[:1024]

            send_kwargs = {"file": discord.File(io.BytesIO(image_data), file_name, description=description)}
            if content is not None:
                send_kwargs["content"] = content
            if reply_to is not None:
                send_kwargs["reference"] = reply_to
            try:
                sent = await ctx.send(**send_kwargs)
            except discord.HTTPException:
                # A deleted anchor message resolves as Discord 400 "reference_unknown".
                # Only retry the reference case -- with reply_to None this is the
                # existing (pre-pipes) behavior of letting HTTPException propagate to
                # the generic handler below. The discord.File above already consumed
                # its BytesIO on the failed attempt, so it must be rebuilt from
                # scratch, not reused (a reused, exhausted BytesIO would send a
                # 0-byte file the second time).
                if reply_to is None:
                    raise
                send_kwargs.pop("reference")
                send_kwargs["file"] = discord.File(io.BytesIO(image_data), file_name, description=description)
                sent = await ctx.send(**send_kwargs)

            if response['revised_prompt']:
                await ctx.send(f"**Revised prompt**: {response['revised_prompt']}")
            # reload the data for the increment since we are async
            data = load_data()
            if current_month not in data:
                data[current_month] = 0
            data[current_month] += 1
            if request_type == "remix":
                data['remixes'] = data.get('remixes', 0) + 1
            if request_type == "release_image":
                data['release_images'] = data.get('release_images', 0) + 1
            if request_type == "daily_image":
                data['daily_images'] = data.get('daily_images', 0) + 1
            if request_type == "daily_edit":
                data['daily_edits'] = data.get('daily_edits', 0) + 1
            save_data(data)
            if not quiet:
                await ctx.send(f"Generated in {format_duration(elapsed)} | Monthly requests: {data[current_month]}")
            return pipe_chain.ArtResult(sent, image_data, size, elapsed)
        except Exception as e:
            if quiet:
                await ctx.send(f"No painting this time, exception for this request: {e}")
            else:
                await ctx.send(f"No painting for: {prompt}, exception for this request: {e}")
            return False
    finally:
        active_requests -= 1


def get_edit_model(model):
    config = MODEL_CONFIGS.get(model, MODEL_CONFIGS["gpt-image-2"])
    return model if config.get("supports_edit") else "gpt-image-2"


async def _classify_image_error(response, prompt):
    """Shared by fetch_image and fetch_image_edit. Returns ('retry'|'safety'|'stop', error_message)."""
    error_json = await response.json()
    error_message = error_json.get("error", {}).get("message") or str(error_json)
    if response.status == 400:
        logger.info(f"Request: {prompt} Safety Violation.")
        data = load_data()
        if 'safety_trips' not in data:
            data['safety_trips'] = 0
        data['safety_trips'] += 1
        save_data(data)
        return 'safety', error_message
    elif response.status in [429, 500, 503]:
        logger.error(f"Request: {prompt} Trying again. Error: {response.status} {error_message}")
        return 'retry', error_message
    else:
        logger.error(f"Request: {prompt} Error: {response.status}: {error_message}")
        return 'stop', error_message


async def fetch_image(prompt, model, size=None):
    """`size`, when given, overrides the model config's default generation size (see
    image_size.py -- used by &paint/&hpaint/&mpaint/&lpaint/&xpaint for
    --res/--landscape/--portrait/--square); None keeps today's behavior of always
    using the model config's configured size. Copies config["params"] into a local
    dict before any override so the shared MODEL_CONFIGS entry is never mutated."""
    config = MODEL_CONFIGS.get(model, MODEL_CONFIGS["gpt-image-2"])
    params = dict(config["params"])
    if size is not None:
        params["size"] = size
    payload = {
        "model": config["model"],
        "prompt": prompt,
        "n": 1,
        "user": "bot_ross",
        **params,
    }
    if config["supports_moderation"]:
        payload["moderation"] = IMAGE_MODERATION

    async with aiohttp.ClientSession() as session:
        for _ in range(2):
            async with session.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json=payload,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Request: {prompt} Success")
                    item = data["data"][0]
                    revised = item.get("revised_prompt") if config["has_revised_prompt"] else None
                    return {"image": item["b64_json"], "revised_prompt": revised}
                verdict, error_message = await _classify_image_error(response, prompt)
                if verdict == 'safety':
                    break
                if verdict == 'retry':
                    await asyncio.sleep(5)

        raise Exception(f"response: {response.status}: {error_message}")


def _image_extension(content_type):
    return (content_type or "image/png").split("/")[-1].split(";")[0] or "png"


async def fetch_image_edit(prompt, model, images, size=None):
    """images: list[(bytes, content_type)] of raw image content and its Discord-reported
    content type (e.g. from discord.Attachment.read()/.content_type).
    `size`, when given, overrides the model config's default edit size (see
    image_size.py, used by &remix to match the first input image's orientation);
    None keeps today's behavior of always sending the model config's configured size.
    Returns {"image": b64, "revised_prompt": None} — the edits endpoint has no revised_prompt."""
    config = MODEL_CONFIGS.get(model, MODEL_CONFIGS["gpt-image-2"])
    async with aiohttp.ClientSession() as session:
        for _ in range(2):
            form = aiohttp.FormData()  # rebuilt every attempt: FormData is single-use
            form.add_field("model", config["model"])
            form.add_field("prompt", prompt)
            form.add_field("n", "1")
            form.add_field("user", "bot_ross")
            size_value = size if size is not None else config["params"].get("size")
            if size_value:
                form.add_field("size", size_value)
            quality = config["params"].get("quality")
            if quality:
                form.add_field("quality", quality)
            if config["supports_moderation"]:
                form.add_field("moderation", IMAGE_MODERATION)
            for i, (img_bytes, content_type) in enumerate(images):
                ext = _image_extension(content_type)
                form.add_field("image[]", img_bytes, filename=f"image_{i}.{ext}",
                                content_type=content_type or "image/png")

            async with session.post(
                    "https://api.openai.com/v1/images/edits",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data=form,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Edit request: {prompt} Success (size={size_value})")
                    item = data["data"][0]
                    return {"image": item["b64_json"], "revised_prompt": None}
                verdict, error_message = await _classify_image_error(response, prompt)
                if verdict == 'safety':
                    break
                if verdict == 'retry':
                    await asyncio.sleep(5)

        raise Exception(f"response: {response.status}: {error_message}")


# --- Daily image-of-the-day scheduler -------------------------------------------------
#
# Thin wrappers around daily_schedule.py's pure logic. The loop wiring here is
# deliberately thin; everything testable about time/DST/retention lives in
# daily_schedule.py (see test_daily_schedule.py). Every actual image call still funnels
# through do_the_art, so the scheduler inherits over_limit, the monthly counter,
# safety-trip accounting, and the active_requests drain bracket for free.

FAILURE_MESSAGE = "My paint brush hit me with a :circlegame: sorry nothing to see here"


class _ChannelContext:
    """Minimal stand-in for a discord.ext.commands.Context: just enough for
    do_the_art (which only ever touches ctx.send and ctx.author.name) to run against
    a plain channel instead of a real command invocation, so the scheduler can reuse
    do_the_art unchanged."""

    def __init__(self, channel):
        self.channel = channel
        self.author = types.SimpleNamespace(name="daily_schedule")

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


def _log_daily_task_result(task):
    """Done-callback for the scheduler's background task. A bare asyncio.create_task()
    return value with nothing holding a reference can be garbage-collected mid-flight,
    and a loop that raises out of its own try/except would otherwise die silently --
    log the exception (if any) so a dead scheduler shows up in the logs instead of just
    quietly not posting anymore."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"Daily scheduler task died: {exc!r}", exc_info=exc)


async def _daily_scheduler_loop():
    """Per-minute tick. Sleeps to the next wall-clock minute boundary (recomputed from
    BOT_ZONE's current time every iteration, so it self-corrects after an NTP step or
    host suspend within one minute), then runs whatever slots are due. One bad tick
    must never kill the heartbeat, so the tick body has its own try/except.

    Announces itself once on start. Until this existed the success path was the ONLY
    path that logged nothing -- disabled, misconfigured and unset all logged, so a
    running scheduler and a scheduler that died before its first tick looked
    identical in `docker logs`, and confirming "will it post at 5?" meant reading the
    source. Logged from inside the task rather than beside create_task() so the line
    is evidence the coroutine actually started, not just that it was scheduled."""
    try:
        entries = daily_schedule.load_schedule(DAILY_SCHEDULE_FILE)
        good, _errors = daily_schedule.validate_schedule(entries)
        enabled = [e for e in good if daily_schedule.slot_is_enabled(e)]
        upcoming = daily_schedule.next_slot(datetime.now(timezone.utc), good, BOT_ZONE)
        if upcoming is None:
            when = "nothing scheduled"
        else:
            entry, instant = upcoming
            local = instant.astimezone(BOT_ZONE)
            countdown = format_duration((instant - datetime.now(timezone.utc)).total_seconds())
            slot_id = entry["id"]
            when = f"next: {slot_id} at {local:%H:%M %Z} on {local:%Y-%m-%d} (in {countdown})"
        # Built on its own line, not inlined into the f-string below: a nested
        # same-quoted f-string (f"...{','.join(f'{e['id']}' ...)}...") is PEP 701
        # syntax that only parses on 3.12+, and the container runs 3.10 -- so it would
        # pass every local check and then crash on deploy. See NoNestedFStringsTest.
        slot_list = ", ".join([e["id"] + " " + e["time"] for e in enabled])
        logger.info(
            f"Daily image scheduler started: zone={BOT_ZONE}, channel={DAILY_IMAGE_CHANNEL_ID}, "
            f"{len(enabled)} enabled slot(s) [{slot_list}]; {when}"
        )
    except Exception:
        # A broken schedule must not stop the heartbeat before it starts -- the tick
        # loop below re-reads and re-validates it every minute anyway.
        logger.exception("Daily scheduler: couldn't summarize the schedule at startup")

    while not draining:
        await asyncio.sleep(daily_schedule.seconds_to_next_minute(datetime.now(BOT_ZONE)))
        if draining:
            break
        try:
            await _run_due_daily_slots()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily scheduler tick failed")


async def _run_due_daily_slots():
    """One tick: load+validate the schedule fresh (never cached, matching the
    magic/macro libraries), find what's due, resolve the channel, and run each due
    slot in order."""
    global _last_schedule_errors
    entries = daily_schedule.load_schedule(DAILY_SCHEDULE_FILE)
    good, errors = daily_schedule.validate_schedule(entries)
    if errors != _last_schedule_errors:
        # Only log when the error set actually changes -- otherwise one bad entry in
        # data/daily_schedule.json would spam a warning every 60 seconds forever.
        for err in errors:
            logger.warning(f"Daily schedule problem: {err}")
        _last_schedule_errors = errors

    state = daily_schedule.load_state(DAILY_STATE_FILE)
    due = daily_schedule.due_slots(datetime.now(timezone.utc), good, state, BOT_ZONE)
    if not due:
        return

    try:
        channel = bot.get_channel(DAILY_IMAGE_CHANNEL_ID) or await bot.fetch_channel(DAILY_IMAGE_CHANNEL_ID)
    except (discord.HTTPException, discord.NotFound, discord.Forbidden) as e:
        # Deliberately do NOT mark anything fired here -- a transient Discord hiccup
        # must not permanently consume the day's slot; it stays eligible for the rest
        # of its MISS_WINDOW and the next tick(s) will retry channel resolution.
        logger.error(f"Daily scheduler: couldn't resolve channel {DAILY_IMAGE_CHANNEL_ID}: {e}")
        return

    for entry, day in due:
        if draining:
            return
        # Persist fired-state BEFORE running the slot: if the bot crashes mid-
        # generation, a restart must not re-fire it (which would re-spend API budget
        # and double-post the same slot).
        state = daily_schedule.mark_fired(state, entry["id"], day)
        daily_schedule.save_state(state, DAILY_STATE_FILE)
        try:
            await _run_daily_slot(channel, entry, day)
        except Exception:
            logger.exception(f"Daily scheduler: slot {entry.get('id')} failed")


async def _retry_delay():
    """Sleep the scheduler's 2-minute retry delay, checking `draining` both before
    and after so a shutdown mid-wait doesn't (a) block close() by sleeping through it,
    or (b) start a retry the drain is about to cut off anyway. Returns True if it's
    still safe to retry, False if draining started during (or before) the wait."""
    if draining:
        return False
    await asyncio.sleep(120)
    return not draining


async def _do_the_art_with_retry(ctx, prompt, request_type, model, **kwargs):
    """Run do_the_art (always quiet=True -- every scheduler call is), retrying once
    after a 2-minute delay if the first attempt failed. Returns the ArtResult on
    success, or False if both attempts failed (or draining cut the retry short).

    Skips the retry (and its 2-minute sleep) entirely when the first failure was
    the monthly cap: do_the_art already posted "Monthly limit reached..." once, and
    the retry would just hit the same cap and post the same message again, all
    while stalling the scheduler loop for 2 minutes for no benefit -- once
    API_LIMIT is reached this would otherwise repeat for every one of the day's
    remaining slots. _run_daily_slot still posts FAILURE_MESSAGE once afterward,
    so the caller-visible contract (one failure notice per failed slot) is
    unchanged; only the pointless second attempt goes away.
    """
    result = await do_the_art(ctx, prompt, request_type, model, quiet=True, **kwargs)
    if result:
        return result
    if over_limit(load_data()):
        return False
    if not await _retry_delay():
        return False
    return await do_the_art(ctx, prompt, request_type, model, quiet=True, **kwargs)


def _daily_image_path(day):
    return os.path.join(DAILY_IMAGES_DIR, daily_schedule.daily_image_filename(day))


def _read_daily_image(path):
    """Read a retained daily base PNG, or None if it doesn't exist / can't be read."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _save_daily_image(path, image_bytes):
    """Persist a retained daily base image, then prune down to DAILY_IMAGE_RETENTION
    newest. The image has already been posted to Discord by the time this runs, so
    disk trouble here must not fail the slot -- log it and move on."""
    try:
        with open(path, "wb") as f:
            f.write(image_bytes)
        for name in daily_schedule.select_images_to_prune(os.listdir(DAILY_IMAGES_DIR)):
            os.remove(os.path.join(DAILY_IMAGES_DIR, name))
    except OSError as e:
        logger.error(f"Daily scheduler: failed to save/prune retained image at {path}: {e}")


async def _run_daily_slot(channel, entry, day):
    """Run one due schedule slot: "generate" posts the day's deterministic base image
    (announcing it, with an optional magic roll); "edit" edits today's retained base
    (recovering it first, silently, if a previous generate never ran/succeeded).

    The generated prompt is logged, never posted -- only the announcement + image
    (and, for a failure, the fixed FAILURE_MESSAGE) is ever visible in the channel.
    """
    ctx = _ChannelContext(channel)
    base_path = _daily_image_path(day)

    if entry["type"] == "generate":
        prompt, seed, ver = release_image.build_release_prompt(daily_schedule.seed_source_for(day))
        logger.info(f"Daily image for {day.isoformat()}: seed={seed} v{ver} prompt={prompt}")

        magic = False
        if entry.get("magic"):
            prompt, magic = maybe_apply_magic_paint(prompt)

        message = daily_schedule.render_message(entry["message"], day)
        if magic:
            message += " 🖌️"
            _bump_magic_counter()
        await ctx.send(message)

        result = await _do_the_art_with_retry(ctx, prompt, "daily_image", IMAGE_MODEL)
        if not result:
            await ctx.send(FAILURE_MESSAGE)
            return
        _save_daily_image(base_path, result.image_bytes)
        return

    # entry["type"] == "edit": read today's retained base, recovering it first (no
    # announcement, no magic roll -- base recovery is plumbing, not the day's event)
    # if it's missing.
    png_bytes = _read_daily_image(base_path)
    if png_bytes is None:
        prompt, seed, ver = release_image.build_release_prompt(daily_schedule.seed_source_for(day))
        logger.info(f"Daily image for {day.isoformat()} missing; recovering base: seed={seed} v{ver} prompt={prompt}")
        result = await _do_the_art_with_retry(ctx, prompt, "daily_image", IMAGE_MODEL)
        if not result:
            await ctx.send(FAILURE_MESSAGE)
            return
        _save_daily_image(base_path, result.image_bytes)
        png_bytes = result.image_bytes

    # Size the edit off the retained base's OWN dimensions -- mirrors
    # resolve_edit_size's no-flag default, including its AUTO fallback.
    dims = image_size.png_dimensions(png_bytes)
    size = image_size.coerce_generation_size(*dims) if dims else image_size.AUTO

    prompt = entry["edit_prompt"]
    magic = False
    if entry.get("magic"):
        prompt, magic = maybe_apply_magic_paint(prompt)

    message = daily_schedule.render_message(entry["message"], day)
    if magic:
        message += " 🖌️"
        _bump_magic_counter()
    await ctx.send(message)

    result = await _do_the_art_with_retry(
        ctx, prompt, "daily_edit", IMAGE_MODEL,
        images=[(png_bytes, "image/png")], size=size,
    )
    if not result:
        await ctx.send(FAILURE_MESSAGE)


@bot.command(
    name='daily_image',
    help="Post today's image of the day. There's no catch-up, so if the scheduler was "
         "down when the morning slot came due, this paints it on demand. If today's image "
         "already exists it's reposted as-is rather than repainted (no monthly request spent).",
)
async def daily_image_cmd(ctx):
    """Manual stand-in for a due "generate" slot, for the case the scheduler exists to
    handle badly: the bot was down at 07:00, the slot fell outside MISS_WINDOW, and
    (by design) nothing will ever fire it. This does that slot's work on demand.

    Two deliberate differences from the scheduled path:

      - It posts to the INVOKING channel, not DAILY_IMAGE_CHANNEL_ID. It's a command;
        the requester picked where the output goes. It also means the command still
        works with the scheduler disabled entirely (no channel configured, or
        DAILY_IMAGE_ENABLED=false).
      - If today's base image is already on disk it is REPOSTED verbatim -- no API
        call, so nothing counts against API_LIMIT, and no second roll of magic paint
        (the retained PNG already baked in whatever the first roll decided). The
        day's prompt is deterministic, so repainting would only spend budget to get a
        different rendering of the same idea.

    The generate path is otherwise identical to _run_daily_slot's: same deterministic
    prompt, same logged-never-posted rule, same retain-and-prune, same one-retry
    failure handling.
    """
    day = datetime.now(BOT_ZONE).date()
    base_path = _daily_image_path(day)
    entry = daily_schedule.find_generate_entry(daily_schedule.load_schedule(DAILY_SCHEDULE_FILE))
    # Fall back to the seed's own wording if the schedule has no (valid) generate entry
    # -- the command shouldn't stop working just because someone hand-edited that slot
    # out of data/daily_schedule.json.
    template = entry["message"] if entry else "It's the image of the day for {date}"
    message = daily_schedule.render_message(template, day)

    png_bytes = _read_daily_image(base_path)
    if png_bytes is not None:
        logger.info(f"&daily_image by {ctx.author.name}: reposting retained {base_path}")
        await ctx.send(f"{message}\n♻️ (repost — already painted today)")
        await ctx.send(file=discord.File(io.BytesIO(png_bytes), daily_schedule.daily_image_filename(day)))
        return

    prompt, seed, ver = release_image.build_release_prompt(daily_schedule.seed_source_for(day))
    logger.info(f"&daily_image by {ctx.author.name} for {day.isoformat()}: seed={seed} v{ver} prompt={prompt}")

    magic = False
    if entry and entry.get("magic"):
        prompt, magic = maybe_apply_magic_paint(prompt)
    if magic:
        message += " 🖌️"
        _bump_magic_counter()
    await ctx.send(message)

    result = await _do_the_art_with_retry(ctx, prompt, "daily_image", IMAGE_MODEL)
    if not result:
        await ctx.send(FAILURE_MESSAGE)
        return
    _save_daily_image(base_path, result.image_bytes)

    # Mark the generate slot fired for today so the scheduler won't post the same
    # image again if its slot is still inside MISS_WINDOW (running this at 06:58
    # must not produce a second post at 07:00). Done only after a successful
    # generate: burning the slot on a failure would defeat the point of the command.
    if entry:
        state = daily_schedule.load_state(DAILY_STATE_FILE)
        daily_schedule.save_state(daily_schedule.mark_fired(state, entry["id"], day), DAILY_STATE_FILE)


# --- &daily_* schedule-management commands ------------------------------------------
#
# The only way to retime a slot, reword its announcement, or change an edit prompt
# used to be hand-editing data/daily_schedule.json on the volume. These commands are
# open to everyone, no permission checks -- same shape as &macro_*/&magic_* above.
# All the actual logic (parsing, validation, id/time canonicalization, formatting)
# lives in daily_schedule.py; these bodies are thin glue, same division of labor as
# every other command family in this file.
#
# Every mutating command (&daily_add/&daily_update/&daily_remove/&daily_toggle) does
# load -> validate -> save with NO await in between (see daily_schedule.py's module
# docstring §0.2): a single-threaded event loop can't interleave two edits as long as
# nothing yields control between the read and the write, so each command computes its
# reply into a local `message` string and sends it exactly once, at the end -- never
# an early `await ctx.send(...)` partway through a mutation. Because
# _run_due_daily_slots() reloads the schedule fresh every tick (never cached), a
# saved edit here takes effect within at most ~60s, no restart required.

# One literal shared by every write path that can leave the schedule with no
# enabled `generate` slot -- &daily_remove, &daily_toggle, and &daily_update
# (checked unconditionally after every successful save, since either an
# `enabled` flip OR a `type` change to "edit" can be the write that empties
# the last one) -- a single constant so the call sites can't drift into
# slightly different wording over time.
NO_ENABLED_GENERATE_WARNING = (
    "\n⚠️ No enabled `generate` slot is left — edit slots will silently repaint the "
    "day's base image themselves."
)


@bot.command(
    name='daily_list',
    help='List the daily image schedule slots. Use &daily_show to see one in full, &daily_update to change it.',
)
async def daily_list(ctx):
    entries = _load_daily_schedule()
    if not entries:
        # load_schedule fails OPEN on a syntax-broken data/daily_schedule.json --
        # it returns [] exactly the same as a genuinely empty/absent file. Without
        # this check that reads as "empty, add away", and the next &daily_add
        # would overwrite the whole (unparseable-but-still-there) file with a
        # single new entry -- silently discarding every existing slot.
        if daily_schedule.schedule_file_is_corrupt(DAILY_SCHEDULE_FILE):
            await ctx.send(
                f"`{DAILY_SCHEDULE_FILE}` exists but could not be parsed as JSON — fix it by hand. "
                "`&daily_add` won't touch it until it parses, to avoid overwriting whatever's still in there."
            )
        else:
            await ctx.send("The daily schedule is empty. Add a slot with `&daily_add <id> <HH:MM> <generate|edit> <message>`.")
        return
    lines = [
        "Use `&daily_show <id>` for the full detail, `&daily_update <id> <field> <value>` to change one, "
        "`&daily_toggle <id>` to switch it off."
    ]
    lines.extend(daily_schedule.format_schedule_lines(entries))
    # At most one trailing note -- there's no point curating a schedule that can
    # never actually post.
    if not DAILY_IMAGE_ENABLED:
        lines.append("Note: `DAILY_IMAGE_ENABLED` is false — nothing on this schedule will fire.")
    elif not DAILY_IMAGE_CHANNEL_ID:
        lines.append("Note: no `DAILY_IMAGE_CHANNEL_ID` is configured — nothing on this schedule will fire.")
    await send_long(ctx, "\n".join(lines))


@bot.command(name='daily_show', help='Show one daily schedule slot in full by id (see &daily_list).')
async def daily_show(ctx, slot_id=None):
    if not slot_id:
        await ctx.send("Which one? `&daily_show <id>` — see `&daily_list` for ids.")
        return
    entries = _load_daily_schedule()
    entry = daily_schedule.find_slot(entries, slot_id)
    if entry is None:
        await ctx.send(f"No daily slot with id `{daily_schedule.truncate_text(daily_schedule.normalize_slot_id(slot_id))}`.")
        return
    lines = []
    error = daily_schedule.validate_slot(entry)
    if error is not None:
        lines.append(f"⚠️ {error}")
    lines.append(daily_schedule.format_slot_detail(entry))
    await send_long(ctx, "\n".join(lines))


@bot.command(
    name='daily_add',
    help='Add a daily schedule slot. &daily_add <id> <HH:MM> <generate|edit> <message> — for an edit slot append " :: <edit prompt>".',
)
async def daily_add(ctx, slot_id=None, slot_time=None, slot_type=None, *, rest=None):
    if not slot_id or not slot_time or not slot_type or not rest:
        await ctx.send(
            "Usage: `&daily_add <id> <HH:MM> <generate|edit> <message>` — for an edit slot add "
            "` :: <edit prompt>`, e.g. `&daily_add teatime 15:30 edit Tea time! :: everyone stops for tea`"
        )
        return

    normalized_id = daily_schedule.normalize_slot_id(slot_id)
    if not daily_schedule.is_valid_slot_id(normalized_id):
        await ctx.send("Daily slot ids must be 1-32 characters: lowercase letters, digits, `_`, or `-`.")
        return

    entries = _load_daily_schedule()
    if not entries and daily_schedule.schedule_file_is_corrupt(DAILY_SCHEDULE_FILE):
        # See daily_list's identical check: load_schedule fails open to [] on a
        # syntax-broken file, indistinguishable from a genuinely empty schedule.
        # Refuse the add rather than writing a single-entry file over whatever's
        # actually still in data/daily_schedule.json.
        message = (
            f"`{DAILY_SCHEDULE_FILE}` exists but could not be parsed as JSON — fix it by hand first. "
            "Adding a slot now would overwrite it with just this one entry."
        )
    elif daily_schedule.find_slot(entries, normalized_id) is not None:
        # find_slot matches broken rows too, not just validated ones -- otherwise
        # adding a second `lunch` next to a corrupted `lunch` would look like it
        # succeeded (validate_schedule would silently drop one as a duplicate) while
        # actually doing nothing.
        message = (
            f"`{normalized_id}` already exists. Use `&daily_update {normalized_id} <field> <value>` to "
            f"change it, or `&daily_remove {normalized_id}` first."
        )
    else:
        add_message, edit_prompt, error = daily_schedule.parse_add_fields(rest)
        if error is not None:
            message = f"Couldn't add that slot: {error}"
        else:
            entry, error = daily_schedule.build_slot_entry(
                normalized_id, slot_time, slot_type, add_message, edit_prompt=edit_prompt,
                author=ctx.author.name, added=date.today().isoformat(),
            )
            if error is not None:
                message = f"Couldn't add that slot: {error}"
            else:
                # Belt-and-suspenders: build_slot_entry already enforces every rule
                # validate_schedule would, but routing the final check through
                # validate_slot means every rule added there in the future
                # automatically gates this write too, with one wording per rule.
                check_error = daily_schedule.validate_slot(entry)
                if check_error is not None:
                    message = f"Couldn't add that slot: {check_error}"
                else:
                    entries.append(entry)
                    _save_daily_schedule(entries)
                    message = (
                        f"Added daily slot `{entry['id']}` — {entry['time']} {entry['type']}. Switch it off "
                        f"with `&daily_toggle {entry['id']}`, remove it with `&daily_remove {entry['id']}`."
                    )
    await ctx.send(message)


@bot.command(
    name='daily_update',
    help="Change one field of a daily schedule slot: &daily_update <id> <field> <value>. Fields: time, type, message, edit_prompt, magic, enabled.",
)
async def daily_update(ctx, slot_id=None, field=None, *, value=None):
    if not slot_id or not field or value is None:
        await ctx.send(
            "Usage: `&daily_update <id> <field> <value>` — fields: `time`, `type`, `message`, `edit_prompt`, "
            "`magic`, `enabled`. e.g. `&daily_update lunch time 12:30`"
        )
        return

    entries = _load_daily_schedule()
    entry = daily_schedule.find_slot(entries, slot_id)
    if entry is None:
        message = f"No daily slot with id `{daily_schedule.truncate_text(daily_schedule.normalize_slot_id(slot_id))}`."
    else:
        # Display only (every use below is inside an f-string), so bound it the same
        # way the not-found replies above already do. &daily_add caps a new id at 32
        # chars, but a hand-edited data/daily_schedule.json can carry an arbitrarily
        # long one, and echoing that unbounded can push the reply past Discord's
        # 2000-char limit -- turning a successful write into an HTTPException the
        # user reads as "the command failed" after it already succeeded.
        sid = daily_schedule.truncate_text(daily_schedule.slot_entry_id(entry))
        new_entry, error = daily_schedule.apply_slot_update(
            entry, field, value, editor=ctx.author.name, edited=date.today().isoformat(),
        )
        if error is not None:
            message = f"Couldn't update `{sid}`: {error}"
        else:
            # The write invariant: a slot that's currently VALID can never be made
            # invalid by a command (refuse, and say why) -- but a slot that's
            # currently INVALID can always be edited, or a row broken in two
            # fields could never be repaired (fixing either field alone would
            # still fail validation). If it's still invalid after the edit, the
            # write is saved anyway and a heads-up is appended.
            was_valid = daily_schedule.validate_slot(entry) is None
            still_error = daily_schedule.validate_slot(new_entry)
            if was_valid and still_error is not None:
                message = f"Couldn't update `{sid}`: {still_error}"
            else:
                for i, existing in enumerate(entries):
                    if existing is entry:
                        entries[i] = new_entry
                        break
                _save_daily_schedule(entries)

                normalized_field = field.strip().lower()
                if normalized_field in ("magic", "enabled"):
                    display = daily_schedule.format_flag(new_entry.get(normalized_field))
                elif normalized_field in ("message", "edit_prompt"):
                    # Never echo the raw value: it's free-text and can be
                    # arbitrarily long (a `message`/`edit_prompt` up to Discord's
                    # own limits), and a bare ctx.send with the full text can
                    # exceed Discord's 2000-char message cap -- the write would
                    # have already happened, but the confirmation would silently
                    # fail to send. Truncate the same way &daily_list's preview
                    # does, and point at &daily_show for the untruncated text.
                    display = daily_schedule.truncate_text(new_entry.get(normalized_field))
                else:
                    display = new_entry.get(normalized_field)
                message = f"Updated `{sid}` — {normalized_field} is now {display}."
                if normalized_field in ("message", "edit_prompt"):
                    message += f" (`&daily_show {sid}` for the full text.)"
                # Checked unconditionally, not just when normalized_field == "enabled":
                # `&daily_update <id> type edit` on the last enabled generate slot
                # leaves the schedule with zero enabled generate slots exactly the
                # same as disabling it would, and the warning must fire either way.
                # has_enabled_generate_slot is cheap (one pass over `entries`), so
                # there is no reason to special-case which field triggers the check.
                if entries and not daily_schedule.has_enabled_generate_slot(entries):
                    message += NO_ENABLED_GENERATE_WARNING
                if still_error is not None:
                    message += f"\n⚠️ Heads up: `{sid}` still won't fire — {still_error}"
    await ctx.send(message)


@bot.command(name='daily_remove', help='Remove a daily schedule slot by id (see &daily_list).')
async def daily_remove(ctx, slot_id=None):
    if not slot_id:
        await ctx.send("Which one? `&daily_remove <id>` — see `&daily_list` for ids.")
        return

    entries = _load_daily_schedule()
    entry = daily_schedule.find_slot(entries, slot_id)
    if entry is None:
        message = f"No daily slot with id `{daily_schedule.truncate_text(daily_schedule.normalize_slot_id(slot_id))}`."
    else:
        # Display only (every use below is inside an f-string), so bound it the same
        # way the not-found replies above already do. &daily_add caps a new id at 32
        # chars, but a hand-edited data/daily_schedule.json can carry an arbitrarily
        # long one, and echoing that unbounded can push the reply past Discord's
        # 2000-char limit -- turning a successful write into an HTTPException the
        # user reads as "the command failed" after it already succeeded.
        sid = daily_schedule.truncate_text(daily_schedule.slot_entry_id(entry))
        remaining = [e for e in entries if e is not entry]
        _save_daily_schedule(remaining)
        message = f"Removed daily slot `{sid}`."
        if not remaining:
            message += " The schedule is now empty — nothing will post until you add a slot."
        elif not daily_schedule.has_enabled_generate_slot(remaining):
            message += NO_ENABLED_GENERATE_WARNING
    await ctx.send(message)


@bot.command(name='daily_toggle', help="Enable or disable a daily schedule slot without deleting it (see &daily_list).")
async def daily_toggle(ctx, slot_id=None):
    if not slot_id:
        await ctx.send("Which one? `&daily_toggle <id>` — see `&daily_list` for ids.")
        return

    entries = _load_daily_schedule()
    entry = daily_schedule.find_slot(entries, slot_id)
    if entry is None:
        message = f"No daily slot with id `{daily_schedule.truncate_text(daily_schedule.normalize_slot_id(slot_id))}`."
    else:
        # Display only (every use below is inside an f-string), so bound it the same
        # way the not-found replies above already do. &daily_add caps a new id at 32
        # chars, but a hand-edited data/daily_schedule.json can carry an arbitrarily
        # long one, and echoing that unbounded can push the reply past Discord's
        # 2000-char limit -- turning a successful write into an HTTPException the
        # user reads as "the command failed" after it already succeeded.
        sid = daily_schedule.truncate_text(daily_schedule.slot_entry_id(entry))
        new_entry = daily_schedule.toggle_slot(entry, editor=ctx.author.name, edited=date.today().isoformat())
        # Informational only -- flipping `enabled` can never invalidate an
        # otherwise-valid entry (no other field changes), and a broken entry must
        # always still be switchable off, so &daily_toggle never refuses a write.
        still_error = daily_schedule.validate_slot(new_entry)

        for i, existing in enumerate(entries):
            if existing is entry:
                entries[i] = new_entry
                break
        _save_daily_schedule(entries)

        if daily_schedule.slot_is_enabled(new_entry):
            message = f"Daily slot `{sid}` is now **enabled**."
        else:
            message = f"Daily slot `{sid}` is now **disabled** — it stays in the schedule but won't fire."
            if not daily_schedule.has_enabled_generate_slot(entries):
                message += NO_ENABLED_GENERATE_WARNING
        if still_error is not None:
            message += f"\n⚠️ Heads up: `{sid}` still won't fire — {still_error}"
    await ctx.send(message)


def generate_file_name(prompt):
    file_name = re.sub(r'[^0-9a-zA-Z]', '_', prompt)[:50]
    random_string = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(6))
    return f"{file_name}_{random_string}.png"


@bot.command(name='stats', help='Check monthly stats. (limit, requests)')
async def stats(ctx):
    current_month = get_current_month()
    data = load_data()
    if current_month not in data:
        data[current_month] = 0
    if 'safety_trips' not in data:
        data['safety_trips'] = 0
    if 'memes' not in data:
        data['memes'] = 0
    uptime_seconds = (datetime.now() - start_time).total_seconds()
    uptime_in_hours = uptime_seconds / 3600

    history = data.get('magic_rate_history', [])
    if history:
        last = history[-1]
        last_change = f"by {last['user']} on {format_rate_change_time(last['time'])}"
    else:
        last_change = "—"

    # Construct the message parts
    uptime_part = f"Uptime: {format_uptime(uptime_seconds)} ({uptime_in_hours:.2f} hours)"
    limit_part = f"Monthly limit: {LIMIT}"
    requests_part = f"Monthly requests: {data[current_month]}"
    memes_part = f"Memes Requested: {data['memes']}"
    violations_part = f"Safety Violations: {data['safety_trips']}"
    magic_rate_part = f"Magic rate: {format_magic_rate(MAGIC_PAINT_RATE)}"
    magic_part = f"Magic applied: {data.get('magic', 0)}"
    remixes_part = f"Remixes: {data.get('remixes', 0)}"
    release_images_part = f"Release images: {data.get('release_images', 0)}"
    daily_images_part = f"Daily images: {data.get('daily_images', 0)}"
    daily_edits_part = f"Daily edits: {data.get('daily_edits', 0)}"
    macros_part = f"Macros expanded: {data.get('macros', 0)}"
    macro_misses_part = f"Macros not found: {data.get('macro_misses', 0)}"
    pipes_part = f"Pipe chains: {data.get('pipes', 0)}"
    pipe_segments_part = f"Pipe segments: {data.get('pipe_segments', 0)}"
    last_change_part = f"Last rate change: {last_change}"

    # Combine the parts into the final message
    message = (
        f"{uptime_part}\n"
        f"{limit_part}\n"
        f"{requests_part}\n"
        f"{memes_part}\n"
        f"{violations_part}\n"
        f"{magic_rate_part}\n"
        f"{magic_part}\n"
        f"{remixes_part}\n"
        f"{release_images_part}\n"
        f"{daily_images_part}\n"
        f"{daily_edits_part}\n"
        f"{macros_part}\n"
        f"{macro_misses_part}\n"
        f"{pipes_part}\n"
        f"{pipe_segments_part}\n"
        f"{last_change_part}"
    )

    # Send the message
    await ctx.send(message)

async def get_meme_prompt(user_prompt):
    if user_prompt:
        chat_prompt = f"Create a prompt for an image meme based on the following idea: {user_prompt}"
    else:
        chat_prompt = "Create a prompt for an image meme based on your wildest imagination."
    system_message = """
    You are a tragically online memelord you know every meme and understand all the funny jokes and variations.
    You very much want to make a humorous image and so you will give a detailed prompt for an image generator
    like DALL-E and similar.  The image should be specific and provide all the relevant funny details.
    Your response should not include any explanation of the meme or any other information beyond the prompt.
    Keep your response under 1024 characters.
    """
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": chat_prompt}
    ]
    response = await asyncio.get_event_loop().run_in_executor(
        None, lambda: openai.ChatCompletion.create(model=MEME_MODEL, messages=messages)
    )
    logger.debug(f"Meme GPT response: {response}")

    try:
        dall_e_prompt = response['choices'][0]['message']['content'].strip()
    except Exception:
        dall_e_prompt = "Two fluffy black cats trying to fix a broken robot based on Bob Ross"

    return dall_e_prompt


def over_limit(data):
    current_month = get_current_month()
    if current_month not in data:
        data[current_month] = 0
    if data[current_month] >= LIMIT:
        return True
    return False


def get_random_bob_ross_quote():
    quotes = [
        "We don't make mistakes, just happy little accidents.",
        "Talent is a pursued interest. Anything that you're willing to practice, you can do.",
        "There's nothing wrong with having a tree as a friend.",
        "You too can paint almighty pictures.",
        "In painting, you have unlimited power.",
        "I like to beat the brush.",
        "You can do anything you want to do. This is your world.",
        "The secret to doing anything is believing that you can do it.",
        "No pressure. Just relax and watch it happen.",
        "All you need to paint is a few tools, a little instruction, and a vision in your mind.",
        "Just let go — and fall like a little waterfall.",
        "Every day is a good day when you paint.",
        "The more you do it, the better it works.",
        "Find freedom on this canvas.",
        "It's life. It's interesting. It's fun.",
        "Believe that you can do it because you can do it.",
        "You can move mountains, rivers, trees — anything you want.",
        "You can put as many or as few highlights in your world as you want.",
        "The more you practice, the better you get.",
        "This is your creation — and it's just as unique and special as you are."
    ]

    return random.choice(quotes)


def main():
    setup_logging()
    load_config()

    # The Dockerfile's `mkdir -p /app/data/daily_images` is masked once data/ is a bind
    # mount (run.sh mounts the host data dir over /app/data), so ensure the retained-
    # image directory exists here too, at startup, every time. os.makedirs creates every
    # missing intermediate directory, so this one call also creates data/ itself -- which
    # is why it MUST run before the three _seed_* calls below, not after: each of them
    # writes its working copy straight to a data/... path, and json_library.seed_library
    # fails open (catches OSError, logs "Failed to seed ...") rather than raising, so on a
    # fresh checkout with no data/ yet, seeding after would silently no-op every library
    # on the first run and only actually seed on the second.
    os.makedirs(DAILY_IMAGES_DIR, exist_ok=True)
    _seed_magic_library()
    _seed_macro_library()
    _seed_daily_schedule()
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
