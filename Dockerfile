FROM python:3.10

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_ross.py .
# Seed library only. At startup bot_ross copies this to data/magic_prompts.json (the
# persistent volume) if that file is absent, so user-added mixins survive redeploys.
COPY magic_prompts.json .

RUN mkdir -p /app/data

CMD ["python", "bot_ross.py"]
