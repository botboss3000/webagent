"""Suggested-Replies engine.

A SILENT, single-shot LLM helper that role-plays as the user and predicts the
next message(s) they are likely to want to send. Used to populate the tappable
suggestion chips above the chat pill.

Design constraints (the whole point of this module):
  - It NEVER runs the agent loop, never persists to the chat, never streams over
    the agent WebSocket, never calls tools. One prompt in, a small JSON array of
    candidate user messages out. That is how the "process work" stays hidden.
  - The persona/prompts come from the ``user-impersonator`` system agent template
    (``data/agents/user-impersonator.json``).
  - The runtime tunables (mode / count / idle seconds) live in a small per-machine
    runtime file (``data/config/suggestions.json``, gitignored) so the Agents-page
    config panel can edit them without touching the template-seeding machinery.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Repo root is two levels up from app/agent/suggestions.py (app/agent -> app -> root)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))

_IMPERSONATOR_JSON = os.path.join(_REPO_ROOT, "data", "agents", "user-impersonator.json")
_RUNTIME_CONFIG = os.path.join(_REPO_ROOT, "data", "config", "suggestions.json")

# Mode values the config panel switches between.
VALID_MODES = ("off", "on", "scheduler")  # "scheduler" == on + idle refresh

_DEFAULTS = {
    "mode": "off",
    "count": 3,
    "idle_seconds": 25,
}


def _load_impersonator_def() -> Dict[str, Any]:
    """Read the impersonator system-agent template from its JSON file.

    Falls back to a minimal built-in persona if the file is missing so the
    feature degrades gracefully rather than erroring.
    """
    try:
        with open(_IMPERSONATOR_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("suggestions: could not read impersonator template: %s", e)
        return {
            "system_prompt": (
                "You role-play as the user. Given the conversation, output ONLY a "
                "JSON array of 1-3 short messages the user is likely to send next, "
                "in the user's voice. No prose, no code fences."
            ),
            "model": None,
            "temperature": 0.7,
            "max_tokens": 300,
            "metadata": {},
        }


def load_runtime_config() -> Dict[str, Any]:
    """Return {mode, count, idle_seconds}, layering: file overrides → template
    metadata defaults → hard defaults."""
    cfg = dict(_DEFAULTS)
    # Template metadata defaults
    try:
        meta = (_load_impersonator_def().get("metadata") or {})
        if meta.get("suggestion_mode") in VALID_MODES:
            cfg["mode"] = meta["suggestion_mode"]
        if isinstance(meta.get("suggestion_count"), int):
            cfg["count"] = meta["suggestion_count"]
        if isinstance(meta.get("idle_seconds"), int):
            cfg["idle_seconds"] = meta["idle_seconds"]
    except Exception:
        pass
    # Per-machine file overrides
    try:
        if os.path.exists(_RUNTIME_CONFIG):
            with open(_RUNTIME_CONFIG, "r", encoding="utf-8") as fh:
                saved = json.load(fh) or {}
            if saved.get("mode") in VALID_MODES:
                cfg["mode"] = saved["mode"]
            if isinstance(saved.get("count"), int):
                cfg["count"] = max(1, min(5, saved["count"]))
            if isinstance(saved.get("idle_seconds"), int):
                cfg["idle_seconds"] = max(5, min(600, saved["idle_seconds"]))
    except Exception as e:
        logger.warning("suggestions: could not read runtime config: %s", e)
    return cfg


def save_runtime_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``updates`` into the runtime config file and return the new config."""
    cfg = load_runtime_config()
    if updates.get("mode") in VALID_MODES:
        cfg["mode"] = updates["mode"]
    if isinstance(updates.get("count"), int):
        cfg["count"] = max(1, min(5, updates["count"]))
    if isinstance(updates.get("idle_seconds"), int):
        cfg["idle_seconds"] = max(5, min(600, updates["idle_seconds"]))
    try:
        os.makedirs(os.path.dirname(_RUNTIME_CONFIG), exist_ok=True)
        with open(_RUNTIME_CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception as e:
        logger.warning("suggestions: could not write runtime config: %s", e)
    return cfg


def _parse_suggestions(raw: str, limit: int) -> List[str]:
    """Tolerantly pull a JSON array of short strings out of the model output."""
    if not raw:
        return []
    text = raw.strip()
    # Strip code fences if the model wrapped the array.
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        text = text.strip()
    # Find the first JSON array in the text.
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    out: List[str] = []
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, str):
                    s = item.strip()
                elif isinstance(item, dict):
                    s = str(item.get("text") or item.get("message") or "").strip()
                else:
                    s = ""
                if s:
                    out.append(s)
    except Exception:
        # Last resort: split lines that look like list items.
        for line in raw.splitlines():
            s = line.strip().lstrip("-*0123456789. ").strip().strip('"')
            if s and not s.startswith("[") and not s.startswith("]"):
                out.append(s)
    # De-dup (case-insensitive) preserving order, then clip to limit.
    seen = set()
    deduped = []
    for s in out:
        key = s.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(s)
    return deduped[:limit]


async def generate_suggestions(
    db: Any,
    user_id: str,
    session_id: Optional[str],
    *,
    count: Optional[int] = None,
) -> List[str]:
    """Produce up to ``count`` suggested next user-messages for the given session.

    Returns an empty list when the engine is off, credentials are missing, or
    anything fails — callers must treat suggestions as best-effort.
    """
    cfg = load_runtime_config()
    if cfg["mode"] == "off":
        return []
    limit = count or cfg["count"]

    impersonator = _load_impersonator_def()
    system_prompt = impersonator.get("system_prompt") or ""
    temperature = impersonator.get("temperature", 0.7)
    max_tokens = impersonator.get("max_tokens", 300)
    model = impersonator.get("model") or os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""

    if not model:
        logger.info("suggestions: no model configured; skipping")
        return []

    # Build the conversation history (best-effort; empty for a fresh chat).
    history: List[Dict[str, Any]] = []
    if session_id:
        try:
            from app.agent.session_history import build_openai_history_from_session
            history = await build_openai_history_from_session(db, user_id, session_id)
        except Exception as e:
            logger.warning("suggestions: could not load history: %s", e)
            history = []

    # Flatten the history into a readable transcript so the impersonator can read
    # it as context without us having to forward tool/system roles verbatim.
    transcript_lines: List[str] = []
    for m in history[-20:]:  # last ~20 turns is plenty of context
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            transcript_lines.append(f"User: {content.strip()}")
        elif role == "assistant":
            transcript_lines.append(f"Assistant: {content.strip()}")
    transcript = "\n".join(transcript_lines).strip()

    if transcript:
        user_msg = (
            "Conversation so far:\n\n" + transcript +
            f"\n\nReturn a JSON array of up to {limit} short messages the user is "
            "likely to want to send next, in the user's voice. JSON array only."
        )
    else:
        user_msg = (
            "The conversation has not started yet. Return a JSON array of up to "
            f"{limit} short, natural opening messages this user might send to the "
            "assistant. JSON array only."
        )

    try:
        from app.agent.loop import _get_client
        client = _get_client()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning("suggestions: LLM call failed: %s", e)
        return []

    return _parse_suggestions(raw, limit)
