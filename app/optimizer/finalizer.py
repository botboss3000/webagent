"""
Reviewer — compares trial results against baseline and picks winners.
Uses context_template "finalizer-prompt" as system prompt.
Now receives full transcript, tool calls, and change content.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_REVIEWER_PROMPT = """You are an optimization reviewer. Your job: compare trial results
against baseline metrics and identify which changes produce meaningful improvements.

You receive:
- Baseline metrics (turns, tokens, time_ms)
- Tool calls from the original session (with errors/success)
- Trial results (averaged estimates per change including old/new content)

Rank changes by:
1. How much they reduce turns/tokens/time versus baseline
2. Whether they address the root cause visible in tool calls
3. Confidence of the improvement
4. Whether success is likely maintained

For context_document and system_prompt changes: small turn increases (+1-2) are
acceptable if token/time savings are significant (>30%).

Return JSON:
{
  "winners": [
    {
      "element": "...",
      "summary": "brief",
      "delta": {"turns_pct": -N, "tokens_pct": -N, "time_pct": -N},
      "confidence": 0.0-1.0,
      "success_kept": true,
      "rank": 1
    }
  ],
  "losers": ["element names that made things worse"],
  "summary": "overall analysis"
}
"""


async def _load_reviewer_prompt(user_id: str) -> str:
    from app.db import get_db
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    if raw:
        conn = raw()
        try:
            conn.execute("SELECT id FROM agents WHERE user_id = ? LIMIT 1", (user_id,))
            agent_row = conn.fetchone()
            if agent_row:
                conn.execute(
                    """SELECT content FROM context_documents
                       WHERE context_type = 'optimizer' AND title = 'finalizer-prompt'
                       AND agent_id = ? LIMIT 1""",
                    (agent_row[0],),
                )
                row = conn.fetchone()
                if row:
                    return row[0]
                conn.execute(
                    """SELECT content FROM context_templates
                       WHERE context_type = 'optimizer' AND title = 'finalizer-prompt'
                       LIMIT 1""",
                )
                row = conn.fetchone()
                if row:
                    return row[0]
        finally:
            conn.close()

    md_path = os.path.join(os.path.dirname(__file__), "prompts", "finalizer.md")
    try:
        if os.path.exists(md_path):
            with open(md_path, encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return DEFAULT_REVIEWER_PROMPT


async def review_trials(
    user_id: str,
    trials: List[Dict[str, Any]],
    baseline: Dict[str, Any],
    transcript: List[str] = None,
    criteria: str = "",
    target: float = 0,
    skill_state: List[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Review trial results against baseline and select winners.
    Now includes structured tool call data and change content."""

    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)
    system_prompt = await _load_reviewer_prompt(user_id)

    # Extract structured tool calls from transcript
    tool_calls = []
    chat_lines = []
    for line in (transcript or [])[-30:]:
        if "(tool:" in line:
            tool_calls.append(line[:400])
        else:
            chat_lines.append(line[:200])

    # Build rich trial data
    trial_data = []
    for tr in trials:
        avg = tr.get("averaged", {})
        change = tr.get("change", {})
        trial_data.append({
            "element": tr.get("element", "unknown"),
            "change_type": change.get("change_type", ""),
            "reasoning": change.get("reasoning", "")[:200],
            "old_excerpt": (change.get("old_excerpt", "") or "")[:400],
            "new_content": (change.get("new_content", "") or "")[:600],
            "estimated_metrics": {
                "turns": avg.get("turns", 0),
                "tokens": avg.get("tokens", 0),
                "time_ms": avg.get("time_ms", 0),
            },
            "confidence": avg.get("confidence", 0.5),
            "success_likely": avg.get("success", True),
        })

    comparison = {
        "baseline": baseline,
        "skill_state": skill_state or [],
        "tool_calls": tool_calls,
        "chat_messages": chat_lines[-10:],
        "trials": trial_data,
    }

    prompt = (
        f"Optimization target: {criteria} ≤ {target}\n" if criteria and target else ""
        + "Compare these trial results against the baseline.\n"
        + "Tool calls show what happened in the original session.\n"
        + "ROOT CAUSE: look at tool errors and identify the real problem.\n\n"
        + json.dumps(comparison, indent=2)
        + "\n\nRank changes. Return JSON with winners, losers, and summary."
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content.strip())
    except Exception as e:
        logger.warning("Reviewer: LLM call failed: %s", e)
        return {"winners": [], "losers": [], "summary": f"Review failed: {e}"}
