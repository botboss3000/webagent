"""Session Titler ability — SELF-CONTAINED drop-in.

Auto-names a chat session from its first few user messages using a tiny LLM
call. Refines progressively over the first 3 user turns, then locks the name.
Pushes a live ``session_title`` event over the per-user WebSocket so the
chat-panel header shows a spinner while titling and swaps in the new name.

As a TURN_HOOK ability, it exports ``TURN_HOOK`` — an async callable that
is dispatched from ``app/api/chat.py`` after every chat turn. This makes the
naming strategy swappable: disable this ability and enable a different one
(e.g. "first-message only", "rule-based naming", "no naming") to change
behaviour without editing core code.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "session_titler",
    "display_name": "Session Titler",
    "category": "ability",
    "status": "stable",
    "summary": "Auto-names chat sessions from the conversation's opening messages.",
    "tools": [],                       # no agent-facing tools — this is a turn hook
    "group": "core",
    "icon": "tag",
    "color": "#7dcfff",
    "description": "After each chat turn, generates a short LLM-summarized name for the session, refining over the first 3 turns then locking.",
    "simple": True,
}

# Re-generate the name for this many opening user turns, then lock it.
_TITLE_TURN_LIMIT = 3

# Keep each message excerpt small so the prompt stays cheap.
_MSG_CLIP = 600

_SYSTEM_PROMPT = (
    "You write a very short title for a chat session, capturing what the user "
    "wants. Rules: 1 to 10 words, strongly prefer 2 to 5 words. Use Title Case. "
    "No surrounding quotes and no trailing punctuation. Ignore greetings, "
    "pleasantries and filler \u2014 focus on the actual task or topic. If the intent "
    "is still vague, summarize the topic as best you can. Reply with the title "
    "only, nothing else."
)

EmitFn = Callable[[dict], Awaitable[None]]


def _model() -> str:
    return os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""


def _clean_title(raw: str) -> str:
    """Normalize the model's reply into a tidy 1\u201310 word title."""
    t = (raw or "").strip()
    if not t:
        return ""
    # First non-empty line only.
    t = next((ln.strip() for ln in t.splitlines() if ln.strip()), "")
    # Strip wrapping quotes the model sometimes adds, then trailing punctuation.
    t = t.strip('"').strip("'").strip().rstrip(".,!?;:").strip()
    # Hard clamp to 10 words.
    words = t.split()
    if len(words) > 10:
        t = " ".join(words[:10])
    return t[:80]


async def _llm_title(user_messages: List[str]) -> str:
    """One lightweight LLM call \u2192 a short title. Returns \"\" on any failure."""
    model = _model()
    if not model:
        logger.info("session_titler: no model configured; skipping")
        return ""

    numbered = "\n".join(
        f"{i + 1}. {(m or '').strip()[:_MSG_CLIP]}"
        for i, m in enumerate(user_messages)
    )
    user_msg = (
        "Here are the first user message(s) of a chat session:\n\n"
        f"{numbered}\n\n"
        "Give a short title for this session."
    )

    try:
        from app.agent.loop import _get_client
        client = _get_client()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=24,
        )
        return _clean_title(resp.choices[0].message.content or "")
    except Exception as e:
        logger.warning("session_titler: LLM call failed: %s", e)
        return ""


async def _maybe_title_session(
    db: Any,
    user_id: str,
    session_id: str,
    emit: Optional[EmitFn] = None,
) -> None:
    """Generate/refine the session name from its first \u22643 user messages.

    Safe to call after every turn: it no-ops once the name is locked (after the
    3rd user turn, or after a manual rename). Never raises \u2014 best-effort.
    """
    try:
        # Skip special sessions that are named by their own flows.
        if session_id.startswith(("optimizer-", "closer-", "slash-")):
            return

        # Don't auto-title if the user (or a prior lock) already settled the name.
        _title, meta = _read_session(db, session_id)
        if meta.get("auto_title_locked"):
            return

        # Pull the user messages so far (oldest first).
        from app.agent.session_history import build_openai_history_from_session
        history = await build_openai_history_from_session(db, user_id, session_id)
        user_msgs = [
            m["content"]
            for m in history
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].strip()
        ]
        if not user_msgs:
            return

        n = len(user_msgs)
        first_msgs = user_msgs[:_TITLE_TURN_LIMIT]
        # Lock once we've seen the 3rd user turn (or a longer pre-existing chat).
        lock = n >= _TITLE_TURN_LIMIT

        async def _push(ev: dict) -> None:
            if emit is None:
                return
            try:
                await emit(ev)
            except Exception:
                pass

        # Spinner on.
        await _push({"type": "session_title", "status": "generating",
                     "session_id": session_id})

        title = await _llm_title(first_msgs)

        if title:
            _write_title(db, session_id, title, lock)
            logger.info("session_titler: named %s -> %r (lock=%s)",
                        session_id[:12], title, lock)

        # Spinner off \u2014 send the new title if we got one, else the current one
        # so the header still settles.
        await _push({"type": "session_title", "status": "done",
                     "session_id": session_id,
                     "title": title or _title or ""})
    except Exception as e:
        logger.debug("session_titler: maybe_title_session failed: %s", e)


def _read_session(db: Any, session_id: str):
    """Return (title, metadata_dict) for the session, or (None, {}) if missing."""
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT title, metadata FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, {}
    title = row[0]
    try:
        meta = json.loads(row[1]) if row[1] else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return title, (meta if isinstance(meta, dict) else {})


def _write_title(db: Any, session_id: str, title: str, lock: bool) -> None:
    """Persist the new title and, when locking, mark auto_title_locked in metadata."""
    _title, meta = _read_session(db, session_id)
    if lock:
        meta["auto_title_locked"] = True
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE sessions SET title=?, metadata=? WHERE id=?",
            (title, json.dumps(meta), session_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── TURN_HOOK: dispatched by app/api/chat.py after every chat turn ──
# Signature: async hook(db, user_id, session_id, emit)
TURN_HOOK = _maybe_title_session