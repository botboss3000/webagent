"""
Context Control — the per-agent "how full is my context" signal.

This is Part 1 of the Context Control ability: a live gauge of how much of the
agent's context window is in use, surfaced to the agent each user turn so it can
feel itself filling up. Later parts use the same settings/limit to decide when to
compact older turns in the background.

Storage: the per-agent token limit lives in the `context_control` ability's
config bag (the `config` JSON column on the agent_abilities row). When the
ability is disabled for an agent, this whole feature is off — no signal, no
limit, nothing injected.

There is NO max-context tracking yet (that's a future feature). Until then the
limit is a plain token number, defaulting to 200,000.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# The ability id this feature is gated behind (must match the agent_abilities row).
ABILITY_ID = "context_control"

# The token limit (context window) the gauge + compaction measure against. By
# default this is auto-detected from the ACTIVE model's real context window (via
# the model catalog); DEFAULT_TOKEN_LIMIT is only the fallback for a model the
# catalog doesn't know, and the value an admin uses when auto-detect is turned off.
DEFAULT_TOKEN_LIMIT = 200_000
DEFAULT_AUTO_LIMIT = True           # auto-track the active model's window

# ── Compaction defaults (Part 2 of Context Control) ──────────────────────────
# When the assembled context exceeds THRESHOLD x token_limit, the turns that have
# aged past a verbatim "hot tail" of TAIL_FRACTION x token_limit are folded into
# frozen summary "cars" (the compaction train). Car sizes are PROPORTIONAL to the
# limit so they stay sensible whether the window is 32K or 1M: each car is made
# from ~SEGMENT_SOURCE_FRACTION of the window and compressed to ~SEGMENT_TARGET_FRACTION,
# once, then never re-summarised (except the far-back merge past MAX_CARS).
DEFAULT_COMPACT_THRESHOLD = 0.85       # fraction of the limit that triggers compaction
DEFAULT_TAIL_FRACTION = 0.30           # share of the limit kept verbatim (most recent turns)
DEFAULT_SEGMENT_SOURCE_FRACTION = 0.25  # raw turns folded into one car, as a fraction of the limit
DEFAULT_SEGMENT_TARGET_FRACTION = 0.05  # compressed size of one car, as a fraction of the limit
DEFAULT_MAX_CARS = 10                    # cars before the oldest are merged

# Rough chars-per-token ratio for the pre-call estimate. The exact prompt-token
# count is only known *after* a provider call returns usage; this heuristic gives
# the agent a usable gauge at the top of each turn. A later part folds the
# provider's exact count back in for the compaction trigger.
_CHARS_PER_TOKEN = 4


def _defaults() -> Dict[str, Any]:
    return {
        "enabled": False,
        "token_limit": DEFAULT_TOKEN_LIMIT,
        "compaction_enabled": True,
        "compact_threshold": DEFAULT_COMPACT_THRESHOLD,
        "tail_fraction": DEFAULT_TAIL_FRACTION,
        # Absolute car sizes derived from the fractions × the limit (the engine
        # reads absolutes; the proportionality lives here in resolution).
        "segment_source_tokens": int(DEFAULT_SEGMENT_SOURCE_FRACTION * DEFAULT_TOKEN_LIMIT),
        "segment_target_tokens": int(DEFAULT_SEGMENT_TARGET_FRACTION * DEFAULT_TOKEN_LIMIT),
        "max_cars": DEFAULT_MAX_CARS,
        "summary_model": "",
    }


def _as_float(val: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return fallback
    if f <= lo or f >= hi:
        return fallback
    return f


def _coerce_bool(val: Any, fallback: bool) -> bool:
    """Tolerant bool parse. Config panel settings arrive as strings, so a bare
    ``bool("false")`` would wrongly read as True — coerce the string forms."""
    if val is None:
        return fallback
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return fallback


async def _resolve_model_limit(
    db: Any, agent_id: str, session_id: Optional[str], user_id: Optional[str]
) -> Optional[int]:
    """The ACTIVE model's real context window for this run, or None if unknown.

    Resolves the effective model exactly as a run does (app-default → agent →
    session) and reads its context size from the model catalog. None on any
    failure or an unknown model, so the caller falls back to the manual limit.
    """
    if not user_id:
        return None
    try:
        from app.admin.settings import resolve_active_model
        agent_rec = await db.get_agent_by_id(agent_id) if agent_id else None
        active = await resolve_active_model(user_id, agent_rec, session_id)
        model = (active or {}).get("model") or ""
        provider = (active or {}).get("provider") or ""
        if not model:
            return None
        from app import model_catalog
        entry = model_catalog.lookup(model, provider) or {}
        ctx = entry.get("context")
        return int(ctx) if ctx and int(ctx) > 0 else None
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: model-limit resolution failed: %s", e)
        return None


async def get_context_settings(
    db: Any, agent_id: str,
    session_id: Optional[str] = None, user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve Context Control settings for an agent: the fill gauge + compaction.

    Returns a dict with ``enabled`` / ``token_limit`` plus the compaction-train
    knobs (``compaction_enabled``, ``compact_threshold``, ``tail_fraction``,
    ``segment_source_tokens``, ``segment_target_tokens``, ``max_cars``,
    ``summary_model``). Defaults to disabled on any missing row or read error, so a
    failure here never breaks a run.

    ``token_limit`` is **model-aware**: when ``auto_context_limit`` is on (default)
    and ``session_id``/``user_id`` are supplied, it is the ACTIVE model's real
    context window (from the catalog), so the gauge and compaction stay correct
    when the session's model is switched. The stored ``token_limit`` is the
    fallback for an unknown model and the value used when auto-detect is off. The
    car sizes (``segment_*_tokens``) are derived **proportionally** from whatever
    limit results, so they scale with the window.

    Enablement + settings come from the agent's ``agent_connections`` row
    (``section='ability'``, ``connection_type='context_control'``) — the same
    place the Abilities-tab config panel saves to and ``turn_hooks_for_agent`` /
    ``gather_enabled_providers`` read from. Per-field values live under
    ``config.ability_settings.<key>``; a flat ``config.<key>`` is accepted as a
    fallback for any hand-written row.
    """
    out = _defaults()
    if not agent_id:
        return out
    # Safety device: when the ability is locked-on it is always active, even if
    # no per-agent row exists yet or a row says disabled — it cannot be turned
    # off. We still read whatever settings the row carries (or fall back to
    # defaults) so the admin's tuning of the knobs is honoured.
    try:
        from app.abilities import ability_is_locked_on
        locked = ability_is_locked_on(ABILITY_ID)
    except Exception:
        locked = False
    try:
        rows = await db.get_agent_connections(agent_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: could not read connections for %s: %s", agent_id, e)
        rows = []
    matched = None
    for r in rows or []:
        if r.get("connection_type") != ABILITY_ID or r.get("section") != "ability":
            continue
        matched = r
        break
    if matched is None:
        # No row: on only when the ability is a locked-on safety device.
        if not locked:
            return out
        cfg = {}
    else:
        if not matched.get("enabled") and not locked:
            return out
        cfg = matched.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg or "{}")
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    # Settings the config panel saves live under ability_settings; fall back
    # to the bare config bag for any manually-written row.
    s = cfg.get("ability_settings")
    if not isinstance(s, dict):
        s = cfg
    # Layer the ADMIN app-level defaults (Agent Settings → Agent Tools) UNDER the
    # per-agent settings: an agent inherits the admin-set default for any knob it
    # hasn't explicitly chosen, and its own choices win. Mirrors how the global
    # tool-permission defaults layer beneath per-agent overrides.
    try:
        from app.admin import ability_config as _abcfg
        s = _abcfg.effective_ability_config(ABILITY_ID, s if isinstance(s, dict) else {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: admin-default merge skipped: %s", e)
    # Manual limit = the stored number, also the fallback when the model is unknown.
    manual_limit = s.get("token_limit", DEFAULT_TOKEN_LIMIT)
    try:
        manual_limit = int(manual_limit)
    except (TypeError, ValueError):
        manual_limit = DEFAULT_TOKEN_LIMIT
    if manual_limit <= 0:
        manual_limit = DEFAULT_TOKEN_LIMIT

    # Auto-detect the limit from the active model's real window unless turned off.
    auto = _coerce_bool(s.get("auto_context_limit"), DEFAULT_AUTO_LIMIT)
    limit = manual_limit
    if auto:
        model_limit = await _resolve_model_limit(db, agent_id, session_id, user_id)
        if model_limit:
            limit = model_limit

    def _as_int(val: Any, fallback: int, lo: int) -> int:
        try:
            i = int(val)
        except (TypeError, ValueError):
            return fallback
        return i if i >= lo else fallback

    # Car sizes are PROPORTIONAL to the resolved limit, so they scale with the
    # window (a 50k absolute chunk would be larger than a 32k model's whole
    # context, and a rounding error on a 1M one).
    seg_source_frac = _as_float(
        s.get("segment_source_fraction"), DEFAULT_SEGMENT_SOURCE_FRACTION, 0.0, 1.0)
    seg_target_frac = _as_float(
        s.get("segment_target_fraction"), DEFAULT_SEGMENT_TARGET_FRACTION, 0.0, 1.0)

    resolved = _defaults()
    resolved.update({
        "enabled": True,
        "token_limit": limit,
        "auto_context_limit": auto,
        # Auto-compaction is forced ON for a locked-on safety device: the whole
        # point of the lock is that runaway context can never be left ungoverned,
        # so any stored "off" choice is overridden (the config panel no longer
        # offers the toggle). Only a non-locked install can still opt out.
        "compaction_enabled": True if locked else _coerce_bool(s.get("compaction_enabled"), True),
        "compact_threshold": _as_float(
            s.get("compact_threshold"), DEFAULT_COMPACT_THRESHOLD, 0.0, 1.0),
        "tail_fraction": _as_float(
            s.get("tail_fraction"), DEFAULT_TAIL_FRACTION, 0.0, 1.0),
        "segment_source_tokens": max(1000, int(seg_source_frac * limit)),
        "segment_target_tokens": max(200, int(seg_target_frac * limit)),
        "max_cars": _as_int(s.get("max_cars"), DEFAULT_MAX_CARS, 2),
        "summary_model": str(s.get("summary_model") or ""),
    })
    return resolved


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Best-effort token count of an assembled OpenAI-style message list.

    Counts characters across message content and any assistant tool-call payloads,
    then divides by a rough chars-per-token ratio. This is an approximation, not
    the provider's exact prompt-token count — good enough for a fill gauge.
    """
    total_chars = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif c is not None:
            total_chars += len(str(c))
        tc = m.get("tool_calls")
        if tc:
            try:
                total_chars += len(json.dumps(tc))
            except Exception:
                total_chars += len(str(tc))
    return max(0, total_chars // _CHARS_PER_TOKEN)


def context_pct(tokens: int, limit: int) -> int:
    """Percentage of the limit in use, clamped to 0..100+ (can exceed 100)."""
    if limit <= 0:
        return 0
    return int(round(100 * tokens / limit))


def status_line(tokens: int, limit: int) -> str:
    """The block injected into the system prompt so the agent sees its own fill."""
    p = context_pct(tokens, limit)
    return (
        "# [CONTEXT]\n"
        f"Approximate context usage: {tokens:,} / {limit:,} tokens (~{p}% full). "
        "As this nears 100%, older parts of this conversation are automatically "
        "summarized in the background to free space. Nothing is ever deleted - if "
        "you need a detail from earlier that is no longer visible above, search "
        "your past messages to recall it."
    )
