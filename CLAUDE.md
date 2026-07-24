# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bot Ross is a Discord bot that generates images using OpenAI image models. It has five main commands: `&paint` (gpt-image-2 by default), `&dpaint` (always dall-e-3), `&meme` (GPT generates a meme prompt, then paints it), `&remix` (uses attached image(s) — or the image(s) of a message the command replies to — as input to the OpenAI image-edit endpoint, falling back to `&paint` behavior when no image is found), and `&release_image` (deterministically derives a mad-libs image prompt from a git hash or any text — see Release Image). It also has magic-management commands: `&magic_list`, `&magic_show`, `&magic_add`, `&magic_update`, `&magic_remove`, and `&magic_rate` (see Magic Paint); and macro-management commands: `&macro_list`, `&macro_show`, `&macro_add`, `&macro_update`, and `&macro_remove` (see Macros). It tracks monthly usage with a configurable limit and persists stats to `data/request_data.json`.

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

The bot lives in `bot_ross.py` (discord.py, `commands.Bot`, command prefix `&`), with four companion modules holding pure, side-effect-free logic so they can be unit tested without importing the bot: `release_image.py` for `&release_image` (see Release Image), `magic_paint.py` for the magic-mixin rate calculation (see Magic Paint), `macros.py` for `;macro` expansion (see Macros) — the latter two delegate their shared library load/save/seed logic to `json_library.py` — and `image_size.py` for `&remix`'s aspect-ratio-aware edit sizing and (as of the `--res`/orientation-flag feature) `&paint`'s family's generation sizing (see below). Images are fetched directly via `aiohttp` to the OpenAI REST API (not the SDK), returned as base64, and sent as Discord file attachments. Generation POSTs JSON to `/v1/images/generations` (`fetch_image`), by default at the model config's fixed `1024x1024` size, but `&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&xpaint` can override it via `--square`/`--landscape`/`--portrait`/`--res WxH` (see below); `&dpaint`/`&meme`/`&release_image` are unaffected by the sizing feature. `&remix` with an image instead POSTs multipart form data to `/v1/images/edits` (`fetch_image_edit`), which has no `revised_prompt` field; it forwards the model config's `quality` and `moderation` (when supported) so the edit matches the chosen quality tier, and returns PNG b64 like generation. Unlike generation, the edit `size` is not the model config's default: `&remix` reads `width`/`height` off the first resolved `discord.Attachment` and calls `image_size.resolve_edit_size(...)`, whose no-flag default coerces those dimensions (via `coerce_generation_size`) to match the input image's own size/aspect as closely as a valid size allows — gpt-image-2's edit endpoint honors arbitrary sizes, so the output keeps the input's shape rather than being bucketed. `auto` is used whenever Discord didn't report usable dimensions (a non-image attachment slipped through, or an image Discord couldn't probe). The chosen size is forwarded through `do_the_art`'s `size` parameter into `fetch_image_edit`, which sends it in place of the config default; it is logged (a size-selection line in `&remix`, plus the resolved size on the edit-success line) but never announced in the channel. The `get_meme_prompt()` function uses the older `openai.ChatCompletion.create` sync API (v0.27.x) to generate meme prompts.

`image_size.py` also owns the generation-side sizing for `&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&xpaint`: `parse_size_flags` strips `--square`/`--landscape`/`--portrait`/`--res WxH` out of the prompt before macro expansion or magic paint ever see it, and `coerce_generation_size` deterministically coerces an arbitrary `--res` into a size the `/v1/images/generations` endpoint accepts (multiple of 16, aspect ratio in `[1/3, 3]`, within a `3840x2160` box, and at least `GEN_MIN_PIXELS` total area — the endpoint's "minimum pixel budget", determined empirically to be ~655,360 px and floored to the smallest confirmed-accepted area, 665,856 = 816×816; sub-budget sizes are scaled up preserving aspect, with a rounding-safe guard so the floor survives the ÷16 rounding). Empirically gpt-image-2's `/v1/images/edits` endpoint honors arbitrary sizes too (despite the docs only listing the three standard sizes + `auto`), so `resolve_edit_size` coerces every path with the **same** `coerce_generation_size` as the generation path rather than snapping to a standard size — an explicit `--res`, and the no-flag default (which coerces the first attachment's own `width`/`height` to match it as closely as possible, or `auto` when Discord didn't report them). The only difference from `resolve_generation_size` is that default: paint defaults to a square, remix to the input image's own size. `resolve_generation_size`/`resolve_edit_size` each return `(size, requested)`, and callers post a coercion notice only when `requested` is set and differs from `size` — an orientation preset, an already-valid `--res`, or the no-flag default (which leaves `requested` unset) stays silent. `fetch_image` (generation) and `fetch_image_edit` (edit) both now accept a `size` override, copying `MODEL_CONFIGS[...]["params"]` before applying it so the shared config dict is never mutated. `&dpaint` (dall-e-3 has a different size/model set), `&meme`, and `&release_image` are **not** wired up to any of this; adding dall-e-3 sizing later would mean giving it its own size profile alongside `GEN_*`/`ORIENTATIONS`, since dall-e-3's allowed sizes differ from gpt-image-2's.

State is stored in `data/request_data.json` — monthly request counts (keyed by `YYYY-MM`), total meme count (`memes`), safety violation count (`safety_trips`), total magic-applied count (`magic`), total successful-remix count (`remixes`), total successful-release-image count (`release_images`), total macro-expansion count (`macros`) and total macro-miss count (`macro_misses`), and `magic_rate_history` (the last 10 rate changes, each `{"user", "rate", "time"}`, newest last). The `data/` volume also holds the working magic library (`data/magic_prompts.json`, seeded at startup — see Magic Paint) and the working macro library (`data/macros.json`, seeded at startup — see Macros). The `over_limit()` function gates image generation against `API_LIMIT`. Note that `data/` is a runtime-only Docker volume mount (see `run.sh`) — it is not part of the repo or image, so it must never be used to ship static content (the magic and macro library working copies are seeded there at runtime from the image, not shipped).

## Magic Paint

The pure logic — the rate roll, mixin selection/application, library load/save/seed, and the `parse_magic_rate`/`format_magic_rate`/`slugify_magic_id` helpers — lives in **`magic_paint.py`** (no Discord/OpenAI/bot side effects; every function takes its path/rate/library explicitly rather than reading a bot_ross global). `bot_ross.py` keeps thin wrappers (`_load_magic_library`, `_save_magic_library`, `_apply_random_magic_entry`, `maybe_apply_magic_paint`, `_seed_magic_library`) that bind those pure functions to `MAGIC_PROMPTS_FILE`/`DEFAULT_MAGIC_PROMPTS_FILE` and the live `MAGIC_PAINT_RATE`; `parse_magic_rate`/`format_magic_rate` are imported directly. `test_magic_paint.py` (stdlib `unittest`, run `python -m unittest test_magic_paint`) imports `magic_paint` directly and covers the headline rate promise (at 25% over 100 generations ~25 get a mixin, plus large-sample convergence), rate parse/format round-trips, mixin application/fail-open, slugging, library I/O, and seed-library data integrity (exactly one Costanza entry, unique ids). Note the startup env parse of `MAGIC_PAINT_RATE` (a plain float in `[0.0, 1.0]`) is intentionally distinct from `parse_magic_rate` (the `%`/fraction-aware parser used by `&magic_rate`) and stays inline in `bot_ross.py`.

The magic library is a list of joke prompt-additions: recurring background characters (asbestos-hunting cats, a lightning-eyed rice-hat engineer, a capybara, a cycling anteater, a farmer hedgehog and his quail wife) plus exactly one non-subtle "...but it's George Costanza." entry.

There are two copies. The **seed** — `magic_prompts.json` at the repo root (tracked in git, shipped in the Docker image as `DEFAULT_MAGIC_PROMPTS_FILE`) — is the read-only default. The **working copy** lives on the persistent volume at `data/magic_prompts.json` (`MAGIC_PROMPTS_FILE`); this is what `&magic_add`/`&magic_remove` mutate and what `_load_magic_library()` reads fresh on every trigger (never cached). At startup `_seed_magic_library()` copies the seed onto the volume only if the working copy is absent, so user-added mixins survive image rebuilds/redeploys. This is the one deliberate exception to "never use `data/` to ship static content": nothing is shipped there — it's seeded at runtime from the image. `magic_paint.py`'s `load_magic_library`/`save_magic_library`/`seed_magic_library` are thin delegates onto the shared `json_library.py` (also used by `macros.py` — see Macros).

Each library entry has an `id` and `text`; entries added at runtime also carry `author` and `added` (ISO date), and entries edited via `&magic_update` also carry `editor` and `edited` (ISO date) — the original `author`/`added` are left untouched by an edit. Original built-in entries omit all four fields, and all read paths tolerate their absence (shown as `built-in`/`—`, or omitted entirely for `editor`/`edited`). The library is written back via `_save_magic_library()` with `indent=2, ensure_ascii=False` (preserves unicode like `♥️`/`—`).

`maybe_apply_magic_paint(prompt)` rolls against `MAGIC_PAINT_RATE` and only touches disk if the roll succeeds; `_apply_random_magic_entry(prompt)` does the guaranteed (100%) load-pick-append used directly by the hidden `&xpaint` command and by `&remix` when an image is attached with no text. Whenever magic paint triggers (roll or guaranteed), the bot's initial quote message gets a `🖌️` appended as a tell — the actual appended text is never shown to the user. The `magic` counter is incremented in `send_quote()` (the single chokepoint every magic path calls) when the tell is shown, so it counts at reveal time regardless of whether generation then succeeds. `&xpaint` is a hidden (`hidden=True`, excluded from `&help`) always-on variant of `&paint`, kept undocumented in the README by design.

**Magic-management commands** (all open to everyone, matching the rest of the bot — no permission checks):
- `&magic_list` — renders a lightweight index of the library (id + author per entry, no prompt text), chunked under Discord's 2000-char limit via `send_long()`, led by a hint line pointing at `&magic_show <id>` (to read a mixin's prompt) and `&magic_update <id> <text>` (to change it).
- `&magic_show <id>` — shows an entry's untruncated text plus author/added (and editor/edited, if present).
- `&magic_add <text>` — appends an entry with an auto-generated slug id (`_slugify_magic_id`, first ~4 words, `-2`/`-3`… on collision), `author=ctx.author.name`, `added=today`.
- `&magic_update <id> <text>` — replaces an existing entry's `text` in place (id/author/added untouched), setting `editor=ctx.author.name` and `edited=today`. Unlike `&magic_remove` + `&magic_add`, this doesn't churn the id or lose creation history.
- `&magic_remove <id>` — drops the entry with that id (or reports it wasn't found).
- `&magic_rate [value]` — no arg reports the live rate as a percent plus who last changed it; with a value, `parse_magic_rate` interprets it (trailing `%` = exact percent, e.g. `.1%`→0.001; no `%` and `>=1` = percent number, e.g. `10`→0.10; no `%` and `<1` = raw fraction, e.g. `.1`→0.10, `.001`→0.001), clamps to `[0.0, 1.0]`, reassigns the `MAGIC_PAINT_RATE` module global, and records the change via `_record_rate_change` (persisted to `magic_rate_history`, last 10). Note the live rate itself is in-memory only, so after a restart it resets to env/default while `magic_rate_history` (and thus `&stats`' "Last rate change") persists — the two can legitimately disagree. `_record_rate_change` stores a timezone-aware timestamp (`datetime.now().astimezone().isoformat()`); `format_rate_change_time()` renders it as `YYYY-MM-DD HH:MM:SS ±HHMM` wherever it's displayed (`&magic_rate`, `&stats`), falling back to no offset for legacy naive timestamps recorded before this was tracked.

## Macros

`;token` anywhere in a `&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&dpaint`/`&xpaint`/`&remix` prompt is expanded, in place, to a short library snippet before the prompt reaches magic paint or the image API — e.g. `&paint A ;rhe is trapped in a datacenter` (see `&macro_list` for the id table). `&release_image` and `&meme` are deliberately **not** wired up to macro expansion.

The pure logic — the `;token` regex, lookup/substitution, id normalization/validation, and library load/save/seed — lives in **`macros.py`** (no Discord/OpenAI/bot side effects; mirrors `magic_paint.py`'s shape). `bot_ross.py` keeps thin wrappers (`_load_macro_library`, `_save_macro_library`, `_seed_macro_library`) binding those functions to `MACROS_FILE`/`DEFAULT_MACROS_FILE`, plus `expand_prompt_macros(ctx, prompt)` — the single chokepoint every prompt-bearing command calls, always as the **first** step (before `maybe_apply_magic_paint`/`_apply_random_magic_entry`), so an expanded macro's text is itself eligible to receive a magic mixin appended after it. `test_macros.py` (stdlib `unittest`, run `python -m unittest test_macros`) mirrors `test_magic_paint.py`'s coverage style: expansion correctness (single/multiple/repeated/case-insensitive/no-recursion/fail-open), id parse/validate, library I/O, and seed-data integrity.

Expansion is a **single pass** over the original prompt: replacement text is never rescanned for further `;tokens`, so a macro whose text happens to contain `;something` cannot recurse or chain. There is no escape syntax — a bare `;` followed by a space or punctuation is ordinary prose and is left alone; `;;token` still expands `token` (the leading `;` is untouched prose, the second `;` triggers the match). Lookup is case-insensitive against normalized ids (`normalize_macro_id`: lowercased, leading `;` and surrounding whitespace stripped).

Whenever any `;token` is present, `expand_prompt_macros` echoes the fully expanded prompt back on an `expanded prompt: ...` line (via `send_long`, so long prompts chunk safely). Crucially this is the **post-macro, pre-magic-paint** prompt — macro expansion always runs first — so it shows the requester exactly what their macros became while still **never revealing a magic mixin** (magic is appended afterward, and stays hidden behind the `🖌️` tell as before). Unlike magic paint, macros are an explicit, deliberate tool the requester typed, so echoing the expansion is transparency, not a spoiler. An unresolved token is swapped for a joke placeholder from `FALLBACK_EXPANSIONS`, a **static code constant** (deliberately not stored in `macros.json`, so it can't be edited via `&macro_add`/`&macro_update`), and called out on a leading `🎲` line naming every miss (e.g. `` 🎲 `;foo`, `;bar` (macro not found, good luck) ``) above the `expanded prompt:` line, so the requester knows a macro didn't resolve, without blocking generation. `expand_prompt_macros` increments the persisted `macros`/`macro_misses` counters (shown in `&stats`) by however many tokens actually hit/missed on that call — 0 if the prompt had no `;tokens` at all, in which case nothing is written to disk and no message is sent.

There are two copies, the same two-copy seed/working-volume model as the magic library: the **seed** — `macros.json` at the repo root (tracked in git, shipped in the image as `DEFAULT_MACROS_FILE`) — is the read-only default; the **working copy** lives on the persistent volume at `data/macros.json` (`MACROS_FILE`), mutated by `&macro_add`/`&macro_update`/`&macro_remove` and read fresh on every expansion (never cached). `_seed_macro_library()` copies the seed onto the volume at startup only if the working copy is absent.

Each library entry has an `id` (the bare token — no leading `;`) and `text`; entries added at runtime also carry `author`/`added` (ISO date), and edited entries also carry `editor`/`edited` (ISO date) — the same shape as the magic library. Unlike `&magic_add` (which auto-slugs an id from the mixin text), `&macro_add <id> <text>` takes the id **explicitly**, since it's also the literal string a user types as `;<id>` in a prompt.

The `text` of a seed entry (and of every `FALLBACK_EXPANSIONS` member) is by convention an **article-less** noun phrase ending in a comma: the writer supplies the article in the prompt (`A ;rhe is trapped...`), so an expansion beginning with "a"/"an"/"the" would render "A a caucasian engineer...". `test_macros.py` asserts this over both the seed library and the fallbacks. It's a convention for the shipped data only — nothing rejects a user-added macro that breaks it.

**Macro-management commands** (all open to everyone, matching the rest of the bot — no permission checks): `&macro_list` (id + truncated text + author index, chunked via `send_long()`), `&macro_show <id>` (untruncated text + author/added/editor/edited), `&macro_add <id> <text>` (validates the id via `is_valid_macro_id`, refuses duplicates and points at `&macro_update`, stores `author=ctx.author.name`, `added=today`), `&macro_update <id> <text>` (replaces `text` in place; id/author/added untouched; sets `editor`/`edited`), and `&macro_remove <id>` (drops it, or reports it wasn't found) — the same shape as the magic-management commands, minus a rate: macro expansion is deterministic (driven by the presence of a `;token`), not probabilistic, so there is no `&macro_rate` equivalent.

## Release Image

`&release_image <git-hash-or-text> [--george] [--vN]` mints a repeatable avatar for a software release. The input (a git hash, or any text — it may contain spaces) is hashed to deterministically pick a mad-libs-style image **prompt**, so the same input always yields the same prompt. The generated image isn't deterministic (OpenAI generation isn't), but the prompt is — that's what makes a release's avatar stable. The command is **deliberately not subject to magic paint** (no `maybe_apply_magic_paint`/`_apply_random_magic_entry` calls; `send_quote(ctx)` is called with `magic=False`). On success it increments a `release_images` counter (in `do_the_art` alongside `remixes`), shown in `&stats`. The resolved prompt, short seed, and algorithm version are revealed in the channel.

All pure logic lives in **`release_image.py`** (no Discord/OpenAI/bot side effects, so `test_release_image.py` imports it directly — importing `bot_ross` isn't possible under test because module load ends in `bot.run()`):
- `parse_release_args(args)` → `(source, version, georgify)`. Flags are stripped and the rest is rejoined as `source`: `--george`/`--costanza` set georgify; `--version N`, `--version=N`, and `--vN` set the version.
- `build_release_prompt(source, version=None, georgify=False)` → `(prompt, seed, version_used)`. Picks one phrase per category via `_release_index`, applies `georgify_template` to the subject when georgify is on (`"George Costanza if he were {subject}"`), then `template.format(**picks)`. Raises `KeyError` on unknown version (the command catches it and lists available versions).
- `_release_index(source, version, category, n)` = `int.from_bytes(sha256(f"{source.strip()}|{version}|{category}").digest()[:8], "big") % n`. Uses SHA-256 (not Python's `random`) for cross-platform/interpreter stability, and hashes each category with its own name so categories are **independent** — editing one word list, or adding/reordering categories, won't shift another category's pick. The public seed shown to users is `sha256(source.strip())[:8]`.

**Versioned artifact.** The templates + word lists live in **`release_algorithms.json`** at the repo root (`RELEASE_ALGORITHMS_FILE`, resolved relative to `release_image.py`, loaded once at import). It's a JSON object keyed by version string; each version has `template`, `georgify_template`, `categories` (the fixed fill-in order), and `word_lists` (category → phrase list). Because the prompt is a pure function of `(input, algorithm)`, **any** change to a template or word list changes the space of outputs, so such a change must ship as a **new version** while old versions are retained (folding `version` into the hash key means the same input picks differently per version). Unlike the magic library, this file is **static, read-only content**: it ships in the Docker image (`COPY release_algorithms.json .`) and is read straight from the image — it is **never** seeded onto the `data/` volume. To add a version: add a new top-level key (e.g. `"2"`) with its own template/lists; `&release_image --v2` targets it and the default is the highest numeric key.

`test_release_image.py` (stdlib `unittest`, run `python -m unittest test_release_image`) covers determinism, uniqueness/reproducibility across 500 inputs, category variety, structural validity (no leftover `{placeholders}`, every pick present), georgify, versioning, arg parsing, and data integrity (per-category minimum counts — subject/setting ≥ 100, others ≥ 24 — no duplicates, template placeholders map to categories).

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

`fetch_image(prompt, model, size=None)` looks up the config, builds the payload, and returns `{"image": b64, "revised_prompt": str_or_None}`; a non-`None` `size` overrides the config's default generation size, always via a fresh `dict(config["params"])` copy so `MODEL_CONFIGS` itself is never mutated. `do_the_art(ctx, prompt, request_type, model, images=None, size=None)` calls `fetch_image` normally, or `fetch_image_edit` when `images` is provided (routed through `get_edit_model(model)`, which falls back to `gpt-image-2` if the requested model doesn't support edits), and skips the "Revised prompt" Discord message when `revised_prompt` is None. `size` (see `image_size.py`) is forwarded on both paths — to `fetch_image_edit` (in place of the model config's default edit size, for `&remix`) and to `fetch_image` (in place of the model config's default generation size, for `&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&xpaint`); `&dpaint`/`&meme`/`&release_image` never pass it, so they keep each model config's fixed default.

To add a new image model: add an entry to `MODEL_CONFIGS` with its supported params and capability flags (including `supports_edit`).

## Key Dependencies

- `discord.py ~2.3.2` — bot framework
- `openai ~0.27.2` — legacy SDK (pre-1.0, uses `openai.ChatCompletion.create`)
- `aiohttp` — direct HTTP calls to the image generation endpoint

## Notes

- The openai SDK usage is the legacy v0.27 API. If upgrading, `openai.ChatCompletion.create` must be migrated to the new client interface.
- gpt-image-2 does not return a `revised_prompt` field via the Image API (only the Responses API does).
- The `data/` directory must exist at runtime (Dockerfile creates it; locally you may need to create it).
