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

@bot.command(name='paint', help='Paint a picture based on a prompt. monthly limit')
async def paint(ctx, *, prompt):
    logger.info(f"Received request from {ctx.author.name} to paint: {prompt}")
    current_month = get_current_month()
    data = load_data()
    if current_month not in data:
        data[current_month] = 0
    if data[current_month] >= LIMIT:
        await ctx.send("Monthly limit reached. Please wait until next month to make more paint requests.")
        return

    quote = get_random_bob_ross_quote()
    await ctx.send(f"{quote}")
    
    try:
        image_b64 = await fetch_image(prompt)
        image_data = base64.b64decode(image_b64)
        image_file = io.BytesIO(image_data)       
        await ctx.send(file=discord.File(image_file, "happy_robot_trees.png", description=f"{prompt}"))
        # reload the data for the increment since we are async
        data = load_data()
        if current_month not in data:
            data[current_month] = 0
        data[current_month] += 1
        save_data(data)
        await ctx.send(f"Current Monthly requests: {data[current_month]}")
    except Exception as e:
        await ctx.send(f"No painting for: {prompt}, exception for this request: {e}")


async def fetch_image(prompt):
    async with aiohttp.ClientSession() as session:
        for _ in range(2): 
            async with session.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {openai.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "prompt": prompt,
                    "n": 1,
                    "size": "1024x1024",
                    "user": "bot_ross",
                    "response_format": "b64_json"
                },
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Request: {prompt} Success")
                    return data["data"][0]["b64_json"]
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
                    

@bot.command(name='stats', help='Check monthly stats. (limit, requests)')
async def stats(ctx):
    current_month = get_current_month()
    data = load_data()
    if current_month not in data:
        data[current_month] = 0
    if 'safety_trips' not in data:
        data['safety_trips'] = 0
    uptime_in_hours = (datetime.now() - start_time).total_seconds() / 3600
    await ctx.send(f"Uptime: {uptime_in_hours:.2f} hours\nMonthly limit: {LIMIT}\nMonthly requests: {data[current_month]}\nSafety Violations: {data['safety_trips']}")

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
