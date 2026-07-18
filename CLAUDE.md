# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bot Ross is a Discord bot that generates images using OpenAI image models. It has four main commands: `&paint` (gpt-image-2 by default), `&dpaint` (always dall-e-3), `&meme` (GPT generates a meme prompt, then paints it), and `&remix` (uses attached image(s) as input to the OpenAI image-edit endpoint, falling back to `&paint` behavior when no image is attached). It tracks monthly usage with a configurable limit and persists stats to `data/request_data.json`.

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
| `MAGIC_PAINT_RATE` | `0.05` | Chance (0.0-1.0) that `&paint`/`&remix` silently appends a background gag to the prompt; invalid or out-of-range values are coerced to the default |

## Architecture

Single-file bot (`bot_ross.py`) using discord.py with the `commands.Bot` framework (command prefix: `&`). Images are fetched directly via `aiohttp` to the OpenAI REST API (not the SDK), returned as base64, and sent as Discord file attachments. Generation POSTs JSON to `/v1/images/generations` (`fetch_image`); `&remix` with an attached image instead POSTs multipart form data to `/v1/images/edits` (`fetch_image_edit`), which has no `revised_prompt` field. The `get_meme_prompt()` function uses the older `openai.ChatCompletion.create` sync API (v0.27.x) to generate meme prompts.

State is stored in `data/request_data.json` — monthly request counts (keyed by `YYYY-MM`), total meme count, and safety violation count. The `over_limit()` function gates image generation against `API_LIMIT`. Note that `data/` is a runtime-only Docker volume mount (see `run.sh`) — it is not part of the repo or image, so it must never be used to ship static content.

## Magic Paint

`magic_prompts.json` (repo root, shipped in the Docker image, tracked in git — distinct from the runtime-only `data/` volume) holds a library of joke prompt-additions: recurring background characters (asbestos-hunting cats, a lightning-eyed rice-hat engineer, a capybara, a cycling anteater, a farmer hedgehog and his quail wife) plus exactly one non-subtle "...but it's George Costanza." entry. It's read fresh from disk via `_load_magic_library()` on every trigger — never cached in memory.

`maybe_apply_magic_paint(prompt)` rolls against `MAGIC_PAINT_RATE` and only touches disk if the roll succeeds; `_apply_random_magic_entry(prompt)` does the guaranteed (100%) load-pick-append used directly by the hidden `&xpaint` command and by `&remix` when an image is attached with no text. Whenever magic paint triggers (roll or guaranteed), the bot's initial quote message gets a `🖌️` appended as a tell — the actual appended text is never shown to the user. `&xpaint` is a hidden (`hidden=True`, excluded from `&help`) always-on variant of `&paint`, kept undocumented in the README by design.

## Model Abstraction

`MODEL_CONFIGS` (defined near the top of `bot_ross.py`) captures per-model API differences:

```python
MODEL_CONFIGS = {
    "gpt-image-2": {
        "params": {"size": "1024x1024", "quality": "high"},
        "has_revised_prompt": False,   # Image API doesn't return revised_prompt
        "supports_moderation": True,
        "supports_edit": True,         # can be used with /v1/images/edits (&remix)
    },
    "dall-e-3": {
        "params": {"size": "1024x1024", "quality": "hd", "style": "vivid"},
        "has_revised_prompt": True,
        "supports_moderation": False,
        "supports_edit": False,        # dall-e-3 has no edits endpoint support
    },
}
```

`fetch_image(prompt, model)` looks up the config, builds the payload, and returns `{"image": b64, "revised_prompt": str_or_None}`. `do_the_art(ctx, prompt, request_type, model, images=None)` calls `fetch_image` normally, or `fetch_image_edit` when `images` is provided (routed through `get_edit_model(model)`, which falls back to `gpt-image-2` if the requested model doesn't support edits), and skips the "Revised prompt" Discord message when `revised_prompt` is None.

To add a new image model: add an entry to `MODEL_CONFIGS` with its supported params and capability flags (including `supports_edit`).

## Key Dependencies

- `discord.py ~2.3.2` — bot framework
- `openai ~0.27.2` — legacy SDK (pre-1.0, uses `openai.ChatCompletion.create`)
- `aiohttp` — direct HTTP calls to the image generation endpoint

## Notes

- The openai SDK usage is the legacy v0.27 API. If upgrading, `openai.ChatCompletion.create` must be migrated to the new client interface.
- gpt-image-2 does not return a `revised_prompt` field via the Image API (only the Responses API does).
- The `data/` directory must exist at runtime (Dockerfile creates it; locally you may need to create it).
