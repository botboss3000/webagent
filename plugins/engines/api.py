"""Alternate-engine REST API — mounted generically like the billing plugin.

Hosts endpoints that talk to the LOCAL CLI harnesses (Codex, …) on behalf of the
browser, which cannot shell out itself. Currently one route:

  GET /api/v1/engines/model-catalog?engine=codex&force=0
      Live model (+ per-model reasoning-effort) options for an alternate engine,
      discovered from the CLI itself rather than hardcoded. The chat footer model
      selector AND the agent Config tab's "Query CLI for latest model options"
      button both use this, so alternate-engine agents show the harness's REAL
      choices without an admin maintaining them.
      Returns {"engine", "source": "cli"|"fallback", "catalog"} — the frontend
      falls back to its curated list when source != "cli".

Auth is deliberately lenient (mirrors /admin/settings/current-model-info): the
caller's token is decoded if present, but the route is not gated — these are
non-sensitive harness metadata (model names/blurbs), not secrets. Plugins are
allowed to import app core (app.auth.jwt) — see plugins/billing/api.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, Query

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/engines", tags=["engines"])


def _resolve_user_id(authorization: str = "", token_qs: str = "") -> str:
    """Best-effort caller id (ANONYMOUS when no valid token) — for logging only."""
    raw = ""
    if authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token_qs:
        raw = token_qs
    if raw:
        payload = decode_token(raw)
        if payload:
            return str(payload.get("user_id", ""))
    return ""


def _catalog_meta(engine: str) -> tuple:
    """(lane, label) for the response — the metadata lane the engine's config is
    saved under, and the human label the UI shows."""
    if engine == "codex":
        return "codex_code", "Codex"
    if engine == "claude_code":
        return "claude_code", "Claude"
    return engine, engine


@router.get("/model-catalog")
async def get_engine_model_catalog(
    engine: str = Query("", alias="engine"),
    force: int = Query(0, alias="force"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Live model options for an alternate engine, read from the local CLI.

    ``engine``: the engine id (metadata.engine) — e.g. "codex".
    ``force=1``: bypass the engine's process cache and re-run the CLI query now
        (what the Config tab's "Query CLI for latest model options" button does).
    """
    _resolve_user_id(authorization or "", token or "")
    engine = (engine or "").strip()
    if not engine:
        return {"engine": "", "source": "fallback", "catalog": None}
    try:
        from plugins.engines import get_engine_model_catalog as _get_cat

        fn = _get_cat(engine)
        if fn is None:  # engine has no CLI catalog (e.g. Claude) → curated fallback
            return {"engine": engine, "source": "fallback", "catalog": None}
        try:
            models = fn(force=bool(force))
        except TypeError:  # hook predates the force param — call without it
            models = fn()
        if not models:
            return {"engine": engine, "source": "fallback", "catalog": None}
        lane, label = _catalog_meta(engine)
        return {
            "engine": engine,
            "source": "cli",
            "catalog": {"lane": lane, "label": label, "models": models, "efforts": None},
        }
    except Exception as e:
        logger.warning("engine model-catalog failed for %s: %s", engine, e)
        return {"engine": engine, "source": "fallback", "catalog": None, "error": str(e)}
