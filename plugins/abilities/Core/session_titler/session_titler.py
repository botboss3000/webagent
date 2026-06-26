"""Session Namer ability — SELF-CONTAINED drop-in.

Auto-names a chat session from its first few user messages using a tiny LLM
call. Refines progressively over the first 3 user turns, then locks the name.
Pushes a live ``session_title`` event over the per-user WebSocket so the
chat-panel header shows a spinner while naming and swaps in the new name.

As a TURN_HOOK ability, it exports ``TURN_HOOK`` — an async callable that
is dispatched from ``app/api/chat.py`` after every chat turn. This makes the
naming strategy swappable: disable this ability and enable a different one
(e.g. "first-message only", "rule-based naming", "no naming") to change
behaviour without editing core code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# Re-generate the name for this many opening user turns, then lock it.
_TITLE_TURN_LIMIT = 3

# Keep each message excerpt small so the prompt stays cheap.
_MSG_CLIP = 600

# Output budget for the title call. A title is only a handful of tokens, but
# REASONING models spend their budget *thinking* before emitting any visible
# text — a tight cap (the old value was 24) truncates mid-thought and returns
# EMPTY content. Give every model enough room to finish and still print the
# title; _clean_title trims whatever comes back to <=10 words, so a generous cap
# is harmless and barely costs more (the model stops right after the short title
# on a normal completion). Must stay model-agnostic — the admin picks the model.
_MAX_TITLE_TOKENS = 512

# Some models/providers intermittently return null/empty content with NO error
# (observed ~1-in-4 on a cheap flash model: finish_reason "stop", content None).
# Retry on both an empty result AND an exception so one unlucky blank doesn't
# strand the session on its fallback name. Robust for any model.
_TITLE_ATTEMPTS = 3

# Brief gap between retries — lets a momentarily-degraded upstream recover
# instead of being hammered with identical back-to-back calls. The namer runs
# in the background after a turn, so this delay is never user-visible.
_RETRY_BACKOFF_S = 0.4

# The title UPDATE competes for SQLite's single writer slot with the other
# turn-end writers (memory save, run-state finalize). If it loses the race it
# raises "database is locked"; retry a few times with a short, growing backoff so
# a transient contention spike doesn't strand the session on its fallback name.
# Background task → these awaits are never user-visible.
_WRITE_ATTEMPTS = 5
_WRITE_BACKOFF_S = 0.25

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
    """One lightweight LLM call \u2192 a short title. Returns \"\" only when every
    attempt fails or comes back blank.

    Robust to flaky providers and any admin-chosen model: many models
    intermittently return empty content with no error, and reasoning models can
    exhaust a tight token budget before emitting any visible text. We retry on
    BOTH an exception and a blank result, and use a generous output budget so
    the model can actually finish and print the title.
    """
    model = _model()
    if not model:
        logger.info("session_namer: no model configured; skipping")
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
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    from app.agent.loop import _get_client
    client = _get_client()

    for attempt in range(1, _TITLE_ATTEMPTS + 1):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                max_tokens=_MAX_TITLE_TOKENS,
            )
            choice = resp.choices[0]
            title = _clean_title(choice.message.content or "")
            if title:
                return title
            # No exception, but the model handed back empty content \u2014 the known
            # flaky-provider case. Log it (so the silent failure is finally
            # visible) and retry on the next loop.
            logger.warning(
                "session_namer: empty title from %s (attempt %d/%d, finish=%s)",
                model, attempt, _TITLE_ATTEMPTS,
                getattr(choice, "finish_reason", "?"),
            )
        except Exception as e:
            logger.warning(
                "session_namer: LLM call failed (attempt %d/%d): %s",
                attempt, _TITLE_ATTEMPTS, e,
            )
        if attempt < _TITLE_ATTEMPTS:
            await asyncio.sleep(_RETRY_BACKOFF_S)

    logger.warning("session_namer: no title after %d attempts (model=%s)",
                   _TITLE_ATTEMPTS, model)
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

        # Pull ONLY the opening user messages (oldest first). The namer titles a
        # chat from its first few turns and then locks — it never needs the rest
        # of the transcript, so we ask the DB for just these instead of loading
        # (and immediately discarding) the entire session into memory. That full
        # load was one of the heaviest allocations at every turn's end and a
        # candidate to tip a memory-stressed process over.
        first_msgs = await db.fetch_first_user_messages(
            user_id, session_id, _TITLE_TURN_LIMIT
        )
        if not first_msgs:
            return

        # Getting the full limit back means there are at least that many user
        # turns → lock the name (same rule as the old "n >= limit", since the
        # query is capped at the limit). Fewer means keep refining next turn.
        lock = len(first_msgs) >= _TITLE_TURN_LIMIT

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
            await _write_title(db, session_id, title, lock)
            logger.info("session_namer: named %s -> %r (lock=%s)",
                        session_id[:12], title, lock)

        # Spinner off \u2014 send the new title if we got one, else the current one
        # so the header still settles.
        await _push({"type": "session_title", "status": "done",
                     "session_id": session_id,
                     "title": title or _title or ""})
    except Exception as e:
        # WARNING, not DEBUG: this used to swallow the failure completely, so the
        # namer's only observable symptom was a session stuck on its fallback name
        # with metadata=None. Surface it (with traceback) so a real failure is
        # diagnosable instead of invisible.
        logger.warning("session_namer: maybe_title_session failed for %s: %s",
                       str(session_id)[:12], e, exc_info=True)


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


async def _write_title(db: Any, session_id: str, title: str, lock: bool) -> None:
    """Persist the new title and, when locking, mark auto_title_locked in metadata.

    Re-reads the lock flag immediately before writing: the hooks fire
    fire-and-forget per turn, so a slow turn-1 task can finish *after* the
    turn-3 task has already locked the (better) name. A non-locking write must
    not clobber an already-locked name, so we bail out in that case.

    Retries on a transient "database is locked" (see _WRITE_ATTEMPTS): the write
    contends with other turn-end writers for SQLite's single writer slot.
    """
    _title, meta = _read_session(db, session_id)
    if meta.get("auto_title_locked") and not lock:
        return
    if lock:
        meta["auto_title_locked"] = True

    last_err: Optional[Exception] = None
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        conn = db._get_conn()
        try:
            conn.execute(
                "UPDATE sessions SET title=?, metadata=? WHERE id=?",
                (title, json.dumps(meta), session_id),
            )
            conn.commit()
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            # Only a lock contention is worth retrying; anything else is a real
            # error and should surface immediately.
            if "lock" not in str(e).lower():
                raise
        finally:
            conn.close()
        if attempt < _WRITE_ATTEMPTS:
            await asyncio.sleep(_WRITE_BACKOFF_S * attempt)
    if last_err is not None:
        raise last_err


# ── TURN_HOOK: dispatched by app/api/chat.py after every chat turn ──
# Signature: async hook(db, user_id, session_id, emit)
TURN_HOOK = _maybe_title_session