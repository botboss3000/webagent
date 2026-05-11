"""
Proposer — single LLM agent. Three modes:
  Mode 1: Analyze interaction + propose concrete changes to modifiable elements.
  Mode 2: Present Reviewer results to user and ask for approval.
Uses context_template "planner-prompt" as system prompt.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def _load_proposer_prompt(user_id: str) -> str:
    """Load the Proposer system prompt — tries context_documents first, then context_templates, then default."""
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if raw:
        conn = raw()
        try:
            agent_row = conn.execute(
                "SELECT id FROM agents WHERE user_id = ? LIMIT 1", (user_id,)
            ).fetchone()
            if agent_row:
                # Try per-user context_documents first
                row = conn.execute(
                    """SELECT content FROM context_documents
                       WHERE context_type = 'optimizer' AND title = 'planner-prompt'
                       AND agent_id = ? LIMIT 1""",
                    (agent_row[0],),
                ).fetchone()
                if row:
                    return row[0]
            # Fall back to global context_templates
            row = conn.execute(
                """SELECT content FROM context_templates
                   WHERE context_type = 'optimizer' AND title = 'planner-prompt'
                   LIMIT 1""",
            ).fetchone()
            if row:
                return row[0]
        finally:
            conn.close()
    # Third: try .md file on disk
    import os
    md_path = os.path.join(os.path.dirname(__file__), "prompts", "planner.md")
    try:
        if os.path.exists(md_path):
            return open(md_path, encoding="utf-8").read()
    except Exception:
        pass
    # Last resort
    return _default_proposer_prompt()


def _default_proposer_prompt() -> str:
    return """You are a skill optimization agent for webAgent, an AI personal assistant.
Your job: analyze interactions, identify what went wrong, and propose concrete
improvements to modifiable elements (skills, tools, prompts, code).

Rules:
- One change per element per round. Don't bundle unrelated changes.
- Be specific: show exact before/after for each change.
- Estimate impact: time, tokens, turns for each change.
- Flag high-risk changes (could break functionality).
- Prefer small, targeted changes over rewrites.

Output format (JSON):
{
  "analysis": "1-2 sentence summary of what went wrong",
  "changes": [
    {
      "element": "name of element (e.g. skills.md, send_email code)",
      "element_type": "context_document|tool_code|system_prompt",
      "change_type": "rewrite|trim|add_error_handling|add_instruction",
      "old_excerpt": "...",
      "new_content": "...",
      "expected_impact": {"time_pct": -20, "tokens_pct": -30, "turns": -1},
      "risk": "low|medium|high",
      "reasoning": "why this helps"
    }
  ],
  "no_changes_needed": false
}
"""


async def propose_improvements(
    user_id: str,
    session_id: str,
    prefilter_data: Dict[str, Any],
    mode: str = "analyze",  # "analyze" or "present"
    reviewer_data: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Mode "analyze": propose changes based on interaction analysis.
    Mode "present": present reviewer results for user approval.
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=45.0)
    system_prompt = await _load_proposer_prompt(user_id)

    if mode == "analyze":
        user_prompt = _build_analyze_prompt(prefilter_data, optimizer_history)
    elif mode == "present":
        user_prompt = _build_present_prompt(reviewer_data)
    else:
        return None

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content.strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("Proposer: LLM call failed: %s", e)
        return None


def _build_analyze_prompt(pf: Dict[str, Any], optimizer_history: Optional[List[str]] = None) -> str:
    """Build user message — only transcript and session errors."""
    transcript = pf.get("transcript", [])
    opportunities = pf.get("opportunities", [])

    lines = ["Analyze this interaction and propose improvements to reduce failures, turns, tokens, or response time."]

    if optimizer_history:
        lines.append("")
        lines.append("## Previous Optimization Attempts")
        for h in optimizer_history[-10:]:
            lines.append(h[:300])

    if opportunities:
        lines.append("")
        lines.append("## Issues in This Session")
        for o in opportunities:
            lines.append(f"- {o.get('skill_name', '?')}: {o.get('issue_desc', '')}")

    lines.append("")
    lines.append("## Transcript")
    for t in transcript[-25:]:
        lines.append(t)

    return "\n".join(lines)


def _build_present_prompt(rd: Optional[Dict[str, Any]]) -> str:
    """Build user message for presentation mode."""
    if not rd:
        return "No reviewer data available. Tell the user no changes were found."

    winners = rd.get("winners", [])
    losers = rd.get("losers", [])
    baseline = rd.get("baseline", {})

    lines = [
        "Present these optimization results to the user in clear, friendly language.",
        "",
        f"## Baseline: {baseline.get('turns', '?')} turns, {baseline.get('tokens', '?')} tokens",
        "",
    ]

    if winners:
        lines.append("## Winners (improved)")
        for w in winners:
            delta = w.get("delta", {})
            lines.append(
                f"- {w.get('element', '?')}: "
                f"tokens {delta.get('tokens_pct', 0)}%, "
                f"time {delta.get('time_pct', 0)}%, "
                f"turns {delta.get('turns', 0)}"
            )
        lines.append("")
        lines.append("Ask the user: 'Apply these winning changes?'")
        lines.append("Include [Apply All] [Apply Winners Only] [Skip] options.")

    if losers:
        lines.append("")
        lines.append("## Rejected (no improvement)")
        for l in losers:
            lines.append(f"- {l.get('element', '?')}: no measurable gain")

    if not winners and not losers:
        lines.append("No changes showed improvement. Tell the user and ask if they want to try different approaches.")

    return "\n".join(lines)
