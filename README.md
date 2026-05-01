# Bot Ross

Bot Ross is a Discord bot that generates images using OpenAI's image models. ChatGPT told me how to write it.

## Commands

| Command | Description |
|---|---|
| `&paint <prompt>` | Generate an image with gpt-image-2 (or `IMAGE_MODEL`) |
| `&dpaint <prompt>` | Generate an image with DALL-E 3 |
| `&meme [idea]` | GPT generates a meme prompt, then paints it |
| `&stats` | Show uptime, monthly request count, and limit |
| `&ping` | Check bot latency |

## Setup

1. Copy `env.example` to `.env` and fill in your secrets:
   ```
   cp env.example .env
   ```

2. Edit `.env` with your `OPENAI_API_KEY` and `DISCORD_BOT_TOKEN`.

## Running with Docker

```bash
# Build and run locally
./build.sh
./run.sh .env /path/to/data

# Build and run on a remote host (e.g. a Raspberry Pi)
./build.sh docks.local
./run.sh .env /path/to/data docks.local
```

If a `bot_ross` container is already running, `run.sh` will stop and remove it before starting the new one. When a host is provided, `DOCKER_HOST=ssh://<host>` is set so all docker commands run against the remote daemon — the `.env` file is read locally and never copied to the remote host.

The `data/` directory stores monthly request counts and stats — mount a host path to persist them across container restarts.

## Running locally

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot_ross.py
```

## Configuration

All options are set via environment variables (see `env.example`):

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | required | OpenAI API key |
| `DISCORD_BOT_TOKEN` | required | Discord bot token |
| `API_LIMIT` | `100` | Max image generations per calendar month |
| `IMAGE_MODEL` | `gpt-image-2` | Image model for `&paint` and `&meme` |
| `IMAGE_MODERATION` | `low` | Content moderation level (`low` or `auto`, gpt-image-2 only) |
| `MEME_MODEL` | `gpt-5.4-mini` | GPT model used to generate meme prompts |
