"""Access enforcement — does this (user, agent) pair have permission to chat?

The chat endpoint calls check_access() after the existing role checks. On
denial we raise HTTP 402 (Payment Required) with a machine-readable reason
the frontend uses to surface the right paywall: buy credits / subscribe /
start trial / re-up trial.

Agents with no billing config (or strategy='free') always allow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from plugins.billing.pricing import (
    Strategy,
    load_effective_config,
    is_exempt,
    _fetch_one,
    _has_billing_tables,
)
from plugins.billing import wallet as wallet_mod

logger = logging.getLogger(__name__)


class AccessReason(str, Enum):
    ALLOW = "allow"
    NEEDS_CREDITS = "needs_credits"
    NEEDS_SUBSCRIPTION = "needs_subscription"
    TRIAL_EXPIRED = "trial_expired"


@dataclass
class AccessDecision:
    allow: bool
    reason: AccessReason
    detail: str = ""
    strategy: str = "free"
    balance_cents: int = 0
    accepted_processors: list = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "reason": self.reason.value,
            "detail": self.detail,
            "strategy": self.strategy,
            "balance_cents": self.balance_cents,
            "accepted_processors": self.accepted_processors or [],
        }


async def check_access(agent: dict, user_id: str, db: Any) -> AccessDecision:
    """Return ALLOW unless billing is configured AND the user can't pay.

    Order:
      1. Tables missing → ALLOW (system not migrated yet).
      2. Exemption → ALLOW.
      3. Effective strategy == 'free' → ALLOW.
      4. Trial active (any quota remaining) → ALLOW.
      5. Active subscription → ALLOW.
      6. Credits with positive balance → ALLOW.
      7. Otherwise → deny with the specific reason.
    """
    agent_id = agent.get("id") or agent.get("agent_id") or ""

    if not _has_billing_tables(db):
        return AccessDecision(True, AccessReason.ALLOW, "billing-not-configured")

    if await is_exempt(db, agent_id, user_id):
        return AccessDecision(True, AccessReason.ALLOW, "exempt")

    cfg = await load_effective_config(db, agent_id)
    strategy = (cfg.get("strategy") or "free").lower()
    processors = cfg.get("allowed_processors") or []

    if strategy == Strategy.FREE.value:
        return AccessDecision(True, AccessReason.ALLOW, "free", strategy=strategy)

    # Trial — active or grant new if config allows
    trial = await _fetch_one(db, "trials", {"user_id": user_id, "agent_id": agent_id})
    if trial:
        if _has_trial_quota(trial):
            return AccessDecision(True, AccessReason.ALLOW, "trial-active", strategy="trial")
        if strategy == Strategy.TRIAL.value:
            return AccessDecision(False, AccessReason.TRIAL_EXPIRED, "Your trial has ended.",
                                   strategy=strategy, accepted_processors=processors)

    # Subscription
    sub = await _fetch_one(db, "subscriptions", {"user_id": user_id, "agent_id": agent_id})
    if sub and sub.get("status") == "active":
        return AccessDecision(True, AccessReason.ALLOW, "subscription-active", strategy="subscription")
    if strategy == Strategy.SUBSCRIPTION.value:
        return AccessDecision(False, AccessReason.NEEDS_SUBSCRIPTION,
                               "This agent requires an active subscription.",
                               strategy=strategy, accepted_processors=processors)

    # Credits / per_message / per_token all draw from the user's credit wallet.
    if strategy in (Strategy.CREDITS.value, Strategy.PER_MESSAGE.value, Strategy.PER_TOKEN.value):
        w = await wallet_mod.get_balance(db, user_id)
        balance = w.available_cents if w else 0
        if balance > 0:
            return AccessDecision(True, AccessReason.ALLOW, "credits-positive",
                                   strategy=strategy, balance_cents=balance,
                                   accepted_processors=processors)
        return AccessDecision(False, AccessReason.NEEDS_CREDITS,
                               "Buy credits to chat with this agent.",
                               strategy=strategy, balance_cents=balance,
                               accepted_processors=processors)

    return AccessDecision(True, AccessReason.ALLOW, "fallthrough", strategy=strategy)


def _has_trial_quota(trial: dict) -> bool:
    import datetime as _dt
    expires_at = trial.get("expires_at")
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
    msgs = trial.get("messages_remaining")
    if msgs is not None and msgs <= 0:
        return False
    toks = trial.get("tokens_remaining")
    if toks is not None and toks <= 0:
        return False
    return True
