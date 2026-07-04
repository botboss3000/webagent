"""Combined boot aggregator — GET /api/v1/boot.

ONE round-trip that gathers the read-only payloads the web app fetches at page
boot, so the browser drains the boot burst in ~1 request instead of ~15 queued
behind the HTTP/1.1 six-socket-per-origin cap (the measured cold-load
bottleneck — see memory boot-perf-next-steps-plan + hybrid-vs-local-perf-audit).

This is a PURE ACCELERATOR, not a new behaviour:

  * Every section is OPTIONAL. If a section errors it is simply omitted, and the
    frontend falls back to its own per-endpoint fetch (a coalescer cache "miss"),
    so a broken section degrades to exactly today's behaviour — never a failed
    boot.
  * Each section is produced by calling the SAME handler function the individual
    endpoint uses, from within THIS request's context. So caller identity
    (resolved once by CallerIdentityMiddleware into a contextvar) and every
    per-endpoint authorization check run identically — there is zero duplicated
    or bypassed auth logic here.

The client side is ui/shared/js (index.html boot coalescer): it fires /boot once,
early, and primes the fetch-coalescer cache from `sections`, keyed by the URL each
module would otherwise fetch. See the coalescer script in index.html.

NOTE — calling FastAPI route functions directly (not via the router) means their
``= Query(default)`` parameters carry the Query() FieldInfo object as the default,
NOT the resolved value. So every parameter is passed EXPLICITLY below; do not rely
on a handler's declared default when calling it here.

Scope (Phase 1): the stable-key, critical-path boot reads. The variable-keyed
session-transcript reads (/db/session-messages with its per-open limit/around_id,
and /db/sessions/{id}/related) are intentionally NOT aggregated here — their cache
keys are not reconstructable up front, so they keep flowing through the normal
coalescer + network. Documented so this is a known, not silent, boundary.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["boot"])


def _plain(value):
    """Coerce a handler return (pydantic model or plain dict/list) to JSON-able."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value


@router.get("/boot")
async def boot(request: Request):
    """Aggregate the read-only page-boot payloads into one response.

    Returns ``{ "sections": { <name>: <body> | (omitted on error) },
    "default_agent_id": <id|null> }``. Anonymous callers get only the public
    sections; user-scoped sections require a verified caller.
    """
    # Lazy imports keep this module free of import-time cycles (it is included
    # after all of these routers in app/main.py). Matches the in-handler import
    # style the aggregated handlers themselves use.
    from app.auth.identity import request_user_id
    from app.auth import access_mode as _access_mode, ui_config as _ui_config
    from app.api.features import get_app_prompts, get_app_prompts_section
    from app.api.chat import get_suggestions_config
    from app.api.agents import (
        get_abilities_catalog,
        get_pages_catalog,
        list_agents,
        get_user_profile,
        list_agent_abilities,
    )
    from app.api.files import check_access
    from app.api.db_viewer import list_sessions

    try:
        uid = request_user_id(request) or ""
    except Exception:
        uid = ""

    sections: dict = {}

    async def _gather(name: str, awaitable):
        """Await one section; on ANY error drop it so the client re-fetches it."""
        try:
            sections[name] = _plain(await awaitable)
        except Exception as e:  # noqa: BLE001 - best-effort by design
            logger.debug("boot: section %r skipped (%s)", name, e)

    # ── Public sections (no identity required) ──────────────────────────────
    tasks = [
        _gather("access_mode", _access_mode()),
        _gather("ui_config", _ui_config(request)),
        _gather("app_prompts", get_app_prompts()),
        _gather("ui_messages", get_app_prompts_section("ui-messages")),
        _gather("suggestions_config", get_suggestions_config()),
        _gather("abilities_catalog", get_abilities_catalog()),
        _gather("pages_catalog", get_pages_catalog()),
    ]

    # ── User-scoped sections (only with a verified caller) ──────────────────
    # Every Query() param is passed explicitly — see module docstring.
    if uid:
        tasks += [
            _gather("check_access", check_access(request)),
            _gather("agents", list_agents(request, user_id=uid,
                                          include_system=False, view="active")),
            _gather("profile", get_user_profile(request, user_id=uid)),
            _gather("sessions", asyncio.to_thread(
                list_sessions, request, user_id=uid, db="local.db",
                agent_id=None, limit=50, include_hidden=False)),
        ]

    await asyncio.gather(*tasks)

    # ── Chained tail: abilities of the agent the UI is about to open. This is
    # the request that most often forms the ~10s cold-load tail because it is
    # chained AFTER the roster. Resolving the RIGHT agent matters — a wrong guess
    # just means the UI re-fetches (a primed miss, still correct, but the stall
    # returns). Priority:
    #   1. the agent bound to the session the UI will open (client sends
    #      ?session_id=) — this is exactly the agent whose abilities it will ask
    #      for, read straight off the sessions list we already loaded;
    #   2. the user's declared default agent;
    #   3. the provisioned WebAgent singleton, else the first agent.
    default_agent_id = None
    if uid:
        profile = sections.get("profile") or {}
        agents = (sections.get("agents") or {}).get("agents") or []
        agent_ids = {a.get("id") for a in agents}

        session_id = request.query_params.get("session_id") or ""
        if session_id:
            for s in (sections.get("sessions") or {}).get("sessions") or []:
                if s.get("id") == session_id and s.get("agent_id"):
                    default_agent_id = s.get("agent_id")
                    break

        if not default_agent_id:
            cand = profile.get("default_agent_id")
            if cand and cand in agent_ids:
                default_agent_id = cand

        if not default_agent_id:
            for a in agents:
                if a.get("is_user_default"):
                    default_agent_id = a.get("id")
                    break
            if not default_agent_id and agents:
                default_agent_id = agents[0].get("id")

        if default_agent_id:
            await _gather(
                "agent_abilities",
                list_agent_abilities(request, default_agent_id, user_id=uid),
            )

    return {"sections": sections, "default_agent_id": default_agent_id}
