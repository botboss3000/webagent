# =============================================================================
# STAGE 1 — Build dependencies (Python wheels)
# =============================================================================
FROM python:3.12-slim AS builder

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the dependency manifest first — leverages Docker layer caching
# so rebuilds are lightning-fast when requirements haven't changed.
COPY requirements.txt .

RUN pip install --user --upgrade pip setuptools wheel && \
    pip install --user -r requirements.txt

# =============================================================================
# STAGE 2 — Runtime image (tiny, hardened)
# =============================================================================
FROM python:3.12-slim AS runtime

# ── Build metadata ────────────────────────────────────────────────────────────
LABEL description="WebAgent — FastAPI agent harness"

# ── Hardened system packages ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    openssh-client \
    ripgrep \
    tini \
    && rm -rf /var/lib/apt/lists/*

# ── Environment ───────────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8080 \
    HOME=/home/appuser \
    CODEX_HOME=/home/appuser/.codex

# Install the official standalone Codex CLI. Its executable and packaged runtime
# live in the image; login/config state is mounted separately at CODEX_HOME.
ARG CODEX_RELEASE=latest
RUN set -eu; \
    export CODEX_RELEASE="${CODEX_RELEASE}"; \
    export CODEX_HOME=/opt/codex; \
    export CODEX_INSTALL_DIR=/usr/local/bin; \
    export CODEX_NON_INTERACTIVE=true; \
    curl -fsSL https://chatgpt.com/codex/install.sh | sh; \
    codex --version

# ── Create non-root user ──────────────────────────────────────────────────────
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --gid 1001 --home /home/appuser --no-create-home appuser && \
    mkdir -p /home/appuser/.codex && \
    chown -R appuser:appgroup /home/appuser

# The checkout is bind-mounted from the host (uid 1000) while WebAgent and Codex
# run as appuser (uid 1001, supplementary gid 1000).
RUN git config --system --add safe.directory /app && \
    git config --system --add safe.directory /workspace/webagent

# ── Copy built Python packages from builder ───────────────────────────────────
COPY --from=builder /root/.local /home/appuser/.local
RUN chown -R appuser:appgroup /home/appuser/.local

# Make sure the local binaries are on PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# ── Application code ──────────────────────────────────────────────────────────
WORKDIR /app
COPY --chown=appuser:appgroup . .

# ── Health check ──────────────────────────────────────────────────────────────
# Expects a route /health that returns 200.  Tweak the path to match your app.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail http://localhost:${APP_PORT}/health || exit 1

# ── Run ───────────────────────────────────────────────────────────────────────
USER appuser:appgroup
EXPOSE ${APP_PORT}

# tini reaps zombie processes and forwards signals cleanly
ENTRYPOINT ["/usr/bin/tini", "--"]
# WebAgent is an ASGI app (app.main:app) served by uvicorn on :8080 — the same
# server, port and WebSocket stack (wsproto) that run.py uses. Multi-worker is
# supported and correct: the singleton background loops run in ONE worker via the
# leader lock (app/coordination/leader.py), and cross-worker chat turns stream via
# the client DB-tail reconcile (see README / docs/claude/deployment.md). Single
# worker gives the smoothest WS streaming — set --workers 1 for that.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4", "--ws", "wsproto"]