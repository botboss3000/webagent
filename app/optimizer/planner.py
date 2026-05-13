"""
Planner — analyzes interactions and proposes changes for Workers to test.
Uses hardcoded prompt (DB loading is unreliable with aiosqlite).
"""

from __future__ import annotations

import json, logging, os
from typing import Any, Dict, List, Optional
from app.optimizer.prompt_loader import load_prompt

logger = logging.getLogger(__name__)




async def propose_improvements(user_id, session_id, prefilter_data, mode="analyze",
                               reviewer_data=None, optimizer_history=None, feedback=""):
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    # Load provider config using same mechanism as main agent loop
    from app.admin.settings import load_provider_for_user
    await load_provider_for_user(user_id)

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-ai/DeepSeek-V4-Flash")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=45.0)
    system_prompt = await load_prompt("planner-prompt")
    user_prompt = _build_analyze_prompt(prefilter_data, optimizer_history, feedback)

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        text = resp.choices[0].message.content.strip()
        logger.info("Planner RAW response (first 500 chars): %s", text[:500])
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].replace("json", "", 1).strip() if len(parts) > 1 else text
        if not text.startswith('{'):
            brace = text.find('{')
            if brace >= 0:
                text = text[brace:]
        # Trim trailing content after the last closing brace
        last_brace = text.rfind('}')
        if last_brace >= 0:
            text = text[:last_brace+1]
        if not text.strip():
            logger.warning("Planner: empty text after JSON extraction")
            return {"analysis": "Planner: empty LLM response", "changes": []}
        try:
            return json.loads(text)
        except json.JSONDecodeError as je:
            logger.warning("Planner: JSON decode failed: %s on text=%s", je, text[:300])
            # Attempt to fix: escape newlines in string values
            try:
                import re
                fixed = re.sub(r'(?<!\\)\n', '\\n', text)
                return json.loads(fixed)
            except json.JSONDecodeError:
                return {"analysis": "Planner: JSON parse error", "changes": []}
    except Exception as e:
        logger.warning("Planner: LLM failed: %s", e)
        return {"analysis": f"Planner error: {e}", "changes": []}


def _build_analyze_prompt(pf, optimizer_history=None, feedback=""):
    transcript = pf.get("transcript", [])
    turns = pf.get("turns", 1)
    tokens = pf.get("tokens", 100)
    skill_state = pf.get("skill_state", [])

    lines = ["Review this session and propose at least 1 change."]
    lines.append(f"\nStats: {turns} turns, ~{tokens} tokens")

    # Include tool errors section - extract all lines containing errors
    tool_errors = [t for t in transcript if "Error:" in t or "failed" in t.lower() or "traceback" in t.lower()]
    if tool_errors:
        lines.append("\n## Tool Errors Found in Session")
        for err in tool_errors:
            lines.append(f"  - {err[:200]}")

    # Include user's explicit feedback from /optimize command, if given
    if feedback:
        lines.append(f"\n## User Feedback\n{feedback}")
        lines.append("This is what the user wants changed. Your proposal MUST address this.")

    # Include rejection feedback from previous iteration if available
    if optimizer_history:
        lines.append("\n## Previous attempt was rejected")
        lines.append(f"Feedback from Workers: {optimizer_history}")
        lines.append("Your new proposal MUST address these rejection reasons.")

    lines.append("\nFull Transcript:")
    for t in transcript[:40]:
        lines.append(t)

    return "\n".join(lines)
