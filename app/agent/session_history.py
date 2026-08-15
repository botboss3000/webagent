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

# Tool output is often much larger than normal chat text. Bound each replayed
# result, while leaving conversation-level history management to Context Control
# so its summary-and-recall path preserves long-running work.
TOOL_RESULT_MAX_CHARS = 12_000

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


def _bounded_tool_content(content: Optional[str]) -> str:
    """Keep persisted tool results useful without replaying arbitrarily large blobs."""
    value = content or ""
    if len(value) <= TOOL_RESULT_MAX_CHARS:
        return value
    omitted = len(value) - TOOL_RESULT_MAX_CHARS
    return f"{value[:TOOL_RESULT_MAX_CHARS]}\n\n[tool output truncated: {omitted:,} characters omitted]"


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


def _ensure_no_orphaned_tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Last-resort safety net: drop any assistant tool_calls that have no
    matching tool results, and drop any orphan tool results whose preceding
    assistant is missing or has no tool_calls. Called at the very end of
    history assembly so the model API never sees an invalid message sequence.
    """
    if not messages:
        return messages
    # Build a set of tool_call_ids that have a result anywhere.
    answered: Set[str] = set()
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            answered.add(m["tool_call_id"])

    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            # Keep only tool_calls that have matching results; if none remain,
            # drop the tool_calls key entirely.
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in answered]
            if kept:
                m = dict(m)
                m["tool_calls"] = kept
            else:
                m = dict(m)
                m.pop("tool_calls", None)
                # Also drop if now empty (no content, no tool_calls)
                if not m.get("content") and not m.get("tool_calls"):
                    i += 1
                    continue
            out.append(m)
            i += 1
        elif m.get("role") == "tool":
            # Walk backward: the nearest assistant must have tool_calls with
            # this tool_call_id, otherwise this tool row is orphan.
            _ok = False
            for _j in range(len(out) - 1, -1, -1):
                _prev = out[_j]
                if _prev.get("role") != "assistant":
                    continue
                if _prev.get("tool_calls") and any(
                    tc.get("id") == m.get("tool_call_id") for tc in _prev["tool_calls"]
                ):
                    _ok = True
                break  # only check the nearest assistant
            if _ok:
                out.append(m)
            else:
                logger.debug(
                    "Skipping orphan tool row tool_call_id=%s — no matching assistant tool_calls",
                    m.get("tool_call_id"))
            i += 1
        else:
            out.append(m)
            i += 1
    return out


def _extract_tool_calls_from_output(output_str: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """Try to extract tool_calls from the interaction's output JSON field.

    New rows (post-clean-content fix) store tool calls in the `output` field
    as ``{"role": "assistant", "content": "...", "tool_calls": [...]}``.
    Legacy rows have no output field and rely on the ``[Tool calls: ...]``
    marker in the content string.

    Returns ``None`` when the output is a remote skeleton
    (``_remote_placeholder: true``) — the caller should skip the tool_calls
    so the history rebuild doesn't feed the model orphaned tool result rows.
    """
    if not output_str:
        return None
    try:
        parsed = json.loads(output_str)
        # Remote placeholder skeleton — tool names + IDs only, no real data.
        # Return None so the caller skips pairing and the orphaned tool result
        # rows that follow are also skipped (see interactions_to_openai_messages).
        if parsed.get("_remote_placeholder"):
            return None
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

            # Check for remote placeholder skeleton — output has tool names + IDs
            # only, with no real data. The full output lives on the originating
            # device's local store.
            _is_remote_placeholder = False
            if r.output:
                try:
                    _parsed = json.loads(r.output)
                    _is_remote_placeholder = bool(_parsed.get("_remote_placeholder"))
                except (json.JSONDecodeError, TypeError):
                    pass

            if tool_calls_from_output:
                # Clean content (no marker), tool calls from output field.
                # These are already in OpenAI format: {id, type, function: {name, arguments}}.
                #
                # Guard: providers reject assistant messages with both content=None
                # and tool_calls=[].  Skip any row where both are empty — there is
                # nothing to replay and the model API will 400.
                _clean = content.strip() or None

                # Peek ahead at following tool rows to find which tool_call_ids
                # have a result row (rows with status='deleted' are already
                # excluded by fetch_interactions). Only include tool calls that
                # are answered, so intentionally deleted tool calls don't produce
                # spurious "[interrupted — no result]" placeholders.
                _answered_ids: Set[str] = set()
                _j = i + 1
                while _j < n and filtered[_j].role == "tool":
                    if filtered[_j].tool_call_id:
                        _answered_ids.add(filtered[_j].tool_call_id)
                    _j += 1
                _filtered_calls = [tc for tc in tool_calls_from_output
                                   if tc.get("id") in _answered_ids or not tc.get("id")]

                if not _filtered_calls:
                    # All tool calls were deleted — emit nothing or just text
                    if _clean:
                        out.append({"role": "assistant", "content": _clean})
                    i += 1
                    while i < n and filtered[i].role == "tool":
                        i += 1
                    continue

                if (not _clean and not _filtered_calls) and not _is_remote_placeholder:
                    i += 1
                    continue
                out.append({
                    "role": "assistant",
                    "content": _clean,
                    "tool_calls": _filtered_calls,
                })
                i += 1
                # Consume following tool rows, pairing by tool_call_id
                answered: Set[str] = set()
                while i < n and filtered[i].role == "tool":
                    tr = filtered[i]
                    if tr.tool_call_id:
                        out.append({"role": "tool", "content": _bounded_tool_content(tr.content), "tool_call_id": tr.tool_call_id})
                        answered.add(tr.tool_call_id)
                    i += 1
                # Self-repair: if the turn was interrupted mid-tool-execution, some
                # tool_calls may have no result row. OpenAI-style APIs reject an
                # assistant message whose tool_calls aren't ALL answered, so emit a
                # synthetic placeholder for any unanswered id.
                for _tc in _filtered_calls:
                    _tid = _tc.get("id")
                    if _tid and _tid not in answered:
                        out.append({"role": "tool", "content": "[interrupted — no result]", "tool_call_id": _tid})
                continue

            # Remote placeholder skeleton — output has tool names + IDs only,
            # no real data. Emit the assistant text without tool_calls, then
            # skip the orphaned tool result rows that follow (they have no
            # matching assistant tool_call to pair with, and the model API
            # rejects orphan tool rows).
            if _is_remote_placeholder:
                # Guard: skip if both content and tool_calls are empty — the
                # model API rejects {"role":"assistant","content":null}.
                _clean = content.strip() or None
                if not _clean:
                    i += 1
                    while i < n and filtered[i].role == "tool":
                        i += 1
                    continue
                out.append({"role": "assistant", "content": _clean})
                i += 1
                while i < n and filtered[i].role == "tool":
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
                            "content": _bounded_tool_content(tr.content),
                            "tool_call_id": tr.tool_call_id,
                        })
                # Skip extra tool rows beyond pair_n — they have no matching
                # tool_call in spec and would be orphaned (model API rejects them).
                # Also add synthetic placeholders for any unanswered calls.
                for _tc in spec:
                    _tid = _tc.get("id") if isinstance(_tc, dict) else None
                    _answered = any(
                        tr.tool_call_id == _tid for tr in tool_rows[:pair_n]
                    ) if _tid else True
                    if _tid and not _answered:
                        out.append({"role": "tool", "content": "[interrupted — no result]", "tool_call_id": _tid})
            else:
                # Guard: skip empty assistant messages (no tool calls, no content).
                # The model API rejects {"role":"assistant","content":""}.
                if content.strip():
                    out.append({"role": "assistant", "content": content})
                i += 1
        elif r.role == "tool":
            # Skip orphan tool rows — a tool message must follow an assistant
            # message with tool_calls, otherwise the model API rejects it.
            # The live loop can never produce this shape, but DB-replay can
            # when an assistant row has no tool_calls in its output field and
            # no legacy marker.
            if r.tool_call_id:
                # Walk backward to find the last assistant message (skip
                # intermediate tool rows that are siblings of the same call).
                _prev_asst = None
                for _pm in reversed(out):
                    if _pm.get("role") == "assistant":
                        _prev_asst = _pm
                        break
                if _prev_asst and _prev_asst.get("tool_calls"):
                    out.append({"role": "tool", "content": _bounded_tool_content(r.content), "tool_call_id": r.tool_call_id})
                else:
                    logger.debug("Skipping orphan tool row id=%s tool_call_id=%s — no preceding assistant tool_calls",
                                 r.id, r.tool_call_id)
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
RESUME_TAIL_MESSAGES = 32

# Below this many stored rows, a session CANNOT have been compacted: compaction
# never deletes raw turns (they stay searchable), so any session that has summary
# segments still carries all of its many raw rows. A handful of rows is therefore
# provably free of segments AND far under any token compaction threshold — so the
# whole context-strategy + compaction + segment-loading path (several sequential
# remote round-trips that would each return "nothing") can be skipped, and the
# rows mapped directly. This is the common hot path: a new or short session — the
# "simple llm" turn the user feels as slow. (A custom CONTEXT_ASSEMBLE strategy,
# if one is ever enabled, won't run on these short sessions, but there is nothing
# meaningful to reshape in a handful of turns; the default Context Control has no
# assemble step. Skipping a compaction that could fire on a huge two-turn paste
# just defers it one turn — the next turn compacts — and the passive read is
# still correct, so this is self-correcting, not a regression.)
HISTORY_FULL_PATH_FLOOR = 8


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

    # If any assistant message in the tail has tool_calls, the cut may have
    # landed between the call and some of its results. Walk backward from the
    # cut to include all matching tool results so every tool_call_id is
    # answered (the model API rejects orphaned tool_call_ids).
    _cut_idx = len(body) - max_tail_messages + start  # first index of tail in body
    for _pos, _msg in enumerate(tail):
        if _msg.get("role") != "assistant" or not _msg.get("tool_calls"):
            continue
        _tcs = [tc.get("id") for tc in _msg["tool_calls"] if tc.get("id")]
        if not _tcs:
            continue
        # Which tool_call_ids already have results AFTER this assistant in tail?
        _answered: Set[str] = set()
        for _fm in tail[_pos + 1:]:
            if _fm.get("role") == "tool" and _fm.get("tool_call_id"):
                _answered.add(_fm["tool_call_id"])
        for _tid in _tcs:
            if _tid not in _answered:
                # A tool result for this call_id was trimmed — scan backward
                for _j in range(_cut_idx - 1, -1, -1):
                    if body[_j].get("role") == "tool" and body[_j].get("tool_call_id") == _tid:
                        tail.insert(_pos + 1, body[_j])
                        # Shift _pos right so the next call_ids scan the same
                        # assistant's now-augmented tool-results correctly.
                        _pos += 1
                        break

    out = list(head)
    if anchor is not None and anchor not in tail:
        out.append(anchor)
    out.extend(tail)
    return _ensure_no_orphaned_tool_calls(out)


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
    # Count first. A compacted session's raw prefix remains searchable, but it
    # must not re-enter Python merely to discover the hot-tail boundary.
    row_count = await db.count_interactions(user_id, session_id)
    if row_count < HISTORY_FULL_PATH_FLOOR:
        rows = await db.fetch_interactions(user_id, session_id)
        return interactions_to_openai_messages(
            rows, exclude_interaction_ids=exclude_interaction_ids)

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
                # Custom assemblers explicitly own the full transcript shape.
                # The default Context Control strategy has no assembler and
                # therefore stays on the bounded suffix path below.
                rows = await db.fetch_interactions(user_id, session_id)
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
        segments = await load_segments(
            db, user_id, session_id, total_count=row_count)
    except Exception as e:  # pragma: no cover - older backends / read error
        logger.debug("load_segments unavailable: %s", e)
    if segments:
        covered = max(int(s.get("end_index") or 0) for s in segments)
        covered = max(0, min(covered, row_count))
        total = len(segments)
        car_msgs = [render_segment_message(s, i + 1, total) for i, s in enumerate(segments)]
        tail = await db.fetch_interactions_from_offset(
            user_id, session_id, covered)
        msgs = interactions_to_openai_messages(
            tail, exclude_interaction_ids=exclude_interaction_ids)
        return _ensure_no_orphaned_tool_calls(car_msgs + msgs)

    rows = await db.fetch_interactions(user_id, session_id)
    return _ensure_no_orphaned_tool_calls(interactions_to_openai_messages(rows, exclude_interaction_ids=exclude_interaction_ids))


# ── Engine compact-and-restart recap (shared by the Claude + Codex engines) ──
# Alternate engines keep their memory OUTSIDE WebAgent (the local CLI's own
# session/thread, which only ever grows). When the user runs `/compact`, the
# engine's compact_restart hook folds this chat into a recap built from the DB
# transcript and arms a one-shot reseed; the engine then starts a FRESH CLI
# session seeded with the recap. The builders below are engine-agnostic, so they
# live here (shared code under app/) — the engines just call them.

RESEED_MSG_CAP = 6000        # max chars kept per recap message
RESEED_TOOL_CAP = 1500       # tool results are often huge dumps — trim harder
RESEED_TOTAL_CAP = 120_000   # overall recap ceiling (chars)


def flatten_msg_content(content: Any) -> str:
    """Best-effort plain text from an OpenAI-style message ``content`` — a plain
    string, or the multimodal list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text") or part.get("content")
                if isinstance(t, str) and t.strip():
                    out.append(t)
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return ""


