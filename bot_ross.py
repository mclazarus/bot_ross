import discord
import os
import json
from datetime import datetime
from discord.ext import commands
import openai
import random

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
    print(f'{bot.user.name} has connected to Discord!')

@bot.command(name='ping', help='Check for bot liveness and latency. (ms)')
async def ping(ctx):
    await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')

@bot.command(name='paint', help='Paint a picture based on a prompt. monthly limit')
async def paint(ctx, *, prompt):
    print(f"Received request from {ctx.author.name} to paint: {prompt}")
    current_month = get_current_month()
    data = load_data()
    if current_month not in data:
        data[current_month] = 0
    if data[current_month] >= LIMIT:
        await ctx.send("Monthly limit reached. Please wait until next month to make more paint requests.")
        return

    quote = get_random_bob_ross_quote()
    await ctx.send(f"{quote}")
    response = openai.Image.create(prompt=prompt, n=1, size="1024x1024", user="bot_ross")
    image_url = response['data'][0]['url']

    await ctx.send(image_url)

    data[current_month] += 1
    save_data(data)
    await ctx.send(f"Current Monthly requests: {data[current_month]}")

@bot.command(name='stats', help='Check monthly stats. (limit, requests)')
async def stats(ctx):
    current_month = get_current_month()
    data = load_data()
    if current_month not in data:
        data[current_month] = 0
    await ctx.send(f"Monthly limit: {LIMIT}")
    await ctx.send(f"Monthly requests: {data[current_month]}")

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
