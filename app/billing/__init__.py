"""Three-tier marketplace billing for webAgent.

Architecture:
    end user --pays--> agent admin --owes platform fee--> app admin

Public surface:
    pricing.resolve_charge(agent, user_id, usage, db) -> ChargeResult
    wallet.preauthorize(user_id, cents, db) / settle(...) / refund(...)
    enforcement.check_access(agent, user_id, db) -> AccessDecision
    processors.get_processor(name) -> PaymentProcessor
"""

from app.billing.pricing import (
    Usage,
    ChargeResult,
    Strategy,
    resolve_charge,
    load_effective_config,
)
from app.billing.enforcement import (
    AccessDecision,
    check_access,
)

__all__ = [
    "Usage",
    "ChargeResult",
    "Strategy",
    "resolve_charge",
    "load_effective_config",
    "AccessDecision",
    "check_access",
]