def strip_native_recall_hint(text: str) -> str:
    """Drop the Context-Control recall footer from a rendered summary 'car'. Those
    hints point at WebAgent native tools (recall_compacted / search_this_session)
    that a local CLI (Claude / Codex) does NOT have, so they'd only confuse it."""
    idx = text.find("Nothing here was deleted")
    return text[:idx].rstrip() if idx != -1 else text


def build_reseed_context(messages: List[Dict[str, Any]]) -> str:
    """Render the compaction-assembled history (summary cars + verbatim tail) into
    one readable recap block to seed a fresh engine session. Size-bounded so a
    long tail can't blow up the new prompt."""
    lines: List[str] = []
    total = 0
    for m in messages:
        role = (m.get("role") or "").strip()
        body = flatten_msg_content(m.get("content")).strip()
        if not body:
            continue
        if role == "system":
            chunk = strip_native_recall_hint(body)
        elif role == "user":
            chunk = f"User: {body[:RESEED_MSG_CAP]}"
        elif role == "assistant":
            chunk = f"Assistant: {body[:RESEED_MSG_CAP]}"
        elif role == "tool":
            snippet = body[:RESEED_TOOL_CAP]
            if len(body) > RESEED_TOOL_CAP:
                snippet += " …[trimmed]"
            chunk = f"[earlier tool result] {snippet}"
        else:
            chunk = body[:RESEED_MSG_CAP]
        if not chunk.strip():
            continue
        if total + len(chunk) > RESEED_TOTAL_CAP:
            lines.append("…[earlier turns trimmed to fit]")
            break
        lines.append(chunk)
        total += len(chunk)
    return "\n\n".join(lines).strip()
