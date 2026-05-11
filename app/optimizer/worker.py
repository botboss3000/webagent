"""
Executor — runs trials to measure the impact of proposed changes.
Uses context_template "worker-prompt" as system prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_EXECUTOR_PROMPT = """You are an execution testing agent. Your job: simulate the impact
of a proposed change by analyzing the change and estimating the resulting metrics.

Given:
- The proposed change (old vs new content)
- The element type (context_document, tool_code, system_prompt)
- The original interaction transcript

Estimate how the change would affect:
- Number of turns (would it eliminate unnecessary tool calls or confirmations?)
- Token usage (is the new version shorter? Does it avoid verbose tool outputs?)
- Response time (would it reduce round trips?)
- Whether the task would still succeed (is the change backward-compatible?)

Return JSON:
{
  "estimated_turns": N,
  "estimated_tokens": N,
  "estimated_time_ms": N,
  "success_likely": true/false,
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}
"""


async def _load_executor_prompt(user_id: str) -> str:
    """Load the executor system prompt: agent-specific first, template, then file fallback."""
    from app.db import get_db

    db = get_db()
    raw = getattr(db, "_get_conn", None)
    if raw:
        conn = raw()
        try:
            # Try agent-specific context document
            conn.execute(
                "SELECT id FROM agents WHERE user_id = ? LIMIT 1",
                (user_id,),
            )
            agent_row = conn.fetchone()

            if agent_row:
                conn.execute(
                    """SELECT content FROM context_documents
                       WHERE context_type = 'optimizer' AND title = 'worker-prompt'
                       AND agent_id = ? LIMIT 1""",
                    (agent_row[0],),
                )
                row = conn.fetchone()
                if row:
                    return row[0]

                # Try template fallback
                conn.execute(
                    """SELECT content FROM context_templates
                   WHERE context_type = 'optimizer' AND title = 'worker-prompt'
                   LIMIT 1""",
                )
                row = conn.fetchone()
                if row:
                    return row[0]
        finally:
            conn.close()

    # Fallback to file
    import os

    md_path = os.path.join(
        os.path.dirname(__file__), "prompts", "worker.md"
    )
    try:
        if os.path.exists(md_path):
            with open(md_path, encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass

    return DEFAULT_EXECUTOR_PROMPT


async def run_trials(
    user_id: str,
    changes: List[Dict[str, Any]],
    transcript: List[str],
    trials_per_change: int = 2,
) -> List[Dict[str, Any]]:
    """Run LLM-based trials for each proposed change in parallel."""

    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)

    system_prompt = await _load_executor_prompt(user_id)

    async def _run_single_trial(change: Dict[str, Any], trial_num: int):
        """Run one trial for one change."""
        element = change.get("element", "unknown")
        new_content = change.get("new_content", "")
        old_excerpt = change.get("old_excerpt", "")
        element_type = change.get("element_type", "context_document")

        prompt = (
            f"Estimate the impact of this change:\n\n"
            f"ELEMENT: {element} (type: {element_type})\n\n"
            f"OLD:\n{old_excerpt[:500]}\n\n"
            f"NEW:\n{new_content[:800]}\n\n"
            f"RECENT INTERACTION:\n{chr(10).join(transcript[-15:])}\n\n"
            f"Return JSON with estimated_turns, estimated_tokens, estimated_time_ms, success_likely, confidence, reasoning."
        )

        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            import json

            return json.loads(resp.choices[0].message.content.strip())
        except Exception as e:
            logger.warning(
                "Executor: trial %d for %s failed: %s", trial_num, element, e
            )
            return None

    # Build all trial tasks (max 4 changes, n trials each)
    change_tasks = []
    for ci, change in enumerate(changes[:4]):
        for tn in range(trials_per_change):
            task = _run_single_trial(change, tn)
            change_tasks.append(((ci, tn), task))

    # Run all in parallel
    results_raw = await asyncio.gather(
        *[t for _, t in change_tasks], return_exceptions=True
    )

    # Process results
    change_data: Dict[int, Dict[str, Any]] = {}
    for idx, ((ci, tn), _) in enumerate(change_tasks):
        raw = results_raw[idx]
        if isinstance(raw, Exception):
            raw = None

        if ci not in change_data:
            change_data[ci] = {"estimates": [], "change": changes[ci]}

        if raw is not None:
            change_data[ci]["estimates"].append(raw)

    # Average results per change
    results = []
    for ci in sorted(change_data.keys()):
        cd = change_data[ci]
        estimates = cd["estimates"]
        change = cd["change"]

        if estimates:
            avg = {
                "turns": sum(e.get("estimated_turns", 0) for e in estimates) / len(estimates),
                "tokens": sum(e.get("estimated_tokens", 0) for e in estimates) / len(estimates),
                "time_ms": sum(e.get("estimated_time_ms", 0) for e in estimates) / len(estimates),
                "success": all(e.get("success_likely", True) for e in estimates),
                "confidence": sum(e.get("confidence", 0.5) for e in estimates) / len(estimates),
            }
        else:
            avg = {
                "turns": 0,
                "tokens": 0,
                "time_ms": 0,
                "success": True,
                "confidence": 0,
            }

        results.append({
            "element": change.get("element", "unknown"),
            "change": change,
            "trials": estimates,
            "averaged": avg,
        })

    return results
