# Must stay >= the Python the code is developed and tested against (3.14), or syntax
# that passes every local check can SyntaxError at container start -- that already
# happened once with a PEP 701 nested f-string, which 3.10 rejects and 3.12 accepts.
# Pinned by DockerfilePythonVersionTest in test_bot_ross_source.py; downgrading means
# auditing the source for newer syntax first, not just editing this line.
FROM python:3.14

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
