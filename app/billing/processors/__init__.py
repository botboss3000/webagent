"""Payment processor registry.

Add a new processor by:
  1. Creating app/billing/processors/<name>.py exposing a class that
     implements the PaymentProcessor protocol from .base.
  2. Importing the class here and registering it in REGISTRY.

The rest of the codebase calls get_processor(name) — it never instantiates
processors directly.
"""

from __future__ import annotations

import os
import logging
from typing import Dict, Optional

from app.billing.processors.base import PaymentProcessor, ProcessorMetadata

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, PaymentProcessor] = {}


def register(processor: PaymentProcessor) -> None:
    _REGISTRY[processor.name] = processor


def get_processor(name: str) -> Optional[PaymentProcessor]:
    return _REGISTRY.get(name)


def list_processors() -> list:
    return [
        {
            "name": p.name,
            "display_name": p.display_name,
            "configured": p.is_configured(),
            "features": p.supported_features(),
        }
        for p in _REGISTRY.values()
    ]


# ── Register concrete processors ──
# We import lazily so the rest of the system still loads if a processor's
# SDK isn't installed (e.g. only Stripe is wired up).
try:
    from app.billing.processors.stripe import StripeProcessor
    register(StripeProcessor())
except Exception as e:
    logger.info("Stripe processor not registered: %s", e)

try:
    from app.billing.processors.paypal import PayPalProcessor
    register(PayPalProcessor())
except Exception as e:
    logger.info("PayPal processor not registered: %s", e)


__all__ = [
    "PaymentProcessor",
    "ProcessorMetadata",
    "get_processor",
    "list_processors",
    "register",
]
