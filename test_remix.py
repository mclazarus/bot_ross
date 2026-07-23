#!/usr/bin/env python3
"""Quick sanity check for the gpt-image-2 /v1/images/edits API call used by &remix.

The OpenAI docs only list 1024x1024/1536x1024/1024x1536/auto for the edit endpoint,
but gpt-image-2 in fact honors an arbitrary WxH there just like the generate endpoint
does -- which is why &remix's --res coerces to an arbitrary valid size rather than
snapping to a standard one. This script sends an arbitrary --size straight to the
endpoint; it defaults to an ultrawide size to exercise exactly that (a wider-than-1.5:1
canvas the edit docs don't advertise). Override with --size WxH (or 'auto').
"""

import os
import sys
import base64
import asyncio
import aiohttp
import time

try:
    import image_size  # to show what production &remix would coerce the size to
except ImportError:
    image_size = None

ENV_FILE = ".env"
# A deliberately ultrawide (2.4:1) size no standard edit size matches -- already a
# valid coerced size (multiple of 16, ratio <= 3), so production &remix now sends it
# verbatim. Override with --size WxH (or 'auto').
DEFAULT_SIZE = "1536x640"

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

async def test(prompt, model, api_key, image_paths, size):
    form = aiohttp.FormData()
    form.add_field("model", model)
    form.add_field("prompt", prompt)
    form.add_field("n", "1")
    form.add_field("size", size)
    form.add_field("moderation", "low")
    for i, path in enumerate(image_paths):
        with open(path, "rb") as f:
            form.add_field("image[]", f.read(), filename=f"image_{i}.png", content_type="image/png")

    print(f"Sending edit request. model={model} size={size} prompt={prompt!r} images={image_paths}")
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

    args = sys.argv[1:]

    # Pull an optional --size WxH (or --size=WxH) out of the args before the usual
    # image-paths / prompt handling, so it can appear anywhere before the '--'.
    size = DEFAULT_SIZE
    cleaned = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--size" and i + 1 < len(args):
            size = args[i + 1]
            i += 2
            continue
        if a.startswith("--size="):
            size = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1
    args = cleaned

    if not args:
        print("Usage: python test_remix.py [--size WxH|auto] <image_path> [image_path2 ...] -- <prompt>")
        print(f"       --size defaults to {DEFAULT_SIZE} (ultrawide, which the edit endpoint honors)")
        sys.exit(1)

    if "--" in args:
        split = args.index("--")
        image_paths = args[:split]
        prompt = " ".join(args[split + 1:]) or "creatively reinterpret this image"
    else:
        image_paths = args
        prompt = "creatively reinterpret this image"

    model = os.environ.get("IMAGE_MODEL", "gpt-image-2")
    if not model.startswith("gpt-image"):
        model = "gpt-image-2"
    # IMAGE_MODEL may be an internal alias like "gpt-image-2-low"; resolve to the real API model name
    api_model = "gpt-image-2" if model.startswith("gpt-image-2") else model
    print(f"Model: {api_model}")

    # For contrast: show what production &remix's --res would coerce this size to
    # (same coercion as the generate path -- it forwards an arbitrary valid size now).
    if image_size is not None and "x" in size.lower():
        try:
            w, h = image_size.parse_resolution(size)
            coerced = image_size.coerce_generation_size(w, h)
            note = "sent as-is" if coerced == size else f"production would coerce to {coerced}"
            print(f"(&remix --res {size} -> {coerced} "
                  f"[{image_size.describe_edit_size(coerced)}]; {note}; this script sends {size} raw)")
        except ValueError:
            pass

    asyncio.run(test(prompt, api_model, api_key, image_paths, size))
