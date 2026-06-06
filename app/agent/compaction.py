"""Context compaction — fold older turns into one rolling summary.

Part 2 of the Context Control ability. When the assembled conversation exceeds
``compact_threshold x token_limit``, the oldest turns are condensed into a single
rolling summary so the agent keeps the gist without re-reading every word. The
raw turns are NEVER deleted — they stay in the ``interactions`` table and remain
searchable; compaction only changes what gets assembled into the prompt.

How it fits together:
  * **Active side (here):** ``maybe_compact`` measures the current context and, if
    over threshold, summarises everything older than a verbatim "hot tail",
    persisting the rolling summary + a ``covered_count`` marker on
    ``session_summaries``.
  * **Passive side (session_history.build_openai_history_from_session):** on every
    turn it reads that marker and assembles ``[summary] + [verbatim tail]``.

The marker is a positional count: how many leading interactions (in ``created_at``
order, the same order ``fetch_interactions`` returns) are folded into the summary.
Cuts only ever land on a **user-turn boundary**, so the verbatim tail always
starts cleanly (never an orphan tool result).

Failure-safe: any error (LLM down, fetch error) makes this a no-op — the turn
proceeds with whatever context it already had.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from app.agent.context_control import estimate_tokens
from app.agent.session_history import (
    interactions_to_openai_messages,
    INTERNAL_TOOL_NAMES,
    TUNNEL_SOURCE,
    strip_think_blocks,
)

logger = logging.getLogger(__name__)

# Cap the summariser's input so a giant aged-out span doesn't blow up one call.
_MAX_TRANSCRIPT_CHARS = 40_000
_MAX_MSG_CHARS = 1_500

SUMMARY_HEADER = "# [EARLIER CONVERSATION SUMMARY]"


def render_summary_message(summary_text: str) -> Dict[str, Any]:
    """The system message injected in place of the folded-away older turns."""
    return {
        "role": "system",
        "content": (
            f"{SUMMARY_HEADER}\n"
            "The earlier part of this conversation was condensed to save context. "
            "Nothing was deleted — the full transcript is still stored and "
            "searchable if you need a detail not captured here.\n\n"
            + (summary_text or "").strip()
        ),
    }


def _summary_client():
    """Lazy LLM client for summarisation — same provider config as chat/embed."""
    try:
        from openai import AsyncOpenAI
    except ImportError:  # pragma: no cover
        from app.openai_compat import AsyncOpenAI
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENROUTER_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or ""
    )
    return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=60.0)


def _summary_model(settings: Dict[str, Any]) -> str:
    return (
        settings.get("summary_model")
        or os.environ.get("COMPACT_MODEL")
        or os.environ.get("LLM_MODEL")
        or os.environ.get("OPENROUTER_MODEL")
        or "deepseek/deepseek-v4-flash"
    )


def _render_transcript(rows: List[Any]) -> str:
    """Render aged-out interaction rows as plain text for the summariser.

    Skips internal pipeline tools and terminal-tunnel traffic (already excluded
    from agent context); truncates each message and the whole transcript.
    """
    parts: List[str] = []
    for r in rows:
        if r.role == "tool" and r.tool_name in INTERNAL_TOOL_NAMES:
            continue
        if getattr(r, "source", None) == TUNNEL_SOURCE:
            continue
        content = strip_think_blocks(r.content or "")
        if not content.strip() and r.role != "tool":
            continue
        if len(content) > _MAX_MSG_CHARS:
            content = content[:_MAX_MSG_CHARS] + " …[truncated]"
        if r.role == "user":
            parts.append(f"USER: {content}")
        elif r.role == "assistant":
            parts.append(f"ASSISTANT: {content}")
        elif r.role == "tool":
            parts.append(f"TOOL[{r.tool_name or '?'}]: {content}")
    text = "\n".join(parts)
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        # Keep the most recent portion of the aged span (older context is the
        # part most safely lossy; the raw rows remain in the DB regardless).
        text = "…[earlier turns truncated]\n" + text[-_MAX_TRANSCRIPT_CHARS:]
    return text


async def _summarise(old_summary: str, transcript: str, settings: Dict[str, Any]) -> Optional[str]:
    """Produce the updated rolling summary, or None on any failure."""
    target = int(settings.get("summary_target_tokens") or 1500)
    sys_prompt = (
        "You maintain a running summary of a long conversation so it can continue "
        "without the full transcript. Merge the EXISTING SUMMARY with the NEW TURNS "
        "into one updated summary. Preserve: decisions made, concrete facts, user "
        "preferences and constraints, unresolved/open threads, and important tool "
        "outcomes. Drop chit-chat and redundancy. Write in compact prose or bullet "
        f"points, roughly {target} tokens or fewer. Output ONLY the summary."
    )
    user_prompt = (
        f"EXISTING SUMMARY:\n{old_summary.strip() or '(none yet)'}\n\n"
        f"NEW TURNS TO FOLD IN:\n{transcript}\n\n"
        "Updated summary:"
    )
    try:
        client = _summary_client()
        resp = await client.chat.completions.create(
            model=_summary_model(settings),
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        logger.warning("compaction: summariser call failed: %s", e)
        return None


async def maybe_compact(
    db: Any, user_id: str, session_id: str, settings: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Compact the session if its context exceeds the threshold. No-op otherwise.

    Returns a small info dict when compaction happened, else None.
    """
    if not settings.get("enabled") or not settings.get("compaction_enabled", True):
        return None
    limit = int(settings.get("token_limit") or 0)
    if limit <= 0:
        return None
    threshold = settings.get("compact_threshold", 0.85)
    tail_fraction = settings.get("tail_fraction", 0.30)

    try:
        rows = await db.fetch_interactions(user_id, session_id)
    except Exception as e:
        logger.warning("compaction: fetch_interactions failed: %s", e)
        return None
    if not rows:
        return None

    try:
        summary_row = await db.get_session_summary(user_id, session_id)
    except Exception as e:
        logger.warning("compaction: get_session_summary failed: %s", e)
        summary_row = None
    old_covered = int((summary_row or {}).get("covered_count") or 0)
    old_summary = (summary_row or {}).get("summary") or ""
    old_covered = max(0, min(old_covered, len(rows)))

    # Current effective context: existing summary (if any) + verbatim tail.
    tail_rows = rows[old_covered:]
    eff_msgs: List[Dict[str, Any]] = []
    if old_summary:
        eff_msgs.append(render_summary_message(old_summary))
    eff_msgs.extend(interactions_to_openai_messages(tail_rows))
    cur_tokens = estimate_tokens(eff_msgs)
    if cur_tokens <= int(threshold * limit):
        return None  # not full enough to compact

    # Choose a new cut at a user-turn boundary, keeping the largest verbatim tail
    # that still fits the tail budget.
    tail_budget = int(tail_fraction * limit)
    candidates = [i for i, r in enumerate(rows) if r.role == "user" and i > old_covered]
    if not candidates:
        return None  # no new user-turn boundary to fold to
    cut: Optional[int] = None
    for idx in reversed(candidates):  # newest -> oldest
        tok = estimate_tokens(interactions_to_openai_messages(rows[idx:]))
        if tok <= tail_budget:
            cut = idx
        else:
            break
    if cut is None:
        # Even the latest turn alone exceeds the tail budget — still make progress
        # by keeping only that latest turn verbatim.
        cut = candidates[-1]
    if cut <= old_covered:
        return None

    newly_aged = rows[old_covered:cut]
    if not newly_aged:
        return None

    transcript = _render_transcript(newly_aged)
    if not transcript.strip():
        return None

    new_summary = await _summarise(old_summary, transcript, settings)
    if not new_summary:
        return None  # failure-safe: leave context as-is

    try:
        await db.upsert_session_summary(
            user_id, session_id,
            summary=new_summary, message_count=cut, covered_count=cut,
        )
    except Exception as e:
        logger.warning("compaction: upsert_session_summary failed: %s", e)
        return None

    info = {
        "covered_count": cut,
        "summarised_rows": len(newly_aged),
        "tokens_before": cur_tokens,
        "limit": limit,
    }
    logger.info(
        "compaction: session %s folded %d rows (covered=%d, ~%d tokens before)",
        session_id, len(newly_aged), cut, cur_tokens,
    )
    return info
