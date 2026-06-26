"""
Rebuild LLM message history from persisted `interactions` rows (DB as source of truth).

Maps stored rows to OpenAI Chat Completions-style message dicts for OpenRouter.
Internal pipeline tools (memory_search, memory_save) are omitted from the model payload.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set

from app.db.interface import StorageBackend
from app.models.schemas import InteractionRecord

logger = logging.getLogger(__name__)

TOOL_MARKER = "\n\n[Tool calls: "  # legacy — kept for backward compat with old rows
INTERNAL_TOOL_NAMES = frozenset({"memory_search", "memory_save"})

# Rows whose `source` is this are terminal-tunnel traffic: persisted to the
# transcript but excluded from the agent's LLM context (defined in
# app/agent/terminal_tunnel.py; duplicated here as a literal to avoid an
# import cycle through that module's lazy app.api imports).
TUNNEL_SOURCE = "terminal_tunnel"

# Reasoning models (Gemini 3.1 Pro, DeepSeek, etc.) stream <think>...</think>
# blocks as part of `content`. Replaying that back to the model on subsequent
# turns causes some providers (notably Gemini via DeepInfra) to return empty
# responses, which the loop then treats as the final answer.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*$", flags=re.DOTALL)


def strip_think_blocks(content: Optional[str]) -> str:
    """Remove `<think>...</think>` reasoning spans (and unclosed trailing ones)."""
    if not content or "<think>" not in content:
        return content or ""
    cleaned = _THINK_BLOCK_RE.sub("", content)
    cleaned = _THINK_UNCLOSED_RE.sub("", cleaned)
    return cleaned.strip()


def _is_parallel_loser(metadata_str: Optional[str]) -> bool:
    """True when an assistant row is a parallel-racing LOSER. Parallel racing was
    removed so no NEW loser rows are written, but historical rows persist in older
    DBs — this guard keeps them out of the replayed model context (and the
    transcript) so they can't resurface the old context-contamination bug."""
    if not metadata_str:
        return False
    try:
        return bool(json.loads(metadata_str).get("parallel_loser"))
    except (json.JSONDecodeError, TypeError):
        return False


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
        # Legacy parallel-racing LOSER rows (metadata.parallel_loser=True) from
        # older DBs are NOT part of the real conversation — replaying them feeds
        # the model conflicting answers to the same turn (and any legacy
        # "[Tool calls: …]" suffix), training it to emit tool calls as plain text.
        # Racing is gone, but the guard stays for historical rows.
        if r.role == "assistant" and _is_parallel_loser(r.metadata):
            continue
        if r.role == "tool" and r.tool_name in INTERNAL_TOOL_NAMES:
            continue
        # Terminal-tunnel traffic (the user driving a program directly through
        # chat) is persisted for the transcript/record but must stay OUT of the
        # agent's context — see app/agent/terminal_tunnel.py. This is the
        # context-exclusion seed of the coming compaction feature.
        if getattr(r, "source", None) == TUNNEL_SOURCE:
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
            content = strip_think_blocks(r.content)

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
                answered: Set[str] = set()
                while i < n and filtered[i].role == "tool":
                    tr = filtered[i]
                    if tr.tool_call_id:
                        out.append({"role": "tool", "content": tr.content, "tool_call_id": tr.tool_call_id})
                        answered.add(tr.tool_call_id)
                    i += 1
                # Self-repair: if the turn was interrupted mid-tool-execution, some
                # tool_calls may have no result row. OpenAI-style APIs reject an
                # assistant message whose tool_calls aren't ALL answered, so emit a
                # synthetic placeholder for any unanswered id.
                for _tc in tool_calls_from_output:
                    _tid = _tc.get("id")
                    if _tid and _tid not in answered:
                        out.append({"role": "tool", "content": "[interrupted — no result]", "tool_call_id": _tid})
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


# How many of the most recent messages a RESUMED turn replays. A resume
# continues an interrupted task — it needs the recent steps, NOT the entire
# transcript. Replaying the whole conversation made the per-attempt working set
# grow with conversation length, and on the very hot resume path (hundreds of
# re-ignitions a day) that drove the runaway memory spikes. The first user
# message (the task) and any leading rolling-summary are kept regardless, so the
# model still has grounding without the full history.
RESUME_TAIL_MESSAGES = 12


