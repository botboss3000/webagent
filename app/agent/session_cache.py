"""
In-memory LRU message array cache keyed by authenticated user and session.

Why it exists
-------------
Every turn of an agent session needs the full ``messages[]`` array (system
prompts + conversation history) sent to the LLM.  Without a cache the server
rebuilds this array from scratch each turn — re-reading the DB (``chat.py``),
re-deserializing the browser's transcript (``browser_storage.py``), and
re-concatenating every layer in ``_build_layered_messages``.

With this cache:
  1. Turn 1 cache-miss → full build as today.
  2. Turn 2+ cache-hit → the stored ``messages[]`` (with full prior
     conversation) is deep-copied, the new user message is appended, and
     the LLM sees a byte-identical prefix → the provider's prompt-cache
     fires (90 % cheaper input tokens).  The prior assistant reply + tool
     interactions are present in the cached array, so the LLM gets full
     conversation context.
  3. Callers (chat.py / browser_storage.py) skip their expensive DB reads
     and JSON deserialization because they check the cache first.

Eviction
--------
- **TTL** — entries older than ``_TTL_SECONDS`` are treated as misses.
- **Capacity** — when the dict exceeds ``_MAX_ENTRIES`` the oldest entry is
  dropped.
- **Hash mismatch** — if the system-prompt-fingerprint changes (agent config
  edited) the cache entry is invalidated and rebuilt.

Thread safety
-------------
``asyncio.Lock`` guards the internal dict so concurrent event-loop tasks
(rare but possible during background automations) do not race.

Telemetry
---------
Every public ``get()`` / ``set()`` call records a hit or miss to the in-memory
metrics recorder (``app.metrics.record_cache_hit`` / ``record_cache_miss``).
The admin Dashboard's ``/metrics`` endpoint surfaces these live — no DB read.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.metrics import record_cache_hit, record_cache_miss

from app.agent.cache_profiles import stable_hash

logger = logging.getLogger(__name__)

_TTL_SECONDS = 1800   # 30 min — after that, rebuild from source
_MAX_ENTRIES = 1000   # ~50 MB at 50 KB/session — safe for most deployments
_MAX_ENTRY_BYTES = 500_000  # skip entries >500 KB — they won't fit prompt-cache anyway


class SessionMessageCache:
    """LRU-ish session message cache with TTL and capacity eviction."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # (user_id, session_id) →
        # (timestamp, messages[], system_hash, authoritative_history_hash)
        self._data: Dict[
            Tuple[str, str],
            Tuple[float, List[Dict[str, Any]], str, str],
        ] = {}

    # ── public API ──────────────────────────────────────────────────────────

    async def has(self, user_id: str, session_id: str) -> bool:
        """Return True if a non-expired cache entry exists for this session."""
        key = (user_id, session_id)
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            ts = entry[0]
            if time.time() - ts > _TTL_SECONDS:
                del self._data[key]
                return False
            return True

    async def get(
        self,
        user_id: str,
        session_id: str,
        system_hash: Optional[str] = None,
        history_hash: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Return a shallow-copied messages array if cache is warm, else None.

        When *system_hash* is provided the entry is also validated against it;
        a mismatch triggers eviction (system prompt has changed).  When omitted
        (e.g. a caller that can't compute the same fingerprint) only TTL is
        checked — loop.py will validate the full hash on its own get().
        """
        key = (user_id, session_id)
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                record_cache_miss("session_messages")
                return None
            ts, messages, cached_hash, cached_history_hash = entry
            # TTL check
            if time.time() - ts > _TTL_SECONDS:
                del self._data[key]
                record_cache_miss("session_messages")
                return None
            # System prompt changed (agent config updated) → rebuild
            if system_hash is not None and cached_hash != system_hash:
                del self._data[key]
                record_cache_miss("session_messages")
                return None
            if history_hash is None or cached_history_hash != history_hash:
                del self._data[key]
                record_cache_miss("session_messages")
                return None
            # Deep-copy: the loop mutates the returned list (appending tool
            # results and assistant replies) — a shallow copy would share the
            # inner dicts and corrupt the cached snapshot.
        import copy as _copy
        record_cache_hit("session_messages")
        return _copy.deepcopy(messages)

    async def set(
        self,
        user_id: str,
        session_id: str,
        messages: List[Dict[str, Any]],
        system_hash: str,
        history_hash: str,
    ) -> None:
        """Store a snapshot of the messages array for this session.

        The caller passes the *final* ``messages[]`` array after a turn completes,
        with the last assistant response appended.  The FULL array is stored so
        the next turn's ``get()`` returns the complete conversation context.

        Size guard: entries larger than ``_MAX_ENTRY_BYTES`` are silently skipped
        rather than cached — they would bloat the process and are unlikely to
        yield prompt-cache hits anyway (deep histories change too much).
        """
        if not history_hash:
            return
        import json as _json
        import copy as _copy
        try:
            _rough = len(_json.dumps(messages, default=str))
        except Exception:
            _rough = 0
        if _rough > _MAX_ENTRY_BYTES:
            return  # skip — entry is too large for the cache

        # Deep-copy so the caller's mutations (the loop appends tool results and
        # assistant replies into the same list) never corrupt the cached snapshot.
        snapshot = _copy.deepcopy(messages)
        key = (user_id, session_id)
        async with self._lock:
            # Capacity evict: drop oldest entry when full
            if len(self._data) >= _MAX_ENTRIES:
                oldest_key = min(self._data.keys(), key=lambda item: self._data[item][0])
                del self._data[oldest_key]
            self._data[key] = (
                time.time(), snapshot, system_hash, history_hash,
            )


class ToolDefsCache:
    """In-memory cache of serialized ``tool_definitions`` arrays.

    Tool schemas are the same across every turn of a session (and often across
    many sessions using the same agent).  Rebuilding them each turn requires
    iterating over every loaded tool, checking ability modes and visibility
    rules, and constructing full JSON-Schema objects — pure CPU work that
    produces the same result.

    Key: stable hash of ``(agent_id, active_tools, active_abilities,
    suppressed_abilities)``.  Invalidates automatically when the user loads a
    new tool, toggles an ability, or the agent config changes.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    async def get(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        """Return the cached tool_definitions list, or None."""
        async with self._lock:
            entry = self._data.get(cache_key)
            if entry is None:
                record_cache_miss("tool_defs")
                return None
            ts, defs = entry
            if time.time() - ts > _TTL_SECONDS:
                del self._data[cache_key]
                record_cache_miss("tool_defs")
                return None
        record_cache_hit("tool_defs")
        return list(defs)  # shallow copy

    async def set(self, cache_key: str, tool_definitions: List[Dict[str, Any]]) -> None:
        async with self._lock:
            if len(self._data) >= _MAX_ENTRIES:
                oldest = min(self._data.keys(), key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[cache_key] = (time.time(), list(tool_definitions))


# ── helpers ───────────────────────────────────────────────────────────────────

def compute_tool_defs_cache_key(
    agent_id: str,
    active_tools: set[str],
    active_abilities: set[str],
    suppressed_abilities: set[str],
) -> str:
    """Deterministic key for the tool_definitions array."""
    return stable_hash([
        agent_id,
        sorted(active_tools),
        sorted(active_abilities),
        sorted(suppressed_abilities),
    ])


# ── singleton accessors ───────────────────────────────────────────────────────

_cache: Optional[SessionMessageCache] = None
_tool_cache: Optional[ToolDefsCache] = None


def get_session_cache() -> SessionMessageCache:
    global _cache
    if _cache is None:
        _cache = SessionMessageCache()
    return _cache


def get_tool_defs_cache() -> ToolDefsCache:
    global _tool_cache
    if _tool_cache is None:
        _tool_cache = ToolDefsCache()
    return _tool_cache
