import datetime
import asyncio
import aiohttp
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
LIMIT = int(os.environ.get('API_LIMIT', 100))
DATA_FILE = "data/request_data.json"

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
    await do_the_art(ctx, gpt_prompt, "meme")
    data = load_data()
    if 'memes' not in data:
        data['memes'] = 0
    data['memes'] += 1
    save_data(data)


@bot.command(name='paint', help='Paint a picture based on a prompt. monthly limit')
async def paint(ctx, *, prompt):
    quote = get_random_bob_ross_quote()
    await ctx.send(f"{quote}")
    await do_the_art(ctx, prompt, "paint")


async def do_the_art(ctx, prompt, request_type):
    logger.info(f"Received {request_type} request from {ctx.author.name} to paint: {prompt}")
    current_month = get_current_month()
    data = load_data()
    if over_limit(data):
        await ctx.send("Monthly limit reached. Please wait until next month to make more paint requests.")
        return

    file_name = await generate_file_name(prompt)

    try:
        response = await fetch_image(prompt)
        image_data = base64.b64decode(response['image'])
        image_file = io.BytesIO(image_data)
        await ctx.send(file=discord.File(image_file, file_name, description=f"{response['revised_prompt']}"))
        # reload the data for the increment since we are async
        await ctx.send(f"**Revised prompt**: {response['revised_prompt']}")
        data = load_data()
        if current_month not in data:
            data[current_month] = 0
        data[current_month] += 1
        save_data(data)
        await ctx.send(f"Current Monthly requests: {data[current_month]}")
    except Exception as e:
        await ctx.send(f"No painting for: {prompt}, exception for this request: {e}")


async def fetch_image(prompt, style="vivid"):
    async with aiohttp.ClientSession() as session:
        for _ in range(2):
            async with session.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={
                        "Authorization": f"Bearer {openai.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "dall-e-3",
                        "prompt": prompt,
                        "n": 1,
                        "size": "1024x1024",
                        "quality": "hd",
                        "style": style,
                        "user": "bot_ross",
                        "response_format": "b64_json",
                    },
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Request: {prompt} Success")
                    result = {"image": data["data"][0]["b64_json"], "revised_prompt": data["data"][0]["revised_prompt"]}
                    return result
                else:
                    error_json = await response.json()
                    if "error" in error_json:
                        error_message = error_json["error"]["message"]
                    else:
                        error_message = await response.text()
                    if response.status in [429, 500, 503]:
                        logger.error(f"Request: {prompt} Trying again. Error: {response.status} {error_message}")
                        await asyncio.sleep(5)
                    elif response.status == 400:
                        logger.info(f"Request: {prompt} Safety Violation.")
                        data = load_data()
                        if 'safety_trips' not in data:
                            data['safety_trips'] = 0
                        data['safety_trips'] += 1
                        save_data(data)
                    else:
                        logger.error(f"Request: {prompt} Error: {response.status}: {error_message}")

        raise Exception(f"response: {response.status}: {error_message}")


async def generate_file_name(prompt):
    # replace all special characters with _
    file_name = re.sub(r'[^0-9a-zA-Z]', '_', prompt)
    # limit size of string to 50 characters
    file_name = file_name[:50]
    # tack on a random bit of data to the end of the file name to avoid collisions
    # Define the characters that can be used in the string
    characters = string.ascii_letters + string.digits
    # Generate a random 6-character string
    random_string = ''.join(random.choice(characters) for _ in range(6))
    file_name = f"{file_name}_{random_string}.png"
    return file_name


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
    """
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": chat_prompt}
        ]
    )

    print(response)

    try:
        dall_e_prompt = response['choices'][0]['message']['content'].strip()
    except:
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
