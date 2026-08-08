FROM python:3.10

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
COPY pipes.py .
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
