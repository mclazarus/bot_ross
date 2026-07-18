import asyncio
import aiohttp
import time
import discord
import os
import json
from datetime import datetime
from discord.ext import commands
import openai
import random
import logging
import coloredlogs
import base64
import io
import string
import re

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

MAGIC_PROMPTS_FILE = "magic_prompts.json"

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


def maybe_apply_magic_paint(prompt):
    """Random-roll magic paint at MAGIC_PAINT_RATE. Only reads the library if the roll succeeds."""
    if random.random() < MAGIC_PAINT_RATE:
        return _apply_random_magic_entry(prompt), True
    return prompt, False


async def send_quote(ctx, magic=False):
    quote = get_random_bob_ross_quote()
    if magic:
        quote += " 🖌️"
    await ctx.send(quote)


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


@bot.command(name='remix', help='Remix an attached image, or paint a prompt if none is attached. monthly limit')
async def remix(ctx, *, prompt=None):
    attachments = [a for a in ctx.message.attachments if (a.content_type or "").startswith("image/")]
    skipped = len(ctx.message.attachments) - len(attachments)
    if skipped:
        await ctx.send(f"Skipping {skipped} attachment(s) that aren't images.")

    if not attachments:
        if not prompt:
            await ctx.send("Please provide a prompt or attach an image to remix.")
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
    uptime_in_hours = (datetime.now() - start_time).total_seconds() / 3600

    # Construct the message parts
    uptime_part = f"Uptime: {uptime_in_hours:.2f} hours"
    limit_part = f"Monthly limit: {LIMIT}"
    requests_part = f"Monthly requests: {data[current_month]}"
    memes_part = f"Memes Requested: {data['memes']}"
    violations_part = f"Safety Violations: {data['safety_trips']}"

    # Combine the parts into the final message
    message = (
        f"{uptime_part}\n"
        f"{limit_part}\n"
        f"{requests_part}\n"
        f"{memes_part}\n"
        f"{violations_part}"
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


bot.run(DISCORD_BOT_TOKEN)
