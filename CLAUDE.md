# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bot Ross is a Discord bot that generates images using OpenAI image models. It has four main commands: `&paint` (gpt-image-2 by default), `&dpaint` (always dall-e-3), `&meme` (GPT generates a meme prompt, then paints it), and `&remix` (uses attached image(s) — or the image(s) of a message the command replies to — as input to the OpenAI image-edit endpoint, falling back to `&paint` behavior when no image is found). It also has magic-management commands: `&magic_list`, `&magic_add`, `&magic_remove`, and `&magic_rate` (see Magic Paint). It tracks monthly usage with a configurable limit and persists stats to `data/request_data.json`.

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
| `MAGIC_PAINT_RATE` | `0.05` | Starting chance (0.0-1.0) that `&paint`/`&remix` silently appends a background gag to the prompt; invalid or out-of-range values are coerced to the default. Read once at startup — the live rate is then controlled in-process via `&magic_rate` and is **not** persisted (resets to this value on restart) |

## Architecture

Single-file bot (`bot_ross.py`) using discord.py with the `commands.Bot` framework (command prefix: `&`). Images are fetched directly via `aiohttp` to the OpenAI REST API (not the SDK), returned as base64, and sent as Discord file attachments. Generation POSTs JSON to `/v1/images/generations` (`fetch_image`); `&remix` with an image instead POSTs multipart form data to `/v1/images/edits` (`fetch_image_edit`), which has no `revised_prompt` field; it forwards the model config's `size`, `quality`, and `moderation` (when supported) so the edit matches the chosen quality tier, and returns PNG b64 like generation. The `get_meme_prompt()` function uses the older `openai.ChatCompletion.create` sync API (v0.27.x) to generate meme prompts.

State is stored in `data/request_data.json` — monthly request counts (keyed by `YYYY-MM`), total meme count (`memes`), safety violation count (`safety_trips`), total magic-applied count (`magic`), total successful-remix count (`remixes`), and `magic_rate_history` (the last 10 rate changes, each `{"user", "rate", "time"}`, newest last). The `data/` volume also holds the working magic library (`data/magic_prompts.json`, seeded at startup — see Magic Paint). The `over_limit()` function gates image generation against `API_LIMIT`. Note that `data/` is a runtime-only Docker volume mount (see `run.sh`) — it is not part of the repo or image, so it must never be used to ship static content (the magic library working copy is seeded there at runtime from the image, not shipped).

## Magic Paint

The magic library is a list of joke prompt-additions: recurring background characters (asbestos-hunting cats, a lightning-eyed rice-hat engineer, a capybara, a cycling anteater, a farmer hedgehog and his quail wife) plus exactly one non-subtle "...but it's George Costanza." entry.

There are two copies. The **seed** — `magic_prompts.json` at the repo root (tracked in git, shipped in the Docker image as `DEFAULT_MAGIC_PROMPTS_FILE`) — is the read-only default. The **working copy** lives on the persistent volume at `data/magic_prompts.json` (`MAGIC_PROMPTS_FILE`); this is what `&magic_add`/`&magic_remove` mutate and what `_load_magic_library()` reads fresh on every trigger (never cached). At startup `_seed_magic_library()` copies the seed onto the volume only if the working copy is absent, so user-added mixins survive image rebuilds/redeploys. This is the one deliberate exception to "never use `data/` to ship static content": nothing is shipped there — it's seeded at runtime from the image.

Each library entry has an `id` and `text`; entries added at runtime also carry `author` and `added` (ISO date). Original built-in entries omit those two fields, and all read paths tolerate their absence (shown as `built-in`/`—`). The library is written back via `_save_magic_library()` with `indent=2, ensure_ascii=False` (preserves unicode like `♥️`/`—`).

`maybe_apply_magic_paint(prompt)` rolls against `MAGIC_PAINT_RATE` and only touches disk if the roll succeeds; `_apply_random_magic_entry(prompt)` does the guaranteed (100%) load-pick-append used directly by the hidden `&xpaint` command and by `&remix` when an image is attached with no text. Whenever magic paint triggers (roll or guaranteed), the bot's initial quote message gets a `🖌️` appended as a tell — the actual appended text is never shown to the user. The `magic` counter is incremented in `send_quote()` (the single chokepoint every magic path calls) when the tell is shown, so it counts at reveal time regardless of whether generation then succeeds. `&xpaint` is a hidden (`hidden=True`, excluded from `&help`) always-on variant of `&paint`, kept undocumented in the README by design.

**Magic-management commands** (all open to everyone, matching the rest of the bot — no permission checks):
- `&magic_list` — renders the library (id, truncated text, author, date), chunked under Discord's 2000-char limit via `send_long()`.
- `&magic_add <text>` — appends an entry with an auto-generated slug id (`_slugify_magic_id`, first ~4 words, `-2`/`-3`… on collision), `author=ctx.author.name`, `added=today`.
- `&magic_remove <id>` — drops the entry with that id (or reports it wasn't found).
- `&magic_rate [value]` — no arg reports the live rate as a percent plus who last changed it; with a value, `parse_magic_rate` interprets it (trailing `%` = exact percent, e.g. `.1%`→0.001; no `%` and `>=1` = percent number, e.g. `10`→0.10; no `%` and `<1` = raw fraction, e.g. `.1`→0.10, `.001`→0.001), clamps to `[0.0, 1.0]`, reassigns the `MAGIC_PAINT_RATE` module global, and records the change via `_record_rate_change` (persisted to `magic_rate_history`, last 10). Note the live rate itself is in-memory only, so after a restart it resets to env/default while `magic_rate_history` (and thus `&stats`' "Last rate change") persists — the two can legitimately disagree.

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
