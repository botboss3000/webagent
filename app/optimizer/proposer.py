"""
Proposer phase — calls LLM to generate improved skill versions.
Uses the same provider/key/model as the main agent.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def propose_improvement(
    skill_name: str,
    skill_content: str,
    issue_desc: str,
    issue_type: str,
) -> Optional[Dict[str, Any]]:
    """
    Ask an LLM to propose an improved version of a skill.

    Returns dict with:
      - new_content: improved version
      - reasoning: why the change
      - gained: (bool) whether improvement was produced
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL", "deepseek-ai/DeepSeek-V4-Flash")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)

    prompt = f"""You are a skill optimizer for an AI personal assistant called webAgent.

A skill called "{skill_name}" needs improvement.

ISSUE: {issue_desc}
TYPE: {issue_type}

CURRENT SKILL CONTENT:
{skill_content}

Propose an improved version. Make it:
- More concise (fewer tokens)
- More reliable (handle errors)
- Clearer instructions

Return ONLY valid JSON in this exact format:
{{"improved": true, "new_content": "the improved skill content here", "reasoning": "why this is better"}}

If the current version is already optimal, return:
{{"improved": false, "reasoning": "why no change needed"}}
"""

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )
        text = resp.choices[0].message.content.strip()

        # Strip markdown code blocks if present
        if text.startswith("```"):
            lines = text.splitlines()
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines).strip()

        result = json.loads(text)

        if result.get("improved") and result.get("new_content"):
            return {
                "improved": True,
                "new_content": result["new_content"],
                "reasoning": result.get("reasoning", "No reasoning provided"),
                "old_content": skill_content,
                "skill_name": skill_name,
            }

        return {
            "improved": False,
            "reasoning": result.get("reasoning", "Skill already optimal"),
            "skill_name": skill_name,
        }

    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Proposer: LLM call or parse failed for %s: %s", skill_name, e)
        return None
