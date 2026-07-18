#!/usr/bin/env python3
"""Quick sanity check for the gpt-image-2 /v1/images/edits API call used by &remix."""

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

async def test(prompt, model, api_key, image_paths):
    form = aiohttp.FormData()
    form.add_field("model", model)
    form.add_field("prompt", prompt)
    form.add_field("n", "1")
    form.add_field("size", "1024x1024")
    form.add_field("moderation", "low")
    for i, path in enumerate(image_paths):
        with open(path, "rb") as f:
            form.add_field("image[]", f.read(), filename=f"image_{i}.png", content_type="image/png")

    print(f"Sending edit request. model={model} prompt={prompt!r} images={image_paths}")
    t0 = time.monotonic()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {api_key}"},
            data=form,
        ) as resp:
            data = await resp.json()
            elapsed = time.monotonic() - t0
            if resp.status == 200:
                img = base64.b64decode(data["data"][0]["b64_json"])
                out = "test_remix_output.png"
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

    if len(sys.argv) < 2:
        print("Usage: python test_remix.py <image_path> [image_path2 ...] -- <prompt>")
        sys.exit(1)

    if "--" in sys.argv:
        split = sys.argv.index("--")
        image_paths = sys.argv[1:split]
        prompt = " ".join(sys.argv[split + 1:]) or "creatively reinterpret this image"
    else:
        image_paths = sys.argv[1:]
        prompt = "creatively reinterpret this image"

    model = os.environ.get("IMAGE_MODEL", "gpt-image-2")
    if not model.startswith("gpt-image"):
        model = "gpt-image-2"
    # IMAGE_MODEL may be an internal alias like "gpt-image-2-low"; resolve to the real API model name
    api_model = "gpt-image-2" if model.startswith("gpt-image-2") else model
    print(f"Model: {api_model}")
    asyncio.run(test(prompt, api_model, api_key, image_paths))