def trim_history_for_resume(
    messages: List[Dict[str, Any]],
    *,
    max_tail_messages: int = RESUME_TAIL_MESSAGES,
) -> List[Dict[str, Any]]:
    """Reduce a full OpenAI-style history to a bounded checkpoint for resume.

    Keeps, in order:
      * an optional leading rolling-summary (system) message, verbatim;
      * the first user message in the conversation — the original task, as a
        grounding anchor — if it would otherwise fall outside the tail;
      * the last ``max_tail_messages`` messages, advanced forward to a safe
        boundary so a ``tool`` result is never left without the assistant
        ``tool_calls`` message that produced it (which the model API rejects).

    The tail is a true suffix of the list, so every assistant ``tool_calls``
    message it keeps is still followed by its own tool results — only an
    *orphaned* leading tool result (whose call was trimmed away) is dropped.
    """
    if not messages or max_tail_messages <= 0 or len(messages) <= max_tail_messages:
        return messages

    head: List[Dict[str, Any]] = []
    body = messages
    # Preserve a leading rolling-summary (system) message verbatim — it is itself
    # a bounded, compacted stand-in for the older turns.
    if body and body[0].get("role") == "system":
        head = [body[0]]
        body = body[1:]

    # Original task anchor: the first user message in the body.
    anchor: Optional[Dict[str, Any]] = next(
        (m for m in body if m.get("role") == "user"), None)

    # Bounded tail, advanced past any leading orphan tool results.
    tail = body[-max_tail_messages:]
    start = 0
    while start < len(tail) and tail[start].get("role") == "tool":
        start += 1
    tail = tail[start:]

    out = list(head)
    if anchor is not None and anchor not in tail:
        out.append(anchor)
    out.extend(tail)
    return out


async def build_openai_history_from_session(
    db: StorageBackend,
    user_id: str,
    session_id: str,
    *,
    exclude_interaction_ids: Optional[Set[str]] = None,
    agent_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch session interactions and map them to OpenAI-style messages.

    Compaction-aware:
      * If ``agent_id`` is given and the Context Control ability is on for it, run
        the active compaction step first (folds older turns into a rolling summary
        when the context exceeds the configured threshold). Failure-safe.
      * Then, if a rolling summary covers the leading interactions, assemble
        ``[summary] + [verbatim tail]`` instead of the full transcript. The raw
        rows are never deleted — they stay searchable.
    """
    # Context-management strategy (write side) — only for agent-loop callers that
    # pass an agent_id. Whichever strategy is enabled (default: Context Control)
    # gets to persist any reshaping of the history before it is read back; the
    # default folds older turns into a rolling summary when over threshold. Other
    # callers (suggestions, webhooks) skip this and still get the passive read.
    strategy = None
    settings: Dict[str, Any] = {}
    if agent_id:
        try:
            from app.abilities import context_strategy_for_agent
            strategy = await context_strategy_for_agent(agent_id)
            if strategy is not None:
                _get = getattr(strategy, "CONTEXT_SETTINGS", None)
                _compact = getattr(strategy, "CONTEXT_COMPACT", None)
                settings = (await _get(db, agent_id, session_id, user_id)) if _get else {}
                if settings.get("enabled") and settings.get("compaction_enabled", True) and _compact:
                    await _compact(db, user_id, session_id, settings)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("context strategy (write side) skipped: %s", e)

    rows = await db.fetch_interactions(user_id, session_id)

    # Optional full assembly override: a strategy may reshape the message list
    # itself (e.g. a sliding-window or retrieval-recall strategy) and take full
    # responsibility for the output. The default Context Control strategy does
    # NOT implement this — its compaction persists a rolling-summary marker that
    # the passive read below already understands, so it returns None and falls
    # through. A strategy that overrides assembly should honour any exclusion it
    # cares about itself.
    if strategy is not None and settings.get("enabled"):
        try:
            _assemble = getattr(strategy, "CONTEXT_ASSEMBLE", None)
            if _assemble is not None:
                shaped = await _assemble(db, user_id, session_id, rows, settings)
                if shaped is not None:
                    return shaped
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("context strategy (assemble) skipped: %s", e)

    # Passive read (read side): assemble the compaction train — the ordered list
    # of frozen summary cars followed by the verbatim hot tail. load_segments folds
    # any legacy single-rolling-summary row into one car, so old sessions still work.
    segments: List[Dict[str, Any]] = []
    try:
        from app.agent.compaction import load_segments, render_segment_message
        segments = await load_segments(db, user_id, session_id, rows)
    except Exception as e:  # pragma: no cover - older backends / read error
        logger.debug("load_segments unavailable: %s", e)
    if segments:
        covered = max(int(s.get("end_index") or 0) for s in segments)
        covered = max(0, min(covered, len(rows)))
        total = len(segments)
        car_msgs = [render_segment_message(s, i + 1, total) for i, s in enumerate(segments)]
        tail = rows[covered:]
        msgs = interactions_to_openai_messages(
            tail, exclude_interaction_ids=exclude_interaction_ids)
        return car_msgs + msgs

    return interactions_to_openai_messages(rows, exclude_interaction_ids=exclude_interaction_ids)
