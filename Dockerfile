FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
