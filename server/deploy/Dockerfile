# MIGRATION ARTIFACT — v1 runs natively on Windows (scripts/start-engram.ps1).
# Use this when moving Engram to a VM. Build context = server/ :
#   docker compose -f deploy/compose.yml up -d --build
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer first (cache-friendly), then the package itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY engram_server ./engram_server
RUN uv sync --frozen --no-dev

# /state is the ONLY stateful mount: brain checkout + deploy key + oauth store.
ENV PYTHONUTF8=1 \
    ENGRAM_MCP_HOST=0.0.0.0 \
    ENGRAM_MCP_PORT=9210 \
    ENGRAM_BRAIN_PATH=/state/brain \
    ENGRAM_DEPLOY_KEY_PATH=/state/id_engram \
    ENGRAM_OAUTH_STORE_PATH=/state/oauth_store.json

EXPOSE 9210
CMD ["uv", "run", "--no-dev", "engram-server"]
