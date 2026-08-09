# Must stay >= the Python the code is developed and tested against (3.14), or syntax
# that passes every local check can SyntaxError at container start -- that already
# happened once with a PEP 701 nested f-string, which 3.10 rejects and 3.12 accepts.
# Pinned by DockerfilePythonVersionTest in test_bot_ross_source.py; downgrading means
# auditing the source for newer syntax first, not just editing this line.
FROM python:3.14 AS base

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_ross.py .
COPY release_image.py .
COPY magic_paint.py .
COPY json_library.py .
COPY macros.py .
COPY image_size.py .
COPY daily_schedule.py .
COPY pipe_chain.py .
COPY message_links.py .
# Seed library only. At startup bot_ross copies this to data/magic_prompts.json (the
# persistent volume) if that file is absent, so user-added mixins survive redeploys.
COPY magic_prompts.json .
# Seed library only, same pattern as magic_prompts.json above -- copied to
# data/macros.json on first run if absent, so user-added macros survive redeploys.
COPY macros.json .
# Seed schedule only, same pattern as macros.json -- copied to
# data/daily_schedule.json on first run if absent, so hand edits to the daily-image
# schedule survive redeploys.
COPY daily_schedule.json .
# Versioned, read-only word lists for &release_image. Static content, read straight
# from the image (never copied to data/) so release avatars stay reproducible.
COPY release_algorithms.json .

RUN mkdir -p /app/data /app/data/daily_images

CMD ["python", "bot_ross.py"]

# ---------------------------------------------------------------------------
# Test stage: runs the local unit-test gate against the EXACT interpreter and
# wheel set the runtime image ships, at build time. build.sh builds this target
# first, so a failing suite can never produce a shippable bot_ross image.
# NOTE: the runtime image is `--target base` (see build.sh) -- a bare
# `docker build -t bot_ross .` would wrongly tag this test stage instead.
FROM base AS test

# Repo artifacts some tests read (never shipped in the runtime image):
# test_bot_ross_source reads Dockerfile and requirements.txt (the latter is
# already in base), test_pipe_chain reads README.md, test_daily_schedule reads
# env.example, and test_bot_ross_source's DocsTruthTest reads CLAUDE.md and
# README.md.
COPY CLAUDE.md Dockerfile README.md env.example ./

# Test modules listed EXPLICITLY, never `COPY test_*.py` and never `unittest
# discover`: test_image.py and test_remix.py hit the live OpenAI API and spend
# real money, so they must stay out of the image entirely -- keeping them
# un-copied means even a future `discover` inside this stage cannot find them.
COPY test_daily_schedule.py test_pipe_chain.py test_message_links.py \
     test_image_size.py test_macros.py test_magic_paint.py \
     test_release_image.py test_bot_ross_source.py test_bot_ross_commands.py \
     test_bot_ross_config.py test_runtime_deps.py ./

RUN python -m unittest test_daily_schedule test_pipe_chain test_message_links \
    test_image_size test_macros test_magic_paint test_release_image \
    test_bot_ross_source test_bot_ross_commands test_bot_ross_config \
    test_runtime_deps
