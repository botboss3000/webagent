"""
Context Control — per-agent compaction targets under an app-wide hard ceiling.

This is Part 1 of the Context Control ability: a live gauge of how much of the
agent's context window is in use, surfaced to the agent each user turn so it can
feel itself filling up. Later parts use the same settings/limit to decide when to
compact older turns in the background.

Storage: the app-level Context Control config owns the maximum token ceiling.
Each agent's ability connection owns its compaction target, verbatim-tail amount,
and self-compaction posture. The runtime layers those scopes and always clamps an
auto-detected model window to the app maximum.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The ability id this feature is gated behind (must match the agent_abilities row).
ABILITY_ID = "context_control"

# The token limit (context window) the gauge + compaction measure against. By
# default this is auto-detected from the ACTIVE model's real context window (via
# the model catalog); DEFAULT_TOKEN_LIMIT is only the fallback for a model the
# catalog doesn't know, and the value an admin uses when auto-detect is turned off.
DEFAULT_TOKEN_LIMIT = 1_050_000
DEFAULT_AUTO_LIMIT = True           # auto-track the active model's window

# ── Compaction defaults (Part 2 of Context Control) ──────────────────────────
# When assembled context exceeds the absolute target, turns that have aged past
# the absolute verbatim hot-tail budget are folded into
# frozen summary "cars" (the compaction train). Car sizes are proportional to the
# per-agent target: each car is made from ~SEGMENT_SOURCE_FRACTION of that target
# and compressed to ~SEGMENT_TARGET_FRACTION,
# once, then never re-summarised (except the far-back merge past MAX_CARS).
DEFAULT_COMPACT_TARGET_TOKENS = 100_000
DEFAULT_VERBATIM_TAIL_TOKENS = 30_000
DEFAULT_SEGMENT_SOURCE_FRACTION = 0.25  # raw turns folded into one car, as a fraction of the limit
DEFAULT_SEGMENT_TARGET_FRACTION = 0.05  # compressed size of one car, as a fraction of the limit
DEFAULT_MAX_CARS = 10                    # cars before the oldest are merged

# Historical tool-evidence policy. ``current_task`` preserves the behaviour
# shipped before this became agent-configurable. A "run" is one genuine user
# turn plus the assistant/tool work it triggered (internal wake-ups do not count).
TOOL_EVIDENCE_POLICIES = frozenset({
    "current_task", "recent_runs", "token_budget", "hybrid", "all",
})
DEFAULT_TOOL_EVIDENCE_POLICY = "current_task"
DEFAULT_FULL_EVIDENCE_RUNS = 2
DEFAULT_FULL_EVIDENCE_TOKEN_BUDGET = 40_000

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
        "compact_target_tokens": DEFAULT_COMPACT_TARGET_TOKENS,
        "verbatim_tail_tokens": DEFAULT_VERBATIM_TAIL_TOKENS,
        # Absolute car sizes derived from the fractions × the limit (the engine
        # reads absolutes; the proportionality lives here in resolution).
        "segment_source_tokens": int(
            DEFAULT_SEGMENT_SOURCE_FRACTION * DEFAULT_COMPACT_TARGET_TOKENS),
        "segment_target_tokens": int(
            DEFAULT_SEGMENT_TARGET_FRACTION * DEFAULT_COMPACT_TARGET_TOKENS),
        "max_cars": DEFAULT_MAX_CARS,
        "summary_model": "",
        "tool_evidence_policy": DEFAULT_TOOL_EVIDENCE_POLICY,
        "full_evidence_runs": DEFAULT_FULL_EVIDENCE_RUNS,
        "full_evidence_token_budget": DEFAULT_FULL_EVIDENCE_TOKEN_BUDGET,
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


def normalize_tool_evidence_settings(values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a safe, complete tool-evidence policy.

    This deliberately accepts a plain override bag so a future member-profile
    resolver can layer ``member > agent > app`` without teaching history
    assembly about membership or entitlement storage.
    """
    values = values if isinstance(values, dict) else {}
    policy = str(values.get("tool_evidence_policy") or "").strip().lower()
    if policy not in TOOL_EVIDENCE_POLICIES:
        policy = DEFAULT_TOOL_EVIDENCE_POLICY

    def _bounded_int(key: str, fallback: int, lo: int, hi: int) -> int:
        try:
            value = int(float(values.get(key, fallback)))
        except (TypeError, ValueError):
            value = fallback
        return min(hi, max(lo, value))

    return {
        "tool_evidence_policy": policy,
        "full_evidence_runs": _bounded_int(
            "full_evidence_runs", DEFAULT_FULL_EVIDENCE_RUNS, 1, 50),
        "full_evidence_token_budget": _bounded_int(
            "full_evidence_token_budget",
            DEFAULT_FULL_EVIDENCE_TOKEN_BUDGET, 1_000, DEFAULT_TOKEN_LIMIT),
    }


