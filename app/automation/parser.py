"""Parse the human-readable ``automation`` slot into structured task rows.

The user writes free-form English describing when to run prompts and how to
deliver results. An LLM call converts that into a list of structured task
dicts that the scheduler can execute. Cron expressions are validated with
``croniter`` before a row is accepted.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


VALID_CHANNELS = {
    "webchat", "telegram", "whatsapp", "sms",
    "email", "slack", "discord",
}


@dataclass
class ParsedTask:
    task_label: str
    prompt: str
    schedule_cron: str
    schedule_natural: str
    timezone: str
    channel: Optional[str]
    channel_recipient: Optional[str]
    silent: bool


@dataclass
class ParseResult:
    tasks: List[ParsedTask]
    error: Optional[str] = None


_LLM_INSTRUCTION = """You convert a free-form Automation file into a JSON list of scheduled tasks.

Each task object MUST have these keys (use null when not specified):
- task_label: short human label (1-6 words)
- prompt: the exact prompt that will be sent to the agent when this task fires
- schedule_cron: a 5-field cron expression (minute hour day-of-month month day-of-week). Use "*" for any.
- schedule_natural: the original human phrase describing when this runs
- timezone: IANA timezone string (e.g. "America/Los_Angeles"); use "UTC" if unspecified
- channel: one of webchat|telegram|whatsapp|sms|email|slack|discord, or null if silent / unspecified
- channel_recipient: external id (phone, chat id, email, etc.) or null
- silent: true if the task explicitly should NOT deliver a message anywhere

Rules:
- Output ONLY a JSON array. No prose. No markdown fences.
- If the input is empty, comments-only, or has no scheduled tasks, output [].
- If the user says "every minute", schedule_cron = "* * * * *".
- If a delivery channel is mentioned but no recipient, leave channel_recipient null.
- Do NOT invent tasks the user didn't describe.
"""


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _extract_json_array(text: str) -> Optional[List[Any]]:
    s = _strip_code_fences(text)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        # Try to find the first JSON array substring.
        m = re.search(r"\[\s*(?:\{.*?\}\s*,?\s*)*\]", s, flags=re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, list):
        return None
    return data


def _validate_cron(expr: str) -> bool:
    expr = (expr or "").strip()
    if not expr:
        return False
    try:
        from croniter import croniter
        return croniter.is_valid(expr)
    except ImportError:
        # Without croniter, accept a basic 5-field expression.
        parts = expr.split()
        return len(parts) == 5


def _coerce_task(raw: Dict[str, Any]) -> Optional[ParsedTask]:
    if not isinstance(raw, dict):
        return None
    prompt = (raw.get("prompt") or "").strip()
    cron = (raw.get("schedule_cron") or "").strip()
    if not prompt or not _validate_cron(cron):
        return None
    channel = raw.get("channel")
    if channel is not None:
        channel = str(channel).strip().lower() or None
        if channel == "none":
            channel = None
        if channel and channel not in VALID_CHANNELS:
            channel = None
    label = (raw.get("task_label") or "").strip() or prompt[:48]
    return ParsedTask(
        task_label=label,
        prompt=prompt,
        schedule_cron=cron,
        schedule_natural=(raw.get("schedule_natural") or "").strip(),
        timezone=(raw.get("timezone") or "UTC").strip() or "UTC",
        channel=channel,
        channel_recipient=(raw.get("channel_recipient") or None) or None,
        silent=bool(raw.get("silent")) or (channel is None and not raw.get("channel_recipient")),
    )


async def _call_llm(slot_content: str, agent_context: Dict[str, Any]) -> str:
    """Call the configured LLM with the parsing instruction. Returns raw text."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""

    if not api_key or not model:
        raise RuntimeError("LLM credentials not configured (LLM_API_KEY / LLM_MODEL).")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=45.0)

    agent_hint = ""
    if agent_context:
        ag_name = agent_context.get("name") or ""
        ag_desc = agent_context.get("description") or ""
        if ag_name or ag_desc:
            agent_hint = f"\nAgent context (for reference only — do not put it in tasks):\nname: {ag_name}\ndescription: {ag_desc}\n"

    messages = [
        {"role": "system", "content": _LLM_INSTRUCTION + agent_hint},
        {"role": "user", "content": f"Automation file content:\n\n{slot_content}\n\nReturn the JSON array."},
    ]

    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
        max_tokens=2048,
    )
    return resp.choices[0].message.content or ""


async def parse_automation_file(
    slot_content: str,
    agent_context: Optional[Dict[str, Any]] = None,
) -> ParseResult:
    """Parse a free-form automation slot into a list of ``ParsedTask`` objects."""
    text = (slot_content or "").strip()
    if not text:
        return ParseResult(tasks=[])

    # Detect placeholder content (default seeded file) → no real tasks.
    if text.lower().startswith("# automation tasks") and "every weekday at 9am" in text.lower():
        return ParseResult(tasks=[])

    try:
        raw_output = await _call_llm(text, agent_context or {})
    except Exception as e:
        logger.warning("Automation parse LLM call failed: %s", e)
        return ParseResult(tasks=[], error=f"LLM call failed: {e}")

    data = _extract_json_array(raw_output)
    if data is None:
        logger.warning("Automation parse: could not extract JSON array from LLM output")
        return ParseResult(tasks=[], error="Parser could not extract a JSON array from the LLM response.")

    tasks: List[ParsedTask] = []
    for item in data:
        t = _coerce_task(item)
        if t is not None:
            tasks.append(t)

    return ParseResult(tasks=tasks)
