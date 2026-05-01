# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bot Ross is a Discord bot that generates images using OpenAI image models. It has three main commands: `&paint` (gpt-image-2 by default), `&dpaint` (always dall-e-3), and `&meme` (GPT generates a meme prompt, then paints it). It tracks monthly usage with a configurable limit and persists stats to `data/request_data.json`.

## Running

```bash
# Copy env.example to .env and fill in your secrets
cp env.example .env

# Run directly
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot_ross.py

# Run with Docker
./build.sh
./run.sh .env /path/to/data
```

Environment variables (see `env.example`):

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | required | OpenAI API key |
| `DISCORD_BOT_TOKEN` | required | Discord bot token |
| `API_LIMIT` | `100` | Max image generations per calendar month |
| `IMAGE_MODEL` | `gpt-image-2` | Image model for `&paint` and `&meme` |
| `IMAGE_MODERATION` | `low` | Content moderation level for gpt-image-2 (`low` or `auto`) |
| `MEME_MODEL` | `gpt-5.4-mini` | GPT model used to generate meme prompts |

## Architecture

Single-file bot (`bot_ross.py`) using discord.py with the `commands.Bot` framework (command prefix: `&`). Images are fetched directly via `aiohttp` to the OpenAI REST API (not the SDK), returned as base64, and sent as Discord file attachments. The `get_meme_prompt()` function uses the older `openai.ChatCompletion.create` sync API (v0.27.x) to generate meme prompts.

State is stored in `data/request_data.json` — monthly request counts (keyed by `YYYY-MM`), total meme count, and safety violation count. The `over_limit()` function gates image generation against `API_LIMIT`.

## Model Abstraction

`MODEL_CONFIGS` (defined near the top of `bot_ross.py`) captures per-model API differences:

```python
MODEL_CONFIGS = {
    "gpt-image-2": {
        "params": {"size": "1024x1024", "quality": "high"},
        "has_revised_prompt": False,   # Image API doesn't return revised_prompt
        "supports_moderation": True,
    },
    "dall-e-3": {
        "params": {"size": "1024x1024", "quality": "hd", "style": "vivid"},
        "has_revised_prompt": True,
        "supports_moderation": False,
    },
}
```

`fetch_image(prompt, model)` looks up the config, builds the payload, and returns `{"image": b64, "revised_prompt": str_or_None}`. `do_the_art(ctx, prompt, request_type, model)` calls `fetch_image` and skips the "Revised prompt" Discord message when `revised_prompt` is None.

To add a new image model: add an entry to `MODEL_CONFIGS` with its supported params and capability flags.

## Key Dependencies

- `discord.py ~2.3.2` — bot framework
- `openai ~0.27.2` — legacy SDK (pre-1.0, uses `openai.ChatCompletion.create`)
- `aiohttp` — direct HTTP calls to the image generation endpoint

## Notes

- The openai SDK usage is the legacy v0.27 API. If upgrading, `openai.ChatCompletion.create` must be migrated to the new client interface.
- gpt-image-2 does not return a `revised_prompt` field via the Image API (only the Responses API does).
- The `data/` directory must exist at runtime (Dockerfile creates it; locally you may need to create it).
