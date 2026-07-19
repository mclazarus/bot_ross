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


def _load_magic_library():
    """Read the magic-prompt library fresh from disk. Not cached in memory."""
    try:
        with open(MAGIC_PROMPTS_FILE, 'r') as f:
            entries = json.load(f)
        if not isinstance(entries, list) or not entries:
            logger.error(f"{MAGIC_PROMPTS_FILE} is empty or malformed.")
            return []
        return entries
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load magic prompt library: {e}")
        return []


def _apply_random_magic_entry(prompt):
    """Load the library, pick one entry, append its text. Fails open (unchanged prompt) if the library is missing/empty."""
    entries = _load_magic_library()
    if not entries:
        return prompt
    text = random.choice(entries).get("text", "")
    return f"{prompt} {text}" if text else prompt


def _save_magic_library(entries):
    """Write the magic-prompt library to disk. Preserves unicode (♥️, —) for readability."""
    with open(MAGIC_PROMPTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def _seed_magic_library():
    """Deploy the bundled default library onto the persistent volume if it isn't there yet.
    Runs at startup so user-added mixins in data/ survive image rebuilds/redeploys; only the
    seed ships in the image."""
    if os.path.exists(MAGIC_PROMPTS_FILE):
        return
    try:
        with open(DEFAULT_MAGIC_PROMPTS_FILE, 'r', encoding='utf-8') as src:
            entries = json.load(src)
        _save_magic_library(entries)
        logger.info(f"Seeded {MAGIC_PROMPTS_FILE} from {DEFAULT_MAGIC_PROMPTS_FILE} ({len(entries)} entries).")
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to seed magic library from {DEFAULT_MAGIC_PROMPTS_FILE}: {e}")


def _slugify_magic_id(text, existing_ids):
    """Build a stable, human-referenceable slug from the first few words of the text,
    appending -2, -3, ... until it's unique against existing_ids."""
    words = re.findall(r'[a-z0-9]+', text.lower())[:4]
    base = '-'.join(words) or "magic"
    slug = base
    n = 2
    while slug in existing_ids:
        slug = f"{base}-{n}"
        n += 1
    return slug


def maybe_apply_magic_paint(prompt):
    """Random-roll magic paint at MAGIC_PAINT_RATE. Only reads the library if the roll succeeds."""
    if random.random() < MAGIC_PAINT_RATE:
        return _apply_random_magic_entry(prompt), True
    return prompt, False


def parse_magic_rate(s):
    """Parse a user-supplied magic rate into a probability in [0.0, 1.0].

    Trailing '%' is interpreted exactly as a percent (10% -> 0.10, .1% -> 0.001).
    Without '%': a value >= 1 is a percent number (10 -> 0.10, 1 -> 0.01),
    a value < 1 is a raw fraction (.1 -> 0.10, .001 -> 0.001).
    Raises ValueError on unparseable input or a result outside [0.0, 1.0]."""
    s = s.strip()
    if s.endswith('%'):
        rate = float(s[:-1]) / 100.0
    else:
        num = float(s)
        rate = num / 100.0 if num >= 1 else num
    if not (0.0 <= rate <= 1.0):
        raise ValueError(f"rate {rate} out of range [0.0, 1.0]")
    return rate


def format_magic_rate(rate):
    """Render a probability as a percent string: 0.05 -> '5%', 0.001 -> '0.1%', 0.10 -> '10%'."""
    return f"{rate * 100:g}%"


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


@bot.command(name='paint', help='Paint a picture based on a prompt. monthly limit')
async def paint(ctx, *, prompt):
    prompt, magic = maybe_apply_magic_paint(prompt)
    await send_quote(ctx, magic)
    await do_the_art(ctx, prompt, "paint", IMAGE_MODEL)


@bot.command(name='hpaint', help='Paint a high quality picture with gpt-image-2. monthly limit')
async def hpaint(ctx, *, prompt):
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "hpaint", "gpt-image-2")


