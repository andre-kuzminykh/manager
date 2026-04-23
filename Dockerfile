# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better layer caching.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install .

# Copy source.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY ops ./ops

# Run as non-root.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

# The bot talks to Slack over an outbound WebSocket (Socket Mode); it does not
# expose any TCP port. Container orchestrators that require a listening port
# should use ops/entrypoint_with_health.py instead.
CMD ["python", "-m", "app.main"]
