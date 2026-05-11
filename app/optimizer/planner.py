"""
Planner — analyzes interactions and proposes changes for Workers to test.
Uses hardcoded prompt (DB loading is unreliable with aiosqlite).
"""

from __future__ import annotations

import json, logging, os
from typing import Any, Dict, List, Optional
from app.optimizer.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """You are the Planner. You MUST output JSON with at least 1 change. Never ask questions. Never return empty changes.

PROPOSE A CONCRETE CHANGE NOW. Even for a simple greeting, propose shortening it.

JSON format (REQUIRED):
{
  "analysis": "1-sentence observation",
  "changes": [
    {
      "element": "system_prompt",
      "element_type": "system_prompt",
      "change_type": "trim",
      "old_excerpt": "the greeting text from the transcript",
      "new_content": "Hi Human.",
      "expected_impact": {"turns_pct": 0, "tokens_pct": -80},
      "risk": "low",
      "reasoning": "shorter greeting"
    }
  ]
}
DO NOT ask questions. DO NOT return empty changes. RETURN JSON NOW."""



PLANNER_PROMPT = """Fallback — loaded from prompt_loader."""


async def propose_improvements(user_id, session_id, prefilter_data, mode="analyze",
                               reviewer_data=None, optimizer_history=None):
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=45.0)
    system_prompt = await load_prompt("planner-prompt")
    user_prompt = _build_analyze_prompt(prefilter_data, optimizer_history)

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content.strip())
    except Exception as e:
        logger.warning("Planner: LLM failed: %s", e)
        return {"analysis": f"Planner error: {e}", "changes": []}


def _build_analyze_prompt(pf, optimizer_history=None):
    transcript = pf.get("transcript", [])
    turns = pf.get("turns", 1)
    tokens = pf.get("tokens", 100)
    skill_state = pf.get("skill_state", [])

    lines = ["Review this session and propose at least 1 change."]
    lines.append(f"\nStats: {turns} turns, ~{tokens} tokens")

    lines.append("\nTranscript:")
    for t in transcript[-25:]:
        lines.append(t)

    return "\n".join(lines)
