# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bot Ross is a Discord bot that generates images using the OpenAI DALL-E 3 API. It has two main modes: `&paint` (user-provided prompt) and `&meme` (GPT-4 generates a meme prompt, optionally based on user input). It tracks monthly usage with a configurable limit and persists stats to `data/request_data.json`.

## Running

```bash
# Required environment variables
export OPENAI_API_KEY=...
export DISCORD_BOT_TOKEN=...
export API_LIMIT=100  # optional, defaults to 100

# Run directly
pip install -r requirements.txt
python bot_ross.py

# Run with Docker
docker build -t bot_ross .
docker run -e OPENAI_API_KEY -e DISCORD_BOT_TOKEN bot_ross
```

## Architecture

Single-file bot (`bot_ross.py`) using discord.py with the `commands.Bot` framework (command prefix: `&`). Images are fetched directly via `aiohttp` to the OpenAI REST API (not the SDK), returned as base64, and sent as Discord file attachments. The `get_meme_prompt()` function uses the older `openai.ChatCompletion.create` sync API (v0.27.x) to generate prompts via GPT-4.

State is stored in `data/request_data.json` — monthly request counts (keyed by `YYYY-MM`), total meme count, and safety violation count. The `over_limit()` function gates image generation against `API_LIMIT`.

## Key Dependencies

- `discord.py ~2.3.2` — bot framework
- `openai ~0.27.2` — legacy SDK (pre-1.0, uses `openai.ChatCompletion.create`)
- `aiohttp` — direct HTTP calls to DALL-E 3 image generation endpoint

## Notes

- The openai SDK usage is the legacy v0.27 API. If upgrading, `openai.ChatCompletion.create` must be migrated to the new client interface.
- Image generation uses `aiohttp` directly rather than the openai SDK.
- The `data/` directory must exist at runtime (Dockerfile creates it; locally you may need to create it).
