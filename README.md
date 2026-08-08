# Bot Ross

Bot Ross is a Discord bot that generates images using OpenAI's image models. ChatGPT told me how to write it.

## Commands

| Command | Description |
|---|---|
| `&paint <prompt>` | Generate an image with gpt-image-2 (or `IMAGE_MODEL`). Flags: `--landscape`/`--portrait`/`--square`, `--res WxH` (coerced to the nearest valid generation size). Chain follow-up edits with `\|` (up to 5 steps) |
| `&dpaint <prompt>` | Generate an image with DALL-E 3. Chain follow-up edits with `\|` (up to 5 steps) |
| `&meme [idea]` | GPT generates a meme prompt, then paints it |
| `&remix [prompt]` | Remix attached image(s), the image in a message you reply to, and/or images from Discord message links pasted in the prompt (same server only) — or paint a prompt if none is found. Output size matches the first image's own dimensions as closely as possible by default; override with `--landscape`/`--portrait`/`--square`/`--res WxH` (coerced to a valid size, same as `&paint`). Chain follow-up edits with `\|` (up to 5 steps) |
| `&release_image <git-hash-or-text> [--george] [--vN]` | Mint a deterministic release avatar: the input is hashed to pick a mad-libs image prompt, so the same input always yields the same prompt. `--george` reimagines the subject as George Costanza; `--vN` selects an algorithm version. Not subject to magic paint. Chain follow-up edits with `\|` (up to 5 steps) |
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
| `&daily_list` | List the daily-image schedule slots (id, time, type, magic, enabled, truncated message) |
| `&daily_show <id>` | Show one daily schedule slot in full |
| `&daily_add <id> <HH:MM> <generate\|edit> <message>` | Add a daily schedule slot — for an `edit` slot, append `` :: <edit prompt>`` after the message |
| `&daily_update <id> <field> <value>` | Change one field of a daily schedule slot (`time`, `type`, `message`, `edit_prompt`, `magic`, `enabled`) |
| `&daily_remove <id>` | Remove a daily schedule slot by id |
| `&daily_toggle <id>` | Enable or disable a daily schedule slot without deleting it |
| `&daily_image` | Post today's image of the day on demand — paints it if the scheduler missed the morning slot, or reposts the retained image (no monthly request spent) if it already exists |
| `&stats` | Show uptime, monthly request count, limit, and magic/remix/release-image/daily-image/macro/pipe activity |
| `&ping` | Check bot latency |

## Daily image of the day

If `DAILY_IMAGE_CHANNEL_ID` is set, Bot Ross posts a deterministic "image of the day"
every morning (07:00, its prompt derived from the date the same way `&release_image`
derives one from a git hash), then posts themed edits of that same retained image at
lunch (12:00), quitting time (17:00), and bedtime (22:00) — all wall-clock in
`BOT_TIMEZONE` (default `America/New_York`), not the container's own clock. The
generated prompt is never shown in the channel, only the announcement and the image.
Retained base images live at `data/daily_images/`, pruned to the newest 14; the
schedule itself lives at `data/daily_schedule.json` (seeded from the image on first
run, same working-copy pattern as the magic/macro libraries) — edit it by hand, or
manage it with the `&daily_list`/`&daily_show`/`&daily_add`/`&daily_update`/
`&daily_remove`/`&daily_toggle` commands (see the table above), open to everyone
like the rest of the bot's commands. Changes take effect within about a minute —
the scheduler reloads the schedule fresh every tick — with no restart needed.
`&daily_update <id> <field> <value>` is the one to reach for to retime a slot, reword
its channel message, or change its edit prompt; `&daily_toggle <id>` flips a slot on
or off without deleting it (and without losing its settings). A slot that's currently
valid can never be saved into an invalid state by these commands — a bad edit is
rejected with an explanation instead. Set `DAILY_IMAGE_ENABLED=false` to turn the
whole scheduler off (the manage commands still work; `&daily_image` still works too).

On startup the bot logs one line confirming what the scheduler will do, so you can
tell at a glance whether it's armed and what's next:

```
Daily image scheduler started: zone=America/New_York, channel=1125788287068541031,
4 enabled slot(s) [morning 07:00, lunch 12:00, quitting_time 17:00, goodnight 22:00];
next: quitting_time at 17:00 EDT on 2026-08-08 (in 253m 0s)
```

If the scheduler is off you get an explicit reason instead — `DAILY_IMAGE_ENABLED is
false`, `no DAILY_IMAGE_CHANNEL_ID set`, or a warning that the channel id is set but
unparseable (which echoes the offending value).

