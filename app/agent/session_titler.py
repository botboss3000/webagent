"""Auto-name a chat session from its first few user messages.

Why this exists
---------------
A session's name used to be a verbatim clip of the user's *first* message. That
is often useless: the first message is a greeting ("hi"), or the real scope of
the conversation only becomes clear a couple of turns in. This module replaces
that with a tiny LLM call that *summarizes* the opening of the conversation into
a short name (1–10 words, biased short).

Behaviour
---------
- It runs as a fire-and-forget background task after each chat turn (dispatched
  from app/api/chat.py), so it never blocks the reply.
- It **refines progressively over the first 3 user messages** — re-generating the
  name on user turn 1, 2 and 3 as the scope settles — then **locks** the name so
  later turns leave it alone. A manual rename also locks it (see db_viewer).
- It is best-effort: any failure is swallowed and the existing name is kept.

It mirrors the lightweight auxiliary-LLM pattern already used by
``app/agent/suggestions.py`` and ``app/agent/compaction.py`` — one non-streaming
``chat.completions`` call via the shared client, no tools, no streaming, no
persistence beyond the one ``sessions.title`` write.

While it thinks, it pushes a ``session_title`` event (status ``generating`` then
``done``) over the per-user WebSocket so the chat-panel header can show the load
spinner and then swap in the new name live.
"""

import json
import logging
import os
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# Re-generate the name for this many opening user turns, then lock it.
_TITLE_TURN_LIMIT = 3

# Keep each message excerpt small so the prompt stays cheap.
_MSG_CLIP = 600

_SYSTEM_PROMPT = (
    "You write a very short title for a chat session, capturing what the user "
    "wants. Rules: 1 to 10 words, strongly prefer 2 to 5 words. Use Title Case. "
    "No surrounding quotes and no trailing punctuation. Ignore greetings, "
    "pleasantries and filler — focus on the actual task or topic. If the intent "
    "is still vague, summarize the topic as best you can. Reply with the title "
    "only, nothing else."
)

EmitFn = Callable[[dict], Awaitable[None]]


def _model() -> str:
    return os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""


def _clean_title(raw: str) -> str:
    """Normalize the model's reply into a tidy 1–10 word title."""
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
    """One lightweight LLM call → a short title. Returns "" on any failure."""
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


async def maybe_title_session(
    db: Any,
    user_id: str,
    session_id: str,
    emit: Optional[EmitFn] = None,
) -> None:
    """Generate/refine the session name from its first ≤3 user messages.

    Safe to call after every turn: it no-ops once the name is locked (after the
    3rd user turn, or after a manual rename). Never raises — best-effort.
    """
    try:
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

        # Spinner off — send the new title if we got one, else the current one
        # so the header still settles.
        await _push({"type": "session_title", "status": "done",
                     "session_id": session_id,
                     "title": title or _title or ""})
    except Exception as e:
        logger.debug("session_titler: maybe_title_session failed: %s", e)
