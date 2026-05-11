"""
Finalizer — judges trial results, compares against baseline, picks winners.
"""

from __future__ import annotations

import json, logging, os
from typing import Any, Dict, List
from app.optimizer.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


async def review_trials(user_id, trials, baseline, transcript=None, criteria="", target=0, skill_state=None):
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)

    tool_calls, chat_lines = [], []
    for line in (transcript or [])[-30:]:
        if "(tool:" in line: tool_calls.append(line[:400])
        else: chat_lines.append(line[:200])

    trial_data = []
    for tr in trials:
        avg = tr.get("averaged", {})
        ch = tr.get("change", {})
        trial_data.append({
            "element": tr.get("element", "?"),
            "change_type": ch.get("change_type", ""),
            "reasoning": ch.get("reasoning", "")[:200],
            "old_excerpt": (ch.get("old_excerpt", "") or "")[:400],
            "new_content": (ch.get("new_content", "") or "")[:600],
            "est_turns": avg.get("turns", 0),
            "est_tokens": avg.get("tokens", 0),
            "est_time_ms": avg.get("time_ms", 0),
            "confidence": avg.get("confidence", 0.5),
        })

    prompt = f"""Compare trials against baseline.

BASELINE: {json.dumps(baseline)}
TARGET: {criteria} <= {target} (if applicable)
TOOL CALLS: {json.dumps(tool_calls[-10:])}
CHAT: {json.dumps(chat_lines[-5:])}
TRIALS: {json.dumps(trial_data, indent=2)}

Return JSON with winners, losers, summary."""

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=1200,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].replace("json", "", 1).strip() if len(parts) > 1 else text
        return json.loads(text)
    except Exception as e:
        logger.warning("Finalizer failed: %s", e)
        return {"winners": [], "losers": [], "summary": str(e)}