A slot only fires if the bot is running within 10 minutes of its scheduled time —
there is deliberately no catch-up, so a morning slot missed during an outage is
simply lost. **`&daily_image`** covers that case: it does the morning slot's work on
demand, posting to the channel you run it in (so it works even with the scheduler
disabled). If today's image has already been painted it is reposted verbatim, marked
`♻️ (repost — already painted today)`, without a second API call — the day's prompt
is deterministic, so repainting would only spend budget on a different rendering of
the same idea. A successful manual generate marks the morning slot fired, so running
it at 06:58 won't produce a second post at 07:00.

The shipped schedule's 4 slots/day (1 generate + 3 edits) count against the same
monthly `API_LIMIT` as every other command — 4 × ~30 days ≈ 120 generations/month
minimum, more whenever a base image needs recovering or a retry fires — which by
itself exceeds the documented default of `API_LIMIT=100`. If you enable
`DAILY_IMAGE_CHANNEL_ID`, raise `API_LIMIT` to match, or the bot will hit its cap
partway through the month and refuse every command — daily and user-triggered alike
— until the next month rolls over.

## Macros

Drop a `;token` anywhere in a `&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&dpaint`/`&xpaint`/`&remix` prompt and it's replaced, in place, with a short snippet before the image is generated:

    &paint A ;rhe is trapped in a datacenter and the servers are all on fire

By convention a macro's text is an article-less noun phrase ending in a comma — you supply the article (`A ;rhe`, `two ;cat`), so the snippet drops into your sentence without doubling it up. Keep that shape when adding your own.

Whenever a macro is used, the bot echoes the fully expanded prompt back on an `expanded prompt: ...` line so you can see exactly what it became — this is shown before any magic mixin is (silently) added, so it never gives the magic away. If `;rhe` isn't a known macro (typo, or it was removed), the bot swaps in a joke placeholder instead of failing, and calls it out with a leading `🎲` line so you know it didn't resolve as expected — generation still proceeds regardless. See `&macro_list` to browse the library, and note that `&release_image`/`&meme` are not wired up to macro expansion.

## Pipes

`&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&dpaint`/`&xpaint`/`&remix`/`&release_image`
all accept `|` to chain up to five steps in one command:

    &paint a lighthouse | make it winter | now at night

The first segment behaves exactly like the command does today (its own quote, its
own magic mode, its own sizing). Every later segment is an image-edit of the
*previous* segment's output — its text becomes the edit instruction, and it goes
through the same size-flags → macro → magic order as any single command, so
`--res`/`--landscape`/`--portrait`/`;macros` and a magic roll all still work per
segment. Each chained image is posted as a **reply to the first image** (not the
previous one), so the whole chain is traversable backward from any link. A magic hit
on a chain segment surfaces only as a bare 🖌️ on that image's message — the mixin
text itself stays hidden, same as everywhere else. Only the first segment posts the
usual quote/notices; later segments are quiet except for their own image and any
size-coercion notice.

More than 4 pipes (6+ segments) gets you exactly one reply — `Okay, simmer down,
buddy.` — and nothing else. A step that fails aborts the chain with `Chain stopped at
step N of M.`; everything posted before that point stays posted. There's no escape
syntax for a literal `|` in a prompt — consistent with `;macros`' stance on `;` — and
no cross-command chains (segment 2+ is always an edit step, regardless of which
command started the chain).

## Sizing

`&paint`/`&hpaint`/`&mpaint`/`&lpaint`/`&remix` accept size flags:

    &paint --landscape a wide valley
    &paint --res 1920x1080 a wide valley
    &remix --portrait

`--square`/`--landscape`/`--portrait` pick one of the three standard sizes silently.
`--res WIDTHxHEIGHT` requests an exact size, coerced to what the endpoint accepts:
divisible by 16, aspect ratio within 3:1, no larger than 3840x2160, and no smaller than
its minimum pixel budget (~0.67 MP — sizes below that are scaled up, keeping their
aspect). You get a note if the coerced size differs from what you typed. This works the
same on `&paint` and on `&remix` — gpt-image-2's edit endpoint honors arbitrary sizes
too, so a remix keeps your ultrawide/tall aspect instead of collapsing it to a standard
size. (`&remix` with no size flag matches the first attachment's own dimensions as
closely as a valid size allows.) `--res` wins if you give both an orientation flag and
`--res`.

## Message links

`&remix` also accepts Discord message links pasted anywhere in the prompt:

    &remix https://discord.com/channels/<guild>/<channel>/<message> make it neon

