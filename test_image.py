#!/usr/bin/env python3
"""Quick sanity check for gpt-image-2 API calls."""

import os
import sys
import base64
import asyncio
import aiohttp
import time

ENV_FILE = ".env"

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

async def test(prompt, model, api_key):
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "quality": "high",
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
                out = "test_output.png"
                with open(out, "wb") as f:
                    f.write(img)
                print(f"OK — generated in {format_duration(elapsed)}, image saved to {out}")
            else:
                print(f"ERROR {resp.status} after {format_duration(elapsed)}: {data}")
                sys.exit(1)

if __name__ == "__main__":
    load_env(ENV_FILE)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print(f"OPENAI_API_KEY not found in {ENV_FILE}")
        sys.exit(1)

    prompt = " ".join(sys.argv[1:]) or "a happy robot painting a landscape"
    model = os.environ.get("IMAGE_MODEL", "gpt-image-2")
    print(f"Model: {model}")
    print(f"Prompt: {prompt}")
    asyncio.run(test(prompt, model, api_key))
