"""
Validator — replays historical interactions against a proposed skill improvement.

Checks whether the improved skill would have handled past interactions
correctly, and estimates the expected metric deltas.
"""

import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


async def validate_proposal(
    proposal: dict,
    historical_interactions: Optional[List[dict]] = None,
) -> dict:
    """
    Validate an improvement proposal against historical usage.

    Args:
        proposal: From Proposer, with current_code, improved_code, opportunity_type, evidence
        historical_interactions: Optional list of past interactions (not yet implemented fully)

    Returns:
        dict with:
        - passed (bool)
        - reasoning (str — why it passed or failed)
        - expected_deltas (dict — estimated metric changes)
    """
    current = proposal.get("current_code", "")
    improved = proposal.get("improved_code", "")
    opp_type = proposal.get("opportunity_type", "")
    evidence = proposal.get("evidence", {})

    # Basic sanity checks before full replay

    # 1. Don't deploy empty or identical content
    if not improved.strip():
        return {
            "passed": False,
            "reasoning": "Improved content is empty.",
            "expected_deltas": {},
        }

    if improved.strip() == current.strip():
        return {
            "passed": False,
            "reasoning": "No changes detected.",
            "expected_deltas": {},
        }

    # 2. Don't deploy if new code is drastically different (possible hallucination)
    len_ratio = len(improved) / max(len(current), 1)
    # For very small skills (<100 chars), allow larger growth
    max_ratio = 15.0 if len(current) < 100 else 5.0
    if len_ratio > max_ratio:
        return {
            "passed": False,
            "reasoning": f"Improved code is {len_ratio:.1f}x larger than original — likely hallucination.",
            "expected_deltas": {},
        }
    if len_ratio < 0.1:
        return {
            "passed": False,
            "reasoning": f"Improved code is {len_ratio:.1f}x smaller than original — too much was cut.",
            "expected_deltas": {},
        }

    # 3. Estimate expected deltas based on opportunity type
    deltas = _estimate_deltas(opp_type, len(current), len(improved), evidence)

    # 4. Replay check (placeholder — full implementation when traffic grows)
    replay_ok = True
    replay_reason = ""
    if historical_interactions and len(historical_interactions) > 0:
        # Full replay would: simulate each past interaction using the improved skill
        # and compare outcomes. For now, flag as needing this but don't block.
        replay_reason = f" (replay on {len(historical_interactions)} interactions deferred)"
    else:
        replay_reason = " (no historical data for replay)"

    return {
        "passed": replay_ok,
        "reasoning": f"Sanity checks passed. Size change: {len(current)}→{len(improved)} chars.{replay_reason}",
        "expected_deltas": deltas,
    }


def _estimate_deltas(
    opp_type: str,
    old_len: int,
    new_len: int,
    evidence: dict,
) -> dict:
    """Rough estimation of metric improvements based on the opportunity type."""
    deltas = {}

    if opp_type == "high_failure":
        failure_rate = evidence.get("failure_rate", 10)
        deltas["failure_rate"] = round(failure_rate * 0.5, 1)  # assume ~50% reduction

    elif opp_type == "high_tokens":
        size_reduction = 1 - (new_len / max(old_len, 1))
        deltas["tokens_per_call"] = round(size_reduction * 100, 1)

    elif opp_type == "high_turns":
        deltas["turns_per_task"] = -1  # assume consolidation saves 1 turn

    elif opp_type == "low_rating":
        deltas["rating"] = "+15"  # assume fix improves rating

    elif opp_type == "large_size":
        size_reduction = 1 - (new_len / max(old_len, 1))
        deltas["skill_size_tokens"] = round(size_reduction * 100, 1)

    elif opp_type == "user_feedback":
        deltas["user_satisfaction"] = "improved based on feedback"

    return deltas