def apply_tool_evidence_override(
    settings: Dict[str, Any], override: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Layer a member/profile override onto resolved Context Control settings.

    No member-profile store is assumed here. When that store is introduced its
    resolver only needs to pass the permitted override bag through this seam.
    """
    out = dict(settings or {})
    if isinstance(override, dict):
        merged = {**out, **{
            key: override[key] for key in (
                "tool_evidence_policy", "full_evidence_runs",
                "full_evidence_token_budget",
            ) if key in override
        }}
    else:
        merged = out
    out.update(normalize_tool_evidence_settings(merged))
    return out


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
    knobs (``compaction_enabled``, ``compact_target_tokens``,
    ``verbatim_tail_tokens``,
    ``segment_source_tokens``, ``segment_target_tokens``, ``max_cars``,
    ``summary_model``). Defaults to disabled on any missing row or read error, so a
    failure here never breaks a run.

    ``token_limit`` is **model-aware**: when ``auto_context_limit`` is on (default)
    and ``session_id``/``user_id`` are supplied, it is the ACTIVE model's real
    context window (from the catalog), so the gauge and compaction stay correct
    when the session's model is switched. The stored ``token_limit`` is the
    fallback for an unknown model and the value used when auto-detect is off. The
    car sizes (``segment_*_tokens``) are derived proportionally from the agent's
    absolute compaction target, so a high app ceiling does not create huge cars.

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
    agent_values = dict(s) if isinstance(s, dict) else {}
    # Layer the ADMIN app-level defaults (App Settings → App Functions) UNDER the
    # per-agent settings: an agent inherits the admin-set default for any knob it
    # hasn't explicitly chosen, and its own choices win. Mirrors how the global
    # tool-permission defaults layer beneath per-agent overrides.
    try:
        from app.admin import ability_config as _abcfg
        s = _abcfg.effective_ability_config(ABILITY_ID, s if isinstance(s, dict) else {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: admin-default merge skipped: %s", e)
    # App-level maximum = the stored number, also the fallback when the model is
    # unknown.  The descriptor marks this field with ceiling=max, so legacy
    # per-agent values can only make the effective limit smaller.
    manual_limit = s.get("token_limit", DEFAULT_TOKEN_LIMIT)
    try:
        manual_limit = int(manual_limit)
    except (TypeError, ValueError):
        manual_limit = DEFAULT_TOKEN_LIMIT
    if manual_limit <= 0:
        manual_limit = DEFAULT_TOKEN_LIMIT

    # Auto-detect the active model's real window unless turned off, but never let
    # it exceed the app-wide ceiling. Large provider windows therefore cannot
    # silently defeat the administrator's RAM/request-size guardrail.
    auto = _coerce_bool(s.get("auto_context_limit"), DEFAULT_AUTO_LIMIT)
    limit = manual_limit
    if auto:
        model_limit = await _resolve_model_limit(db, agent_id, session_id, user_id)
        if model_limit:
            limit = min(model_limit, manual_limit)

    def _as_int(val: Any, fallback: int, lo: int) -> int:
        try:
            i = int(float(val))
        except (TypeError, ValueError):
            return fallback
        return i if i >= lo else fallback

    # Absolute per-agent budgets. Legacy percentage/fraction keys remain readable
    # so existing agent/session rows migrate without a database rewrite; every new
    # save uses the token keys.
    target_raw = s.get("compact_target_tokens")
    legacy_target = s.get("compact_threshold")
    if ("compact_target_tokens" not in agent_values
            and agent_values.get("compact_threshold") is not None):
        legacy_target = agent_values["compact_threshold"]
        target_raw = None
    if target_raw is None and legacy_target is not None:
        target_raw = _as_float(
            legacy_target, 0.85, 0.0, 1.0) * limit
    target_tokens = min(limit, _as_int(
        target_raw, min(DEFAULT_COMPACT_TARGET_TOKENS, limit), 1_000))

    verbatim_raw = s.get("verbatim_tail_tokens")
    legacy_verbatim = s.get("tail_fraction")
    if ("verbatim_tail_tokens" not in agent_values
            and agent_values.get("tail_fraction") is not None):
        legacy_verbatim = agent_values["tail_fraction"]
        verbatim_raw = None
    if verbatim_raw is None and legacy_verbatim is not None:
        verbatim_raw = _as_float(
            legacy_verbatim, 0.30, 0.0, 1.0) * limit
    verbatim_tokens = _as_int(
        verbatim_raw, min(DEFAULT_VERBATIM_TAIL_TOKENS, target_tokens), 1_000)
    verbatim_tokens = min(verbatim_tokens, max(1_000, target_tokens - 1_000))

    # Car sizes are proportional to the agent's compaction TARGET, not the much
    # larger app ceiling. A 100K target therefore produces ~25K source / ~5K
    # summary cars even when the provider supports a million-token window.
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
        "compact_target_tokens": target_tokens,
        "verbatim_tail_tokens": verbatim_tokens,
        "segment_source_tokens": max(1000, int(seg_source_frac * target_tokens)),
        "segment_target_tokens": max(200, int(seg_target_frac * target_tokens)),
        "max_cars": _as_int(s.get("max_cars"), DEFAULT_MAX_CARS, 2),
        "summary_model": str(s.get("summary_model") or ""),
        **normalize_tool_evidence_settings(s),
    })

    # Per-session override (saved from the chat footer's compaction panel) wins over
    # the agent's stored knobs for THIS one conversation — mirroring how the
    # per-session model override layers over the agent/app default. Only the two
    # user-facing token budgets can be tuned per-chat; everything else stays agent-wide.
    if session_id:
        try:
            ov = await db.get_session_context_override(session_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("context_control: session override read skipped: %s", e)
            ov = None
        if isinstance(ov, dict):
            if ov.get("compact_target_tokens") is not None:
                resolved["compact_target_tokens"] = min(limit, _as_int(
                    ov.get("compact_target_tokens"),
                    resolved["compact_target_tokens"], 1_000))
            elif ov.get("compact_threshold") is not None:  # legacy fraction
                resolved["compact_target_tokens"] = min(limit, int(
                    _as_float(ov.get("compact_threshold"), 0.85, 0.0, 1.0) * limit))
            if ov.get("verbatim_tail_tokens") is not None:
                resolved["verbatim_tail_tokens"] = _as_int(
                    ov.get("verbatim_tail_tokens"),
                    resolved["verbatim_tail_tokens"], 1_000)
            elif ov.get("tail_fraction") is not None:  # legacy fraction
                resolved["verbatim_tail_tokens"] = int(
                    _as_float(ov.get("tail_fraction"), 0.30, 0.0, 1.0) * limit)
            resolved["verbatim_tail_tokens"] = min(
                resolved["verbatim_tail_tokens"],
                max(1_000, resolved["compact_target_tokens"] - 1_000),
            )

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


def status_line(
    tokens: int, limit: int,
    compact_target_tokens: int = DEFAULT_COMPACT_TARGET_TOKENS,
) -> str:
    """The block injected into the system prompt so the agent sees its own fill."""
    p = context_pct(tokens, limit)
    return (
        "# [CONTEXT]\n"
        f"Approximate context usage: {tokens:,} / {limit:,} tokens (~{p}% full). "
        f"At the configured {compact_target_tokens:,}-token compaction target, older parts of this "
        "conversation are automatically "
        "summarized in the background to free space. Nothing is ever deleted - if "
        "you need a detail from earlier that is no longer visible above, search "
        "your past messages to recall it."
    )
