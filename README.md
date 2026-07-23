# Bot Ross

Bot Ross is a Discord bot that generates images using OpenAI's image models. ChatGPT told me how to write it.

## Commands

| Command | Description |
|---|---|
| `&paint <prompt>` | Generate an image with gpt-image-2 (or `IMAGE_MODEL`) |
| `&dpaint <prompt>` | Generate an image with DALL-E 3 |
| `&meme [idea]` | GPT generates a meme prompt, then paints it |
| `&remix [prompt]` | Remix attached image(s) — or the image in a message you reply to — with a prompt, or paint a prompt if none is attached. Output size matches the first image's orientation (square/landscape/portrait) |
| `&release_image <git-hash-or-text> [--george] [--vN]` | Mint a deterministic release avatar: the input is hashed to pick a mad-libs image prompt, so the same input always yields the same prompt. `--george` reimagines the subject as George Costanza; `--vN` selects an algorithm version. Not subject to magic paint |
| `&magic_list` | List the magic mixins (id, truncated text, author, date) |
| `&magic_show <id>` | Show the full text of a magic mixin |
| `&magic_add <text>` | Add a magic mixin appended to prompts when magic fires |
| `&magic_update <id> <text>` | Update a magic mixin's text in place, recording you as editor |
| `&magic_remove <id>` | Remove a magic mixin by id |
| `&magic_rate [value]` | Show the current magic rate, or set it (`10`, `.1`, `10%`, `.1%`) |
| `&macro_list` | List the ;macro ids (id, truncated text, author) |
| `&macro_show <id>` | Show the full text of a ;macro |
| `&macro_add <id> <text>` | Add a ;macro — the id is what you type as `;<id>` in a prompt |
| `&macro_update <id> <text>` | Update a ;macro's text in place, recording you as editor |
| `&macro_remove <id>` | Remove a ;macro by id |
| `&stats` | Show uptime, monthly request count, limit, and magic/remix/release-image/macro activity |
| `&ping` | Check bot latency |

## Macros

Drop a `;token` anywhere in a `&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&dpaint`/`&xpaint`/`&remix` prompt and it's replaced, in place, with a short snippet before the image is generated:

    &paint A ;rhe is trapped in a datacenter and the servers are all on fire

By convention a macro's text is an article-less noun phrase ending in a comma — you supply the article (`A ;rhe`, `two ;cat`), so the snippet drops into your sentence without doubling it up. Keep that shape when adding your own.

If `;rhe` isn't a known macro (typo, or it was removed), the bot swaps in a joke placeholder instead of failing, and calls it out with a `🎲` message so you know it didn't resolve as expected — generation still proceeds regardless. Successful expansions are silent. See `&macro_list` to browse the library, and note that `&release_image`/`&meme` are not wired up to macro expansion.

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

The `data/` directory stores monthly request counts, stats, the working magic-mixin library (`data/magic_prompts.json`), and the working macro library (`data/macros.json`) — mount a host path to persist them across container restarts and redeploys. On startup the bot seeds both `data/magic_prompts.json` and `data/macros.json` from the image's bundled defaults only if they aren't already present, so mixins/macros added via `&magic_add`/`&macro_add` survive image rebuilds.

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
| `MAGIC_PAINT_RATE` | `0.05` | Chance (0.0-1.0) that `&paint`/`&remix` silently appends a background gag to the prompt |
