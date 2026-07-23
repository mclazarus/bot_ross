import asyncio
import aiohttp
import time
import discord
import os
import json
from datetime import datetime, date
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
from magic_paint import parse_magic_rate, format_magic_rate

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot_ross")
coloredlogs.install(level='INFO', logger=logger, milliseconds=True)

# Load OpenAI API key and Discord bot token from environment variables
openai.api_key = os.environ['OPENAI_API_KEY']
DISCORD_BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']

# Configuration
LIMIT            = int(os.environ.get('API_LIMIT', 100))
IMAGE_MODEL      = os.environ.get('IMAGE_MODEL', 'gpt-image-2-low')
IMAGE_MODERATION = os.environ.get('IMAGE_MODERATION', 'low')
MEME_MODEL       = os.environ.get('MEME_MODEL', 'gpt-5.4-mini')
DATA_FILE        = "data/request_data.json"

try:
    MAGIC_PAINT_RATE = float(os.environ.get('MAGIC_PAINT_RATE', 0.05))
    if not (0.0 <= MAGIC_PAINT_RATE <= 1.0):
        raise ValueError
except (TypeError, ValueError):
    MAGIC_PAINT_RATE = 0.05

# The working library lives on the persistent data/ volume so user-added mixins survive
# redeploys; DEFAULT_MAGIC_PROMPTS_FILE is the seed baked into the image (see _seed_magic_library).
MAGIC_PROMPTS_FILE = "data/magic_prompts.json"
DEFAULT_MAGIC_PROMPTS_FILE = "magic_prompts.json"

# Same two-copy seed/working-volume pattern as the magic library, for ;macro expansions.
MACROS_FILE = "data/macros.json"
DEFAULT_MACROS_FILE = "macros.json"

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


async def send_quote(ctx, magic=False):
    quote = get_random_bob_ross_quote()
    if magic:
        quote += " 🖌️"
        data = load_data()
        data['magic'] = data.get('magic', 0) + 1
        save_data(data)
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


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f)


def get_current_month():
    return datetime.now().strftime("%Y-%m")


@bot.event
async def on_ready():
    logger.info(f'{bot.user.name} has connected to Discord!')


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


@bot.command(name='paint', help='Paint a picture based on a prompt. Flags: --landscape/--portrait/--square, --res WxH. monthly limit')
async def paint(ctx, *, prompt):
    prompt, size = await _prep_generation_size(ctx, prompt)
    if prompt is None:
        return
    prompt = await expand_prompt_macros(ctx, prompt)
    prompt, magic = maybe_apply_magic_paint(prompt)
    await send_quote(ctx, magic)
    await do_the_art(ctx, prompt, "paint", IMAGE_MODEL, size=size)


@bot.command(name='hpaint', help='Paint a high quality picture with gpt-image-2. Flags: --landscape/--portrait/--square, --res WxH. monthly limit')
async def hpaint(ctx, *, prompt):
    prompt, size = await _prep_generation_size(ctx, prompt)
    if prompt is None:
        return
    prompt = await expand_prompt_macros(ctx, prompt)
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "hpaint", "gpt-image-2", size=size)


@bot.command(name='mpaint', help='Paint a medium quality picture with gpt-image-2. Flags: --landscape/--portrait/--square, --res WxH. monthly limit')
async def mpaint(ctx, *, prompt):
    prompt, size = await _prep_generation_size(ctx, prompt)
    if prompt is None:
        return
    prompt = await expand_prompt_macros(ctx, prompt)
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "mpaint", "gpt-image-2-medium", size=size)


@bot.command(name='lpaint', help='Paint a low quality picture with gpt-image-2. Flags: --landscape/--portrait/--square, --res WxH. monthly limit')
async def lpaint(ctx, *, prompt):
    prompt, size = await _prep_generation_size(ctx, prompt)
    if prompt is None:
        return
    prompt = await expand_prompt_macros(ctx, prompt)
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "lpaint", "gpt-image-2-low", size=size)


@bot.command(name='dpaint', help='Paint with DALL-E 3. monthly limit')
async def dpaint(ctx, *, prompt):
    prompt = await expand_prompt_macros(ctx, prompt)
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "dpaint", "dall-e-3")


# Hidden always-on variant of &paint. Named xpaint (not mpaint) since &mpaint is
# already the medium-quality command. Not listed in help; the addition is never revealed.
@bot.command(name='xpaint', help='Paint a picture, with a little extra magic. Flags: --landscape/--portrait/--square, --res WxH.', hidden=True)
async def xpaint(ctx, *, prompt):
    prompt, size = await _prep_generation_size(ctx, prompt)
    if prompt is None:
        return
    prompt = await expand_prompt_macros(ctx, prompt)
    magic_prompt = _apply_random_magic_entry(prompt)
    await send_quote(ctx, magic=True)
    await do_the_art(ctx, magic_prompt, "xpaint", IMAGE_MODEL, size=size)


