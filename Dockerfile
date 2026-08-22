# syntax=docker/dockerfile:1

# ---- frontend build ----------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


# ---- backend runtime -----------------------------------------------------
# Playwright's Python wheel and its browser binaries are tightly version-coupled,
# and this image already ships the OS-level libraries Chromium needs headless —
# keep the tag in sync with the `playwright` version pinned in uv.lock.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

# uv manages this project's env the same way it does in local dev (see README).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8088

# Dependencies first, in their own layer, so editing source code doesn't bust the
# (slow) dependency-install cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY scrape.py ./
COPY src/ ./src/
RUN uv sync --frozen

# The base image doesn't pre-install browser binaries; download the one matching
# our pinned playwright version. OS deps are already present in this image.
RUN uv run playwright install chromium

# Production frontend build, served by FastAPI's StaticFiles mount (server.py).
COPY --from=frontend /web/dist ./web/dist

EXPOSE 8088

CMD ["uv", "run", "scrape-ui"]
