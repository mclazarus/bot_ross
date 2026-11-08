# Changelog

Newest first. Every release gets a `## Build N · YYYY-MM-DD` entry, numbered one up from the last.
The release's commit is stamped into the image when it's built. Once a build has shipped, its
heading gets the short commit hash: `## Build N · YYYY-MM-DD · abc1234`.

## Build 14 · 2026-11-07
- Paintings now use OpenAI's gpt-image-2.6-flare, which is faster than 2.5. `&hpaint`,
  `&mpaint` and `&lpaint` use it too.
- `--gif` makes an animated GIF instead of a still: `--frames N` (2–16) and `--ms N` (per
  frame) tune it. In a `|` chain the last step can be the GIF, so `&paint a fox | --gif`
  paints a fox and then animates it.
- `&animate` animates an image you attach, reply to or link, or paints an animation from
  just a prompt.
- `--svg` makes a vector picture, posted with a PNG preview since Discord can't show SVGs.
- `--boring` turns off magic for that command, every `|` step included.
- Some mornings the image of the day comes to life: there's a 20% chance it's posted as a GIF
  instead of a still. `&daily_gif_rate` shows or changes the chance.
- Five secret magic mixins: on any day with at least five prompts, at 11:30 pm Bot Ross
  writes a new mixin inspired by what everyone painted that day, replacing the oldest once
  there are five. They turn up in magic paint like the others.
- `&meme` writes its prompts with a newer GPT model (gpt-6-sol), which comes up with funnier,
  more detailed memes.
- `&release_image` and the image of the day pick from much bigger word lists, about 8,300
  ideas instead of 431, so they repeat themselves far less. `&release_image --v1` still makes
  the old pictures.
- `&stats` counts GIFs, SVGs and secret mixins.
- `&version` shows which build is running, and `&changelog [build]` shows what changed in it
  or in any earlier build.
- Bot Ross says hello in the channel when he starts up, with his build number.

## Build 13 · 2026-09-14 · e6f8d85
- Paintings use gpt-image-2.5-flare, at its fast low-quality tier by default. `&hpaint`,
  `&mpaint` and `&lpaint` pick its high, medium and low tiers.

## Build 12 · 2026-08-08 · f53496a
- Behind the scenes: Python 3.14, current libraries, the OpenAI SDK replaced with plain web
  requests, and the full test suite runs before any new version can ship. No command
  changes.

## Build 11 · 2026-08-08 · cc9ad0f
- Image of the day: a painting every morning, plus themed edits of that same painting through
  the day. `&daily_image` posts the morning image on demand, and `&daily_list`, `&daily_show`,
  `&daily_add`, `&daily_update`, `&daily_remove` and `&daily_toggle` manage the schedule.
- Pipes: chain up to five steps with `|`, like `&paint a fox | make it winter`. Each step edits
  the image before it, and every step replies to the first image so the chain is easy to
  follow.
- `&remix` can use the image from a pasted Discord message link on this server.
- Settings with a trailing `# comment` in the config no longer quietly break.

## Build 10 · 2026-07-24 · 8bffa73
- Restarts no longer cut off a painting in progress. Bot Ross finishes what he's working on
  before he shuts down.
- Very small `--res` sizes are bumped up to the smallest size OpenAI accepts instead of
  failing.

## Build 9 · 2026-07-22 · 00310ee
- Size flags for `&paint` and its quality variants and `&remix`: `--landscape`, `--portrait`,
  `--square`, or `--res WxH` for an exact size (adjusted to the nearest size OpenAI allows).
- `&remix` keeps the size and shape of the first image you give it.
- When a `;macro` fires, Bot Ross shows the expanded prompt so you can see what he painted.

## Build 8 · 2026-07-22 · 9f5419e
- `;macros`: type `;name` anywhere in a prompt and it's replaced with a saved description, so
  you don't have to retype your favourite characters. Manage them with `&macro_list`,
  `&macro_show`, `&macro_add`, `&macro_update` and `&macro_remove`.
- `&remix` matches the shape of the image you give it instead of always painting a square.

## Build 7 · 2026-07-20 · 7987492
- `&magic_show <id>` shows a magic mixin's full text, and `&magic_update <id> <text>` edits
  one in place.
- `&magic_list` is a short index of ids and authors.
- Magic rate changes show the full date, time and timezone.

## Build 6 · 2026-07-18 · 9b8b47e
- Magic paint: sometimes a painting gets a recurring background gag slipped in, with a 🖌️
  tell. `&magic_rate` shows or sets how often, and `&magic_list`, `&magic_add` and
  `&magic_remove` manage the gags.
- `&remix` edits the images you attach or reply to, and paints from scratch if there aren't
  any.
- `&release_image <hash>` paints a release avatar from a git hash (or any text). The same
  input always gives the same prompt. `--george` makes the subject George Costanza.
- `&stats` shows uptime in days, hours and minutes, and counts magic, remixes and release
  images.

## Build 5 · 2026-04-30 · 8e2cafc
- Paintings use OpenAI's gpt-image-2, at its fast low tier by default. `&hpaint`, `&mpaint`
  and `&lpaint` choose high, medium or low quality, and `&dpaint` still uses DALL-E 3.
- Each painting says how long it took.
- `&meme` uses a newer GPT model to write its prompt, and a meme only counts once its image
  has actually been made.

## Build 4 · 2024-03-14 · 893d91b
- `&meme [idea]`: GPT writes a meme prompt from your idea, or from its own imagination, and Bot
  Ross paints it.
- Bot Ross shows the revised prompt DALL-E 3 actually painted.

## Build 3 · 2023-11-06 · 37cc460
- Paintings use DALL-E 3, and `&natpaint` paints in its more natural style.
- Image files are named after their prompt.
- `&stats` counts prompts that hit OpenAI's safety filter.

## Build 2 · 2023-03-27 · 2583b7e
- Paintings no longer freeze the bot while they're being made.
- Images are posted as files instead of links, and some failed requests are retried
  automatically.

## Build 1 · 2023-03-26 · bb1ade3
- Bot Ross paints a picture from your prompt with `&paint`, using OpenAI's DALL-E, and keeps
  count of the month's requests against a monthly limit.