Each link is stripped out of the prompt text and its image attachments are added to
the remix, up to 4 images total — after any image you attached directly and after
the message you replied to, so those still drive the output size by default. Only
links to messages **in the same server** are honored, and only when **you** (not
just the bot) can read that channel's history; anything else is skipped with a
plain notice instead of failing the whole command. Links from another server, a DM,
or a channel you can't see are refused for the same reason: without that check,
`&remix` could be used to pull images out of private channels you don't have access
to. Up to 4 unique links are used per command (duplicates collapse into one); extras
beyond that are dropped with a note.

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

`./build.sh` first runs the full test suite inside the image (`docker build --target test .`) and aborts without tagging `bot_ross` if any test fails, so a broken build never produces a shippable image.

If a `bot_ross` container is already running, `run.sh` will stop and remove it before starting the new one. The stop **drains in-flight image generations**: on SIGTERM the bot stops accepting new commands and waits (up to `DRAIN_TIMEOUT` seconds, default `300` — a full 5-step `|` pipe chain is bracketed as one drain unit and can easily take longer than a minute) for running generations to finish before exiting, so a redeploy doesn't drop paintings (or chains) mid-flight. `run.sh` uses `docker stop -t $STOP_TIMEOUT` (default `330`, override via the `STOP_TIMEOUT` env var) — keep it above `DRAIN_TIMEOUT` so the bot exits on its own before Docker force-kills. When a host is provided, `DOCKER_HOST=ssh://<host>` is set so all docker commands run against the remote daemon — the `.env` file is read locally and never copied to the remote host.

The `data/` directory stores monthly request counts, stats, the working magic-mixin library (`data/magic_prompts.json`), the working macro library (`data/macros.json`), the working daily-image schedule (`data/daily_schedule.json`), the daily scheduler's fired-slot bookkeeping (`data/daily_state.json`), and the retained daily base images (`data/daily_images/`) — mount a host path to persist them across container restarts and redeploys. On startup the bot seeds `data/magic_prompts.json`, `data/macros.json`, and `data/daily_schedule.json` from the image's bundled defaults only if they aren't already present, so mixins/macros/schedule edits added via the bot's commands (or by hand, for the schedule) survive image rebuilds.

## Running locally

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python bot_ross.py
```

## Configuration

All options are set via environment variables (see `env.example`).

> **Never put a comment on the same line as a value in `.env`.** `run.sh` passes the
> file to `docker run --env-file`, which splits each line on the first `=` and takes
> the entire remainder as the value — `#` and everything after it included. So
> `BOT_TIMEZONE=America/New_York  # my zone` sets the timezone to the literal string
> `America/New_York  # my zone`, which isn't a valid zone, and the bot falls back to
> UTC. Every setting is parsed leniently (a bad value falls back to its default rather
> than crashing), so this fails *silently* — a poisoned `DAILY_IMAGE_CHANNEL_ID` just
> leaves the scheduler switched off. Keep comments on their own lines. Since v-current
> the bot logs a warning at startup naming any variable whose value looks like it
> swallowed a comment.

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | required | OpenAI API key |
| `DISCORD_BOT_TOKEN` | required | Discord bot token |
| `API_LIMIT` | `100` | Max image generations per calendar month. Note: the shipped daily-image schedule alone fires 4 slots/day (1 generate + 3 edits) — 4 × ~30 days ≈ 120/month, more on a base-image recovery or retry — which exceeds this default by itself, so enabling `DAILY_IMAGE_CHANNEL_ID` below means raising this accordingly |
| `IMAGE_MODEL` | `gpt-image-2` | Image model for `&paint` and `&meme` |
| `IMAGE_MODERATION` | `low` | Content moderation level (`low` or `auto`, gpt-image-2 only) |
| `MEME_MODEL` | `gpt-5.4-mini` | GPT model used to generate meme prompts |
| `MAGIC_PAINT_RATE` | `0.05` | Chance (0.0-1.0) that `&paint`/`&remix` silently appends a background gag to the prompt |
| `DRAIN_TIMEOUT` | `300` | Seconds to let in-flight image generations (including a whole in-progress `\|` pipe chain) finish on shutdown before the bot closes |
| `BOT_TIMEZONE` | `America/New_York` | IANA timezone the daily-image schedule's slot times are wall-clock in. An unknown zone falls back to UTC with a logged warning |
| `DAILY_IMAGE_CHANNEL_ID` | unset | Discord channel id the daily image/edits post to. Unset disables the scheduler. Setting this adds ~120 generations/month against `API_LIMIT` (see above) — raise it accordingly |
| `DAILY_IMAGE_ENABLED` | `true` | Set `false` to disable the daily-image scheduler outright, even with a channel id configured |
