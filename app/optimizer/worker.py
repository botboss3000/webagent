"""
Worker — estimates impact of proposed changes via LLM.
Parallel execution via asyncio.gather.
"""

from __future__ import annotations

import asyncio, json, logging, os
from typing import Any, Dict, List
from app.optimizer.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


async def run_trials(user_id, changes, transcript, trials_per_change=2):
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)

    async def _run_one(change, trial_num):
        element = change.get("element", "unknown")
        new_content = change.get("new_content", "")
        old_excerpt = change.get("old_excerpt", "")

        prompt = f"""Estimate impact of this change:

ELEMENT: {element}

OLD:
{old_excerpt[:500]}

NEW:
{new_content[:800]}

RECENT:
{chr(10).join(transcript[-15:])}

OUTPUT FORMAT:
First write a MESSAGE explaining your analysis - what this change does, how it affects performance, risks.
Then output JSON:
{{
  "message": "Your conversational analysis explaining your reasoning and confidence",
  "estimated_turns": N,
  "estimated_tokens": N,
  "estimated_time_ms": N,
  "success_likely": true/false,
  "confidence": 0.0-1.0,
  "confidence_reasoning": "If confidence is LOW (<0.5), explain EXACTLY why this change won't work. What is the root cause? What would need to be different? This goes back to the Planner to help them adjust their approach.",
  "reasoning": "brief"
}}"""
        # Temporary: parse JSON from the response even if message prefix exists

        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, max_tokens=800,
            )
            text = resp.choices[0].message.content.strip()
            if text.startswith("```"):
                parts = text.split("```")
                text = parts[1].replace("json", "", 1).strip() if len(parts) > 1 else text
            # Extract JSON from response — LLM may prepend a message before the JSON block
            if not text.startswith('{'):
                brace = text.find('{')
                if brace >= 0:
                    text = text[brace:]
            return json.loads(text)
        except Exception as e:
            logger.warning("Worker trial %d for %s failed: %s", trial_num, element, e)
            return None

    tasks = []
    for ci, change in enumerate(changes[:4]):
        for tn in range(trials_per_change):
            tasks.append(((ci, tn), _run_one(change, tn)))

    results_raw = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)

    change_data = {}
    for idx, ((ci, tn), _) in enumerate(tasks):
        raw = results_raw[idx]
        if isinstance(raw, Exception): raw = None
        if ci not in change_data:
            change_data[ci] = {"estimates": [], "change": changes[ci]}
        if raw is not None:
            change_data[ci]["estimates"].append(raw)

    results = []
    for ci in sorted(change_data.keys()):
        cd = change_data[ci]
        est = cd["estimates"]
        ch = cd["change"]
        if est:
            avg = {
                "turns": sum(e.get("estimated_turns", 0) for e in est) / len(est),
                "tokens": sum(e.get("estimated_tokens", 0) for e in est) / len(est),
                "time_ms": sum(e.get("estimated_time_ms", 0) for e in est) / len(est),
                "success": all(e.get("success_likely", True) for e in est),
                "confidence": sum(e.get("confidence", 0.5) for e in est) / len(est),
            }
        else:
            avg = {"turns": 0, "tokens": 0, "time_ms": 0, "success": True, "confidence": 0}
        results.append({"element": ch.get("element", "unknown"), "change": ch, "trials": est, "averaged": avg})

    return results