@bot.command(name='mpaint', help='Paint a medium quality picture with gpt-image-2. monthly limit')
async def mpaint(ctx, *, prompt):
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "mpaint", "gpt-image-2-medium")


@bot.command(name='lpaint', help='Paint a low quality picture with gpt-image-2. monthly limit')
async def lpaint(ctx, *, prompt):
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "lpaint", "gpt-image-2-low")


@bot.command(name='dpaint', help='Paint with DALL-E 3. monthly limit')
async def dpaint(ctx, *, prompt):
    await ctx.send(get_random_bob_ross_quote())
    await do_the_art(ctx, prompt, "dpaint", "dall-e-3")


# Hidden always-on variant of &paint. Named xpaint (not mpaint) since &mpaint is
# already the medium-quality command. Not listed in help; the addition is never revealed.
@bot.command(name='xpaint', help='Paint a picture, with a little extra magic.', hidden=True)
async def xpaint(ctx, *, prompt):
    magic_prompt = _apply_random_magic_entry(prompt)
    await send_quote(ctx, magic=True)
    await do_the_art(ctx, magic_prompt, "xpaint", IMAGE_MODEL)


@bot.command(name='remix', help='Remix an image with a prompt. Attach an image, reply to one, or do both — and add a prompt to guide the transformation. Falls back to painting if no image is found. Monthly limit applies.')
async def remix(ctx, *, prompt=None):
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
        prompt, magic = maybe_apply_magic_paint(prompt)
        await send_quote(ctx, magic)
        await do_the_art(ctx, prompt, "remix", IMAGE_MODEL)
        return

    images = [(await a.read(), a.content_type) for a in attachments]
    if prompt:
        prompt, magic = maybe_apply_magic_paint(prompt)
    else:
        prompt = _apply_random_magic_entry("creatively reinterpret this image")
        magic = True

    await send_quote(ctx, magic)
    await do_the_art(ctx, prompt, "remix", IMAGE_MODEL, images=images)


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


@bot.command(name='magic_list', help='List the magic mixins (id, text, author, date).')
async def magic_list(ctx):
    entries = _load_magic_library()
    if not entries:
        await ctx.send("The magic library is empty.")
        return
    lines = []
    for entry in entries:
        text = entry.get("text", "")
        if len(text) > 80:
            text = text[:77] + "..."
        author = entry.get("author", "built-in")
        added = entry.get("added", "—")
        lines.append(f"`{entry.get('id', '?')}` — {text} (by {author}, {added})")
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
    new_id = _slugify_magic_id(text, existing_ids)
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


async def do_the_art(ctx, prompt, request_type, model, images=None):
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
            response = await fetch_image_edit(prompt, get_edit_model(model), images)
        else:
            response = await fetch_image(prompt, model)
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


async def fetch_image(prompt, model):
    config = MODEL_CONFIGS.get(model, MODEL_CONFIGS["gpt-image-2"])
    payload = {
        "model": config["model"],
        "prompt": prompt,
        "n": 1,
        "user": "bot_ross",
        **config["params"],
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


async def fetch_image_edit(prompt, model, images):
    """images: list[(bytes, content_type)] of raw image content and its Discord-reported
    content type (e.g. from discord.Attachment.read()/.content_type).
    Returns {"image": b64, "revised_prompt": None} — the edits endpoint has no revised_prompt."""
    config = MODEL_CONFIGS.get(model, MODEL_CONFIGS["gpt-image-2"])
    async with aiohttp.ClientSession() as session:
        for _ in range(2):
            form = aiohttp.FormData()  # rebuilt every attempt: FormData is single-use
            form.add_field("model", config["model"])
            form.add_field("prompt", prompt)
            form.add_field("n", "1")
            form.add_field("user", "bot_ross")
            size = config["params"].get("size")
            if size:
                form.add_field("size", size)
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
                    logger.info(f"Edit request: {prompt} Success")
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
bot.run(DISCORD_BOT_TOKEN)
