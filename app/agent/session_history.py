"""
Rebuild LLM message history from persisted `interactions` rows (DB as source of truth).

Maps stored rows to OpenAI Chat Completions-style message dicts for OpenRouter.
Internal pipeline tools (memory_search, memory_save) are omitted from the model payload.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

from app.db.interface import StorageBackend
from app.models.schemas import InteractionRecord

logger = logging.getLogger(__name__)

TOOL_MARKER = "\n\n[Tool calls: "  # legacy — kept for backward compat with old rows
INTERNAL_TOOL_NAMES = frozenset({"memory_search", "memory_save"})


def _extract_tool_calls_from_output(output_str: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """Try to extract tool_calls from the interaction's output JSON field.

    New rows (post-clean-content fix) store tool calls in the `output` field
    as ``{"role": "assistant", "content": "...", "tool_calls": [...]}``.
    Legacy rows have no output field and rely on the ``[Tool calls: ...]``
    marker in the content string.
    """
    if not output_str:
        return None
    try:
        parsed = json.loads(output_str)
        tc = parsed.get("tool_calls")
        if tc and isinstance(tc, list):
            return tc
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def interactions_to_openai_messages(
    interactions: List[InteractionRecord],
    *,
    exclude_interaction_ids: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Convert interaction rows (ordered oldest-first) into OpenAI-style chat messages.

    - Drops rows whose id is in exclude_interaction_ids (e.g. current user turn
      appended again by stream_agent_events).
    - Omits internal tools from the model transcript.
    - Rebuilds assistant tool_calls from the output field (new) or the
      persisted [Tool calls: ...] suffix (legacy fallback).
    """
    exclude = exclude_interaction_ids or set()
    filtered: List[InteractionRecord] = []
    for r in interactions:
        if r.id in exclude:
            continue
        if r.role == "tool" and r.tool_name in INTERNAL_TOOL_NAMES:
            continue
        # Skip optimizer system messages (init, prefilter) — they are metadata,
        # not conversation turns. The optimizer session should only show
        # Planner/Closer responses, not server-generated boilerplate.
        if r.role == "assistant" and (r.content or "").startswith("📊 **Optimization Analysis**"):
            continue
        if r.role == "assistant" and (r.content or "").startswith("Stats:"):
            continue
        filtered.append(r)

    out: List[Dict[str, Any]] = []
    i = 0
    n = len(filtered)

    while i < n:
        r = filtered[i]
        if r.role == "user":
            out.append({"role": "user", "content": r.content})
            i += 1
        elif r.role == "assistant":
            content = r.content or ""

            # NEW: try clean path first — read tool calls from the output field
            tool_calls_from_output = _extract_tool_calls_from_output(r.output)
            if tool_calls_from_output:
                # Clean content (no marker), tool calls from output field.
                # These are already in OpenAI format: {id, type, function: {name, arguments}}.
                out.append({
                    "role": "assistant",
                    "content": content.strip() or None,
                    "tool_calls": tool_calls_from_output,
                })
                i += 1
                # Consume following tool rows, pairing by tool_call_id
                while i < n and filtered[i].role == "tool":
                    tr = filtered[i]
                    if tr.tool_call_id:
                        out.append({"role": "tool", "content": tr.content, "tool_call_id": tr.tool_call_id})
                    i += 1
                continue

            # LEGACY: parse tool calls from the [Tool calls: ...] marker in content
            if TOOL_MARKER in content:
                base, _, rest = content.partition(TOOL_MARKER)
                json_part = rest.strip()
                try:
                    spec = json.loads(json_part)
                    if not isinstance(spec, list):
                        raise ValueError("tool calls payload is not a list")
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning("Could not parse tool calls from assistant row %s: %s", r.id, e)
                    out.append({"role": "assistant", "content": content})
                    i += 1
                    continue

                i += 1
                tool_rows: List[InteractionRecord] = []
                while i < n and filtered[i].role == "tool":
                    tool_rows.append(filtered[i])
                    i += 1

                pair_n = min(len(spec), len(tool_rows))
                tool_calls: List[Dict[str, Any]] = []
                for j in range(pair_n):
                    item = spec[j] if isinstance(spec[j], dict) else {}
                    name = item.get("name") or ""
                    args = item.get("args", "{}")
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    tid = tool_rows[j].tool_call_id or f"legacy_{j}"
                    tool_calls.append({
                        "id": tid,
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    })

                out.append({
                    "role": "assistant",
                    "content": base.strip() if base.strip() else None,
                    "tool_calls": tool_calls,
                })
                for j in range(pair_n):
                    tr = tool_rows[j]
                    if tr.tool_call_id:
                        out.append({
                            "role": "tool",
                            "content": tr.content,
                            "tool_call_id": tr.tool_call_id,
                        })
                for j in range(pair_n, len(tool_rows)):
                    tr = tool_rows[j]
                    if tr.tool_call_id:
                        out.append({
                            "role": "tool",
                            "content": tr.content,
                            "tool_call_id": tr.tool_call_id,
                        })
            else:
                out.append({"role": "assistant", "content": content})
                i += 1
        elif r.role == "tool":
            if r.tool_call_id:
                out.append({"role": "tool", "content": r.content, "tool_call_id": r.tool_call_id})
            i += 1
        else:
            logger.debug("Skipping unknown interaction role %s id=%s", r.role, r.id)
            i += 1

    return out


async def build_openai_history_from_session(
    db: StorageBackend,
    user_id: str,
    session_id: str,
    *,
    exclude_interaction_ids: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch session interactions and map them to OpenAI-style messages."""
    rows = await db.fetch_interactions(user_id, session_id)
    return interactions_to_openai_messages(rows, exclude_interaction_ids=exclude_interaction_ids)
