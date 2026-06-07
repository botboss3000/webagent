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
LABEL vendor="Your Company" \
      description="Production Flask web application" \
      org.opencontainers.image.source="https://github.com/your-org/your-repo"

# ── Hardened system packages ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    tini \
    && rm -rf /var/lib/apt/lists/*

# ── Environment ───────────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    FLASK_DEBUG=0 \
    # Override these at runtime with -e or in your orchestrator
    FLASK_APP=run.py \
    APP_CONFIG=config.ProductionConfig \
    APP_PORT=5000

# ── Create non-root user ──────────────────────────────────────────────────────
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --gid 1001 --no-create-home appuser

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
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--access-logfile", "-", "--error-logfile", "-", "run:app"]