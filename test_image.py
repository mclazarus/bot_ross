#!/usr/bin/env python3
"""Quick sanity check for OpenAI image-generation API calls. Hits the LIVE API and
spends real money, so it is deliberately excluded from the local test gate and never
copied into the Docker image.

    python test_image.py                                  # IMAGE_MODEL from .env, quality high
    python test_image.py --model gpt-image-2.5-flare --quality low,xhigh,max a red fox
    python test_image.py --quality low "a happy robot"

`--model` is the raw API model name (NOT a bot_ross MODEL_CONFIGS alias like
`gpt-image-2.5-flare-low`); `--quality` is a comma-separated list, and every quality
is generated in turn and timed individually so the tiers can be compared. Each
image is saved as test_output_<model>_<quality>.png.
"""

import os
import sys
import base64
import asyncio
import aiohttp
import time

ENV_FILE = ".env"
DEFAULT_MODEL = "gpt-image-2.5-flare"

def load_env(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            os.environ.setdefault(key.strip(), val.strip())

def format_duration(seconds):
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    elif seconds < 90:
        return f"{seconds:.1f}s"
    else:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"

def parse_args(argv):
    model, qualities, words = None, ["high"], []
    it = iter(argv)
    for arg in it:
        if arg == "--model":
            model = next(it)
        elif arg == "--quality":
            qualities = [q.strip() for q in next(it).split(",") if q.strip()]
        else:
            words.append(arg)
    return model, qualities, " ".join(words) or "a happy robot painting a landscape"

async def test(prompt, model, quality, api_key):
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "quality": quality,
        "moderation": "low",
    }
    print(f"Sending: {payload}")
    t0 = time.monotonic()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        ) as resp:
            data = await resp.json()
            elapsed = time.monotonic() - t0
            if resp.status == 200:
                img = base64.b64decode(data["data"][0]["b64_json"])
                out = f"test_output_{model}_{quality}.png"
                with open(out, "wb") as f:
                    f.write(img)
                print(f"OK — {model} quality={quality} generated in {format_duration(elapsed)}, "
                      f"{len(img)} bytes, image saved to {out}")
                return elapsed, None
            print(f"ERROR {resp.status} after {format_duration(elapsed)}: {data}")
            return elapsed, data

async def main(prompt, model, qualities, api_key):
    results = []
    for quality in qualities:
        elapsed, error = await test(prompt, model, quality, api_key)
        results.append((quality, elapsed, error))
    print("\nTiming summary:")
    print(f"  {'quality':<8} {'time':>10}  result")
    for quality, elapsed, error in results:
        status = "ok" if error is None else f"FAILED: {error.get('error', {}).get('message', error)}"
        print(f"  {quality:<8} {format_duration(elapsed):>10}  {status}")
    if any(error is not None for _, _, error in results):
        sys.exit(1)

if __name__ == "__main__":
    load_env(ENV_FILE)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print(f"OPENAI_API_KEY not found in {ENV_FILE}")
        sys.exit(1)

    model, qualities, prompt = parse_args(sys.argv[1:])
    model = model or DEFAULT_MODEL
    print(f"Model: {model}")
    print(f"Qualities: {', '.join(qualities)}")
    print(f"Prompt: {prompt}")
    asyncio.run(main(prompt, model, qualities, api_key))
