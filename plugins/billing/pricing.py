"""Pricing engine — the single source of truth for what a chat costs.

resolve_charge() is the only place pricing logic lives. The chat loop, the
admin UIs, and the test suite all go through it. Strategies dispatch off
billing_configs.strategy; exemptions short-circuit before any strategy runs.

Money is always integer cents.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from plugins.billing.extensions import apply_config_augment

logger = logging.getLogger(__name__)


# ── Types ──────────────────────────────────────────────────────────────────


class Strategy(str, Enum):
    FREE = "free"
    CREDITS = "credits"
    PER_MESSAGE = "per_message"
    PER_TOKEN = "per_token"
    SUBSCRIPTION = "subscription"
    TRIAL = "trial"


@dataclass
class Usage:
    """Output of one LLM call. Tokens come straight from chunk.usage."""
    input_tokens: int = 0
    output_tokens: int = 0
    provider_cost_cents: int = 0  # raw provider cost (markup added later)
    message_count: int = 1


@dataclass
class ChargeResult:
    """What the end user pays for one LLM call (the agent admin keeps it).

    Any allocation of that charge is applied downstream by the optional billing
    extension; this calculation does not know about it."""
    end_user_charge_cents: int = 0
    strategy: str = "free"
    is_byo_llm: bool = False
    is_trial: bool = False
    is_exempt: bool = False
    exempt_reason: Optional[str] = None
    insufficient_funds: bool = False
    notes: Dict[str, Any] = field(default_factory=dict)


# ── Config loading ─────────────────────────────────────────────────────────


_DEFAULT_RATE_CARD = {
    "credits_per_message_cents": 100,      # 1 credit = 1 cent; flat 100¢/msg
    "credits_per_1k_input_tokens": 50,
    "credits_per_1k_output_tokens": 150,
    "per_message_cents": 100,
    "per_1k_input_token_cents": 50,
    "per_1k_output_token_cents": 150,
    "llm_markup_pct": 50.0,                # only applied for default-LLM
    "min_charge_cents": 1,
}


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def _row_to_config(row: Optional[dict]) -> dict:
    if not row:
        return {}
    return {
        "scope": row.get("scope"),
        "strategy": row.get("strategy") or "free",
        "allowed_strategies": _parse_json(row.get("allowed_strategies"), []),
        "allowed_processors": _parse_json(row.get("allowed_processors"), []),
        "rate_card_default_llm": _parse_json(row.get("rate_card_default_llm"), {}),
        "rate_card_byo_llm": _parse_json(row.get("rate_card_byo_llm"), {}),
        "trial_config": _parse_json(row.get("trial_config"), {}),
        "subscription_price_cents": int(row.get("subscription_price_cents") or 0),
        "currency": row.get("currency") or "usd",
    }


def _empty_config() -> dict:
    return _row_to_config({})


async def load_effective_config(db: Any, agent_id: str) -> dict:
    """Return the agent's own billing config (defaults to 'free' when unset, so
    pre-existing agents stay free until their admin configures billing).

    An optional billing extension, if installed, may augment this with extra
    defaults/limits; otherwise the agent config stands alone and the agent admin
    keeps 100% of every charge."""
    if not _has_billing_tables(db):
        return _empty_config()
    agent_row = await _fetch_one(db, "billing_configs", {"scope": f"agent:{agent_id}"})
    cfg = _row_to_config(agent_row) if agent_row else _empty_config()
    return await apply_config_augment(db, agent_id, cfg)


# ── Exemption check ────────────────────────────────────────────────────────


async def is_exempt(db: Any, agent_id: str, user_id: str) -> Optional[str]:
    """Return a string reason if exempt, else None. Cheap — three indexed lookups."""
    if not _has_billing_tables(db):
        return None
    # Agent-wide
    row = await _fetch_one(db, "billing_exemptions", {"kind": "agent", "agent_id": agent_id})
    if row:
        return "agent"
    # User-global
    row = await _fetch_one(db, "billing_exemptions", {"kind": "user", "user_id": user_id})
    if row:
        return "user"
    # User-for-agent
    row = await _fetch_one(db, "billing_exemptions", {
        "kind": "user_for_agent",
        "agent_id": agent_id,
        "user_id": user_id,
    })
    if row:
        return "user_for_agent"
    return None


# ── BYO-LLM detection ──────────────────────────────────────────────────────


def _is_byo_llm(agent: dict) -> bool:
    """True when the agent ships its own LLM key. Pricing uses BYO rate card."""
    meta_raw = agent.get("metadata")
    if isinstance(meta_raw, str):
        try:
            meta = json.loads(meta_raw)
        except Exception:
            meta = {}
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        meta = {}
    llm_config = meta.get("llm_config") or {}
    if llm_config.get("use_default") is True:
        return False
    # Treat presence of a non-empty api_key / secret_ref as BYO
    if llm_config.get("api_key") or llm_config.get("secret_ref"):
        return True
    if llm_config.get("byo") is True:
        return True
    return False


# ── Strategy implementations ──────────────────────────────────────────────


def _rate_card_for(agent: dict, cfg: dict) -> dict:
    byo = _is_byo_llm(agent)
    card_raw = cfg.get("rate_card_byo_llm") if byo else cfg.get("rate_card_default_llm")
    card = dict(_DEFAULT_RATE_CARD)
    if isinstance(card_raw, dict):
        card.update({k: v for k, v in card_raw.items() if v is not None})
    return card


def _strategy_credits(usage: Usage, agent: dict, cfg: dict) -> int:
    card = _rate_card_for(agent, cfg)
    # Charge per-token if rates are set, else fall back to flat-per-message.
    in_rate = card.get("credits_per_1k_input_tokens", 0)
    out_rate = card.get("credits_per_1k_output_tokens", 0)
    if in_rate or out_rate:
        cents = (
            usage.input_tokens * in_rate / 1000.0
            + usage.output_tokens * out_rate / 1000.0
        )
    else:
        cents = card.get("credits_per_message_cents", 0) * usage.message_count
    if not _is_byo_llm(agent):
        markup = float(card.get("llm_markup_pct", 0))
        if markup:
            cents += usage.provider_cost_cents * markup / 100.0
    return max(int(round(cents)), int(card.get("min_charge_cents", 1)))


def _strategy_per_message(usage: Usage, agent: dict, cfg: dict) -> int:
    card = _rate_card_for(agent, cfg)
    cents = card.get("per_message_cents", 0) * usage.message_count
    if not _is_byo_llm(agent):
        markup = float(card.get("llm_markup_pct", 0))
        if markup:
            cents += usage.provider_cost_cents * markup / 100.0
    return max(int(round(cents)), int(card.get("min_charge_cents", 1)))


def _strategy_per_token(usage: Usage, agent: dict, cfg: dict) -> int:
    card = _rate_card_for(agent, cfg)
    in_rate = card.get("per_1k_input_token_cents", 0)
    out_rate = card.get("per_1k_output_token_cents", 0)
    cents = (
        usage.input_tokens * in_rate / 1000.0
        + usage.output_tokens * out_rate / 1000.0
    )
    if not _is_byo_llm(agent):
        markup = float(card.get("llm_markup_pct", 0))
        if markup:
            cents += usage.provider_cost_cents * markup / 100.0
    return max(int(round(cents)), int(card.get("min_charge_cents", 1)))


_STRATEGY_DISPATCH = {
    Strategy.CREDITS.value: _strategy_credits,
    Strategy.PER_MESSAGE.value: _strategy_per_message,
    Strategy.PER_TOKEN.value: _strategy_per_token,
}


# ── Public entry point ─────────────────────────────────────────────────────


async def resolve_charge(
    agent: dict,
    user_id: str,
    usage: Usage,
    db: Any,
    *,
    cfg: Optional[dict] = None,
    trial_row: Optional[dict] = None,
    subscription_row: Optional[dict] = None,
) -> ChargeResult:
    """Compute the charge for one LLM call. Caller passes already-loaded rows
    to avoid extra queries when we already have them.

    Order of precedence:
        1. Exemption (returns zero, flag set)
        2. Trial (if active for this (user, agent), decrements counters)
        3. Subscription (if active, free for the user; agent admin still gets
           a portion of the subscription revenue tracked elsewhere)
        4. Strategy on the effective config
        5. Default 'free'
    """
    agent_id = agent.get("id") or agent.get("agent_id") or ""

    # 1. Exemption
    reason = await is_exempt(db, agent_id, user_id)
    if reason:
        return ChargeResult(strategy="free", is_exempt=True, exempt_reason=reason)

    # 2. Effective config
    if cfg is None:
        cfg = await load_effective_config(db, agent_id)
    strategy = (cfg.get("strategy") or "free").lower()
    byo = _is_byo_llm(agent)

    # 3. Trial
    if trial_row is None and _has_billing_tables(db):
        trial_row = await _fetch_one(
            db, "trials", {"user_id": user_id, "agent_id": agent_id}
        )
    if _trial_active(trial_row, usage):
        # Decrement counters in-place; caller persists via settle().
        return ChargeResult(strategy=strategy, is_byo_llm=byo, is_trial=True)

    # 4. Active subscription means no incremental charge.
    if subscription_row is None and _has_billing_tables(db):
        subscription_row = await _fetch_one(
            db, "subscriptions", {"user_id": user_id, "agent_id": agent_id}
        )
    if subscription_row and subscription_row.get("status") == "active":
        return ChargeResult(strategy="subscription", is_byo_llm=byo)

    # 5. Strategy dispatch — the end user pays and the agent admin keeps it. Any
    # allocation of that charge is applied downstream by the optional billing
    # extension, never in this calculation.
    if strategy in _STRATEGY_DISPATCH:
        end_user = _STRATEGY_DISPATCH[strategy](usage, agent, cfg)
        return ChargeResult(
            end_user_charge_cents=end_user,
            strategy=strategy,
            is_byo_llm=byo,
        )

    # Default — free
    return ChargeResult(strategy="free", is_byo_llm=byo)


def _trial_active(trial_row: Optional[dict], usage: Usage) -> bool:
    if not trial_row:
        return False
    import datetime as _dt
    expires_at = trial_row.get("expires_at")
    if expires_at:
        try:
            if isinstance(expires_at, str):
                exp = _dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                exp = expires_at
            now = _dt.datetime.now(tz=exp.tzinfo) if exp.tzinfo else _dt.datetime.utcnow()
            if exp < now:
                return False
        except Exception:
            pass
    msgs = trial_row.get("messages_remaining")
    if msgs is not None and msgs <= 0:
        return False
    toks = trial_row.get("tokens_remaining")
    if toks is not None and toks < usage.input_tokens + usage.output_tokens:
        return False
    return True


# ── DB helpers ─────────────────────────────────────────────────────────────


def _has_billing_tables(db: Any) -> bool:
    """Best-effort check so the system stays functional before migration is run."""
    if db is None:
        return False
    if hasattr(db, "_billing_ready"):
        return db._billing_ready
    ready = False
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='billing_configs' LIMIT 1"
                ).fetchone()
                ready = row is not None
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            # Supabase: assume present; if not, the first query will surface it.
            ready = True
    except Exception:
        ready = False
    try:
        setattr(db, "_billing_ready", ready)
    except Exception:
        pass
    return ready


async def _fetch_one(db: Any, table: str, filters: dict) -> Optional[dict]:
    """Cross-backend single-row fetch. SQLite uses _get_conn; Supabase the client."""
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                where = " AND ".join([f"{k}=?" for k in filters.keys()])
                sql = f"SELECT * FROM {table} WHERE {where} LIMIT 1"
                row = conn.execute(sql, tuple(filters.values())).fetchone()
                if row is None:
                    return None
                # sqlite3.Row supports dict-style access
                return {k: row[k] for k in row.keys()}
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            q = db.get_raw_client().table(table).select("*")
            for k, v in filters.items():
                q = q.eq(k, v)
            res = q.limit(1).execute()
            return res.data[0] if res.data else None
    except Exception as e:
        logger.debug("billing._fetch_one(%s) failed: %s", table, e)
    return None
