"""
Seed optimizer prompt templates into context_templates table.
Called once on first optimizer run per DB instance.
No hardcoded fallbacks — seeds empty content by default.
The actual system prompts come from data/agents/*.json
which populate the agent_templates table, NOT context_templates.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# No hardcoded prompts — they come from data/agents/*.json files
ALL_TEMPLATES = []


def seed_optimizer_templates() -> int:
    """Seeds optimizer prompt templates into context_templates.
    Currently no-op since prompts come from data/agents/*.json
    which populate agent_templates (used by chat.py for optimizer routing).
    """
    return 0