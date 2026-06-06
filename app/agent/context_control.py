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

# Fallback token limit when the ability is enabled but no custom value is saved.
# There is no per-model max-context lookup yet, so every agent starts at 200K.
DEFAULT_TOKEN_LIMIT = 200_000

# ── Compaction defaults (Part 2 of Context Control) ──────────────────────────
# When the assembled context exceeds THRESHOLD x token_limit, the older turns are
# folded into a rolling summary, keeping a verbatim "hot tail" of TAIL_FRACTION x
# token_limit. See app/agent/compaction.py.
DEFAULT_COMPACT_THRESHOLD = 0.85   # fraction of the limit that triggers compaction
DEFAULT_TAIL_FRACTION = 0.30       # share of the limit kept verbatim (most recent turns)
DEFAULT_SUMMARY_TARGET_TOKENS = 1500  # rough target size of the rolling summary

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
        "summary_target_tokens": DEFAULT_SUMMARY_TARGET_TOKENS,
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


async def get_context_settings(db: Any, agent_id: str) -> Dict[str, Any]:
    """Resolve Context Control settings for an agent: the fill gauge + compaction.

    Returns a dict with ``enabled`` / ``token_limit`` plus the compaction knobs
    (``compaction_enabled``, ``compact_threshold``, ``tail_fraction``,
    ``summary_target_tokens``, ``summary_model``). Defaults to disabled on any
    missing row or read error, so a failure here never breaks a run.
    """
    out = _defaults()
    if not agent_id:
        return out
    try:
        rows = await db.get_agent_abilities(agent_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: could not read abilities for %s: %s", agent_id, e)
        return out
    for r in rows or []:
        if r.get("ability_id") != ABILITY_ID:
            continue
        if not r.get("enabled"):
            return out
        cfg = r.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        limit = cfg.get("token_limit", DEFAULT_TOKEN_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = DEFAULT_TOKEN_LIMIT
        if limit <= 0:
            limit = DEFAULT_TOKEN_LIMIT
        resolved = _defaults()
        resolved.update({
            "enabled": True,
            "token_limit": limit,
            "compaction_enabled": bool(cfg.get("compaction_enabled", True)),
            "compact_threshold": _as_float(
                cfg.get("compact_threshold"), DEFAULT_COMPACT_THRESHOLD, 0.0, 1.0),
            "tail_fraction": _as_float(
                cfg.get("tail_fraction"), DEFAULT_TAIL_FRACTION, 0.0, 1.0),
            "summary_target_tokens": int(cfg.get("summary_target_tokens", DEFAULT_SUMMARY_TARGET_TOKENS) or DEFAULT_SUMMARY_TARGET_TOKENS),
            "summary_model": str(cfg.get("summary_model") or ""),
        })
        return resolved
    return out


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
