# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /uvx /bin/
# uv hardlinks from its cache by default; that cache is a separate layer/mount,
# so force plain copies to avoid cross-filesystem hardlink failures.
ENV UV_LINK_MODE=copy

WORKDIR /app

# Deps only, before src/ is copied: this layer stays cached across code
# changes and only rebuilds when pyproject.toml or uv.lock change. No
# [build-system] in pyproject.toml, so this installs dependencies only —
# there's no project package for uv to build or install.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/


FROM python:3.12-slim AS crawler

# ffmpeg for ffprobe, used by the video handler to read duration.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser -m appuser

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app/.venv ./.venv
COPY --from=builder --chown=appuser:appuser /app/src ./src
COPY --chown=appuser:appuser migrations/ ./migrations/
RUN chown appuser:appuser /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src"

USER appuser

# ENTRYPOINT, not CMD: `docker compose run crawler stats` (or `crawl <seed>`)
# has to append an argument to this, not replace the whole command -- CMD
# would be replaced wholesale and try to exec a binary literally named
# "stats".
ENTRYPOINT ["python", "-m", "crawler.cli"]
