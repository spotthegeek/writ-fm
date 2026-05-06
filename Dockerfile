FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /code/writ-fm

# Install dependencies first — separate layer for cache reuse
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

# Copy application code
COPY . .

# Install the project itself into the venv
RUN uv sync --no-dev

ENV PATH="/code/writ-fm/.venv/bin:$PATH"
