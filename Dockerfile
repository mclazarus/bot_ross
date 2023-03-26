FROM python:3.10

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_ross.py .

RUN mkdir -p /app/data

CMD ["python", "bot_ross.py"]