@bot.command(name='remix', help='Remix an image with a prompt. Attach an image, reply to one, or do both — and add a prompt to guide the transformation. Flags: --landscape/--portrait/--square, --res WxH (snapped to the nearest of 1024x1024/1536x1024/1024x1536 when editing an image). Falls back to painting if no image is found. Monthly limit applies.')
async def remix(ctx, *, prompt=None):
    # Flags are parsed FIRST, before macro expansion/magic paint, exactly like the
    # generation commands' _prep_generation_size -- but remix doesn't use that shared
    # helper because its size resolution differs by which path it ends up on below
    # (edit vs. generation-fallback) and it must still work when there's no prompt at
    # all (image-only remix).
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
                return
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

    if not attachments:
        if not prompt:
            await ctx.send("We need a happy little image to work with before we can remix anything. Attach one, or reply to a message that has one!")
            return
        prompt = await expand_prompt_macros(ctx, prompt)
        prompt, magic = maybe_apply_magic_paint(prompt)
        size, requested = image_size.resolve_generation_size(orientation, res_wh)
        if res_wh and orientation:
            await ctx.send(f"(`--res` overrides `--{orientation}`)")
        if requested and requested != size:
            await ctx.send(f"Using `{size}` (adjusted from `{requested}` to fit the size limits).")
        await send_quote(ctx, magic)
        await do_the_art(ctx, prompt, "remix", IMAGE_MODEL, size=size)
        return

    size, requested = image_size.resolve_edit_size(
        orientation, res_wh, attachments[0].width, attachments[0].height
    )
    if res_wh and orientation:
        await ctx.send(f"(`--res` overrides `--{orientation}`)")
    if requested and requested != size:
        await ctx.send(
            f"Remix outputs one of 1024x1024/1536x1024/1024x1536; using `{size}` "
            f"(`{image_size.describe_edit_size(size)}`)."
        )
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
    await do_the_art(ctx, prompt, "remix", IMAGE_MODEL, images=images, size=size)


@bot.command(name='release_image', help='Generate a deterministic release avatar from a git hash (or any text) — same input always yields the same prompt. Flags: --george, --vN. Monthly limit applies.')
async def release_image_cmd(ctx, *, args=None):
    if not args or not args.strip():
        await ctx.send("Give me a git hash or any text to immortalize as a release image...")
        return
    source, version, georgify = release_image.parse_release_args(args)
    if not source:
        await ctx.send("...I need something to hash besides the flags.")
        return
    try:
        prompt, seed, ver = release_image.build_release_prompt(source, version, georgify)
    except KeyError:
        available = ", ".join(sorted(release_image.RELEASE_ALGORITHMS, key=lambda v: int(v)))
        await ctx.send(f"Unknown algorithm version. Available: {available}")
        return
    # Release images are deliberately NOT subject to magic paint, so send a plain quote.
    await send_quote(ctx)
    george = " | 🥸 George mode" if georgify else ""
    await ctx.send(f"Release image for `{source}` | seed {seed} | algo v{ver}{george}\n**Prompt**: {prompt}")
    await do_the_art(ctx, prompt, "release_image", IMAGE_MODEL)


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


async def do_the_art(ctx, prompt, request_type, model, images=None, size=None):
    # `size` (see image_size.py) is now forwarded on BOTH paths below: fetch_image_edit
    # (images given -- &remix with an attachment) and fetch_image (generation --
    # &paint/&hpaint/&mpaint/&lpaint/&xpaint honoring --res/--landscape/--portrait/
    # --square). None keeps each path's own model-config default size.
    # &dpaint/&meme/&release_image never pass size, so they stay unaffected.
    logger.info(f"Received {request_type} request from {ctx.author.name} using {model} to paint: {prompt}")
    current_month = get_current_month()
    data = load_data()
    if over_limit(data):
        await ctx.send("Monthly limit reached. Please wait until next month to make more paint requests.")
        return False

    file_name = generate_file_name(prompt)

    try:
        t0 = time.monotonic()
        if images:
            response = await fetch_image_edit(prompt, get_edit_model(model), images, size=size)
        else:
            response = await fetch_image(prompt, model, size=size)
        elapsed = time.monotonic() - t0
        image_data = base64.b64decode(response['image'])
        image_file = io.BytesIO(image_data)
        description = (response['revised_prompt'] or prompt)[:1024]
        await ctx.send(file=discord.File(image_file, file_name, description=description))
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
        save_data(data)
        await ctx.send(f"Generated in {format_duration(elapsed)} | Monthly requests: {data[current_month]}")
        return True
    except Exception as e:
        await ctx.send(f"No painting for: {prompt}, exception for this request: {e}")
        return False


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
                        "Authorization": f"Bearer {openai.api_key}",
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
                    headers={"Authorization": f"Bearer {openai.api_key}"},
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
    macros_part = f"Macros expanded: {data.get('macros', 0)}"
    macro_misses_part = f"Macros not found: {data.get('macro_misses', 0)}"
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
        f"{macros_part}\n"
        f"{macro_misses_part}\n"
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


_seed_magic_library()
_seed_macro_library()
bot.run(DISCORD_BOT_TOKEN)
