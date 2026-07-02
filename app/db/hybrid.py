"""Hybrid local-first storage backend (SQLite hot store + Postgres authority).

WHAT THIS IS
------------
A decorator over TWO ``StorageBackend``-shaped objects — a LOCAL SQLite backend
and a REMOTE Postgres backend — that presents the single ``StorageBackend``
surface the rest of the app already calls. It is inserted in ``get_db()`` exactly
the way :class:`EncryptedStorageBackend` wraps a single backend today. Nothing
above the storage layer (agent loop, API, UI) changes.

Like ``EncryptedStorageBackend`` it deliberately does NOT inherit from the
``StorageBackend`` ABC (that would force us to re-declare ~100 abstract methods).
It duck-types: any method NOT explicitly overridden here is delegated by
``__getattr__`` to a default backend, so callers typed as ``StorageBackend`` are
unaffected.

DISPOSITIONS (see temp/hybrid-local-first-db-handoff.md §3)
----------------------------------------------------------
Every method resolves to one of four behaviours:

  * SERVER-AUTHORITATIVE (money / identity / coordination) → remote, synchronous.
    A local copy must NEVER decide authorization/billing. This is the default for
    anything not explicitly localized, so the app stays correct-by-default.
  * SYNCED (shared content/config)  → local read (fast) + local write-through, with
    the write enqueued to an outbox and pushed to remote in the background.
  * PRE-CACHED LOCAL (chat-start config) → same as Synced, warm local replica.
  * LOCAL-ONLY / LOCAL-HEAVY (disposable detail) → local only, never remote.

PHASING
-------
This file is built up phase by phase; each phase is independently shippable and
guarded so the live app is untouched until the hybrid is explicitly enabled.

  * Phase 1 (this commit): the seam. Everything delegates to REMOTE (the
    authority) — i.e. exactly today's behaviour when Postgres is the active DB —
    while a live LOCAL backend is constructed and held, ready for later phases.
    Zero behaviour change; proves the wrapper composes with encryption + the
    factory.
  * Phase 2: local-only tier (diagnostics / render recordings never hit remote).
  * Phase 3: the ``interactions`` split (fat tool payloads local, skeleton synced).
  * Phase 4: write-through + outbox sync for the rest of the Synced tier.

ENABLEMENT
----------
On by default. Turned off via ``app/db_hybrid.json`` (``{"enabled": false}``) or the
``WEBAGENT_DB_HYBRID`` env var when config is env-locked. Only ever built when a
reachable Postgres-family remote exists — on a local-only install it is a no-op
and ``get_db()`` returns the plain local backend as before.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Interactions split tuning ─────────────────────────────────────────────────
# A fat column value LARGER than this (chars) is pulled OFF the remote authority
# and kept only in the local hot store; anything at or under it stays inline on
# the remote skeleton (small + almost always valid JSON, so cross-device readers
# and any JSON consumers are unaffected). Phase 0 showed the `input` column alone
# is 44.9 MB across a few hundred tool rows — those whales are exactly what this
# gate moves local, while a short user message or tiny tool result rides along on
# the skeleton untouched. The sync engine's push imports this same constant, so
# the strip gate is defined in exactly one place.
_FAT_THRESHOLD_CHARS = 2048


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_local_payloads_sync(ids) -> Dict[str, dict]:
    """Read the full ``input``/``output`` for interaction ids from the LOCAL store
    directly (no HybridBackend instance needed), returning
    ``{id: {"input":.., "output":..}}``.

    For read paths that query the remote skeleton on their own connection (the
    admin DB Viewer) and want to restore the fat payloads the local-first hot
    store holds. Synchronous — opens the local (possibly SQLCipher) file via
    db_crypto. Returns ``{}`` on any error / when a row isn't local, so callers
    can merge unconditionally."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return {}
    try:
        from app.db import db_crypto
        from app.db.local import DEFAULT_DB_PATH
        conn = db_crypto.connect(DEFAULT_DB_PATH, "local")
    except Exception:
        return {}
    out: Dict[str, dict] = {}
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = ("SELECT id, input, output FROM interactions "
                 f"WHERE id IN ({','.join('?' * len(chunk))})")
            for row in conn.execute(q, chunk).fetchall():
                out[row["id"]] = {"input": row["input"], "output": row["output"]}
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out

# ── Enablement flag ──────────────────────────────────────────────────────────
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FLAG_FILE = os.path.join(_APP_DIR, "db_hybrid.json")
_enabled_cache: Optional[bool] = None


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


def hybrid_enabled() -> bool:
    """True if the hybrid local-first layer is switched on for this install.

    Env-locked deployments read ``WEBAGENT_DB_HYBRID`` (1/true/yes/on/absent=on);
    otherwise the persisted ``db_hybrid.json`` flag. Absent/unreadable = ON (the
    default), so a fresh install ships with local-first storage active whenever
    a Postgres-family remote is configured (a no-op otherwise)."""
    global _enabled_cache
    if _enabled_cache is not None:
        return _enabled_cache
    val = True
    if _env_locked():
        raw = os.environ.get("WEBAGENT_DB_HYBRID", "").strip().lower()
        val = raw not in ("0", "false", "no", "off") if raw else True
    else:
        try:
            if os.path.exists(_FLAG_FILE):
                with open(_FLAG_FILE, "r") as f:
                    val = bool((json.load(f) or {}).get("enabled", True))
        except Exception as e:
            logger.warning("Failed to read db_hybrid.json: %s", e)
            val = True
    _enabled_cache = val
    return val


def set_hybrid_enabled(on: bool) -> None:
    """Persist the hybrid on/off flag. Takes effect on the next ``get_db()`` build
    (callers should ``reset_db_instance()`` after flipping it)."""
    global _enabled_cache
    _enabled_cache = bool(on)
    if _env_locked():
        return
    try:
        tmp = _FLAG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"enabled": bool(on)}, f, indent=2)
        os.replace(tmp, _FLAG_FILE)
    except Exception as e:
        logger.warning("Failed to write db_hybrid.json: %s", e)


# ── The backend ──────────────────────────────────────────────────────────────


class HybridBackend:
    """Local-first decorator over a (local, remote) backend pair.

    ``local`` is a LocalBackend (SQLite hot store); ``remote`` is a Postgres-family
    backend (the shared authority). Both share LocalBackend's method surface —
    PostgresBackend subclasses LocalBackend — which is what makes per-method
    routing tractable.

    Phase 1: every StorageBackend method delegates to ``remote`` via ``__getattr__``
    (unchanged behaviour). ``local`` is live and reachable via ``self.local`` for
    the phases that follow.
    """

    def __init__(self, local, remote) -> None:
        self._local = local
        self._remote = remote

    # Expose the two halves for the sync engine + later-phase overrides + tests.
    @property
    def local(self):
        return self._local

    @property
    def remote(self):
        return self._remote

    # ── Pass-through ─────────────────────────────────────────────────────────
    def __getattr__(self, name):
        # Only invoked for attributes not found on the instance/class, so any
        # method explicitly overridden below shadows this. Default target is the
        # REMOTE authority — Phase 1 behaviour and the safe default for every
        # server-authoritative method thereafter.
        return getattr(self._remote, name)

    def get_raw_client(self):
        """Direct query client for admin/tools. Points at the remote authority so
        admin reads/writes see the shared source of truth. (Local-only tables are
        queried through the typed methods, which route to ``local``.)"""
        return self._remote.get_raw_client()

    # ══════════════════════════════════════════════════════════════════════════
    # Stage 2 — local-first transcript, background push
    #
    # The FULL interaction row (content + the fat `input`/`output`) is written to
    # the LOCAL SQLite hot store FIRST — the id is minted locally and the write
    # completes without touching the network, so a turn never waits on the remote.
    # The row is then recorded in the local OUTBOX; the background SyncEngine
    # (app/db/sync/) drains it to the remote authority within a few seconds as a
    # SKELETON (fat columns over the size gate stripped to NULL), keeping a second
    # device / the shared record current. Reads are served from SQLite; on first
    # access to a session its row + existing transcript are pulled from the remote
    # into SQLite ("warm on chat open"), so history is present locally. Fat detail
    # for rows another device wrote is the skeleton only (handoff §7).
    #
    # (Stage 1 wrote the remote skeleton synchronously inline; Stage 2 replaced
    # that inline round-trip with the outbox enqueue below.)
    # ══════════════════════════════════════════════════════════════════════════

    # Full interactions column set (matches app/db/schema/tables.py), minus
    # created_at which the local row defaults to now on a fresh write and is
    # preserved verbatim when mirroring existing remote rows.
    _ICOLS = ("id", "session_id", "parent_id", "role", "content", "tool_name",
              "tool_call_id", "channel", "metadata", "input", "output", "source",
              "from_id", "to_id", "session_seq", "turn_id", "turn_seq", "status")

    # (The remote skeleton's fat-column stripping now lives in the sync engine's
    # push, gated by the shared _FAT_THRESHOLD_CHARS above.)

    # ── Warm SQLite: pull a session + its transcript from remote on first touch ──

    async def _ensure_local_session(self, user_id: str, session_id: str) -> None:
        """Mirror a session into the local store once per process. Cheap no-op
        after the first call for a given session."""
        seen = getattr(self, "_mirrored", None)
        if seen is None:
            seen = set()
            self._mirrored = seen
        if session_id in seen:
            return
        seen.add(session_id)
        try:
            rraw = self._remote.get_raw_client()
            srows = rraw.table("sessions").select("*").eq("id", session_id).execute().data or []
            if not srows:
                return
            # Drop NULL-valued keys so LOCAL defaults fill NOT-NULL columns the
            # remote left null (e.g. sessions.hidden / sort_order) — an explicit
            # NULL would violate the local NOT NULL constraint.
            srow = {k: v for k, v in srows[0].items() if v is not None}
            self._local.get_raw_client().table("sessions").upsert(srow, on_conflict="id").execute()
            irows = rraw.table("interactions").select("*").eq("session_id", session_id).execute().data or []
            if irows:
                cols = list(irows[0].keys())
                col_sql = ",".join(cols)
                placeholders = ",".join("?" * len(cols))
                values = [tuple(r.get(c) for c in cols) for r in irows]
                async with self._local._write_lock:
                    conn = self._local._get_conn()
                    try:
                        conn.executemany(
                            f"INSERT OR IGNORE INTO interactions ({col_sql}) VALUES ({placeholders})",
                            values,
                        )
                        conn.commit()
                    finally:
                        conn.close()
        except Exception as e:
            logger.warning("hybrid: mirroring session %s to local failed: %s", session_id, e)
            # Don't wedge writes if the mirror hiccups — allow the local row to be
            # created; the session upsert above usually succeeded first.

    async def _local_write_full(self, row: Dict[str, Any]) -> None:
        """Upsert one FULL interaction row into the local store (all columns)."""
        cols = [c for c in self._ICOLS if c in row]
        col_sql = ",".join(cols)
        placeholders = ",".join("?" * len(cols))
        set_sql = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        vals = tuple(row.get(c) for c in cols)
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                conn.execute(
                    f"INSERT INTO interactions ({col_sql}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {set_sql}",
                    vals,
                )
                conn.commit()
            except Exception as e:
                logger.warning("hybrid: local full-row write failed for %s: %s", row.get("id"), e)
            finally:
                conn.close()

    async def _local_update(self, interaction_id: str, fields: Dict[str, Any]) -> None:
        """Patch selected columns of a local interaction row (streaming finalize)."""
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [interaction_id]
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                conn.execute(f"UPDATE interactions SET {sets} WHERE id=?", vals)
                conn.commit()
            except Exception as e:
                logger.warning("hybrid: local update failed for %s: %s", interaction_id, e)
            finally:
                conn.close()

    def _outbox(self):
        """Lazily-built local outbox — the durable queue the background SyncEngine
        drains to the remote authority. Kept here so a write can enqueue even when
        no engine is running yet (it just accumulates until one starts)."""
        ob = getattr(self, "_outbox_inst", None)
        if ob is None:
            from app.db.sync import Outbox
            ob = Outbox(self._local)
            self._outbox_inst = ob
        return ob

    async def _enqueue(self, table: str, row_id: str) -> None:
        """Record a local row for background push; never let a queue hiccup break
        the write (the row is already durable locally — worst case it syncs late)."""
        try:
            await self._outbox().enqueue(table, row_id)
        except Exception as e:
            logger.warning("hybrid: outbox enqueue for %s/%s failed: %s", table, row_id, e)

    async def insert_interaction(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        parent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[str] = None,
        input_data: Optional[str] = None,
        output_data: Optional[str] = None,
        sender_id: Optional[str] = None,
        receiver_id: Optional[str] = None,
        source: Optional[str] = None,
        session_seq: Optional[int] = None,
        turn_id: Optional[str] = None,
        turn_seq: Optional[int] = None,
        status: str = "complete",
    ) -> str:
        # Local-first: mint the id here, write the FULL row to SQLite (source of
        # truth for reads), then enqueue it for the background push to remote. No
        # network on the write path.
        rid = str(uuid.uuid4())
        await self._ensure_local_session(user_id, session_id)
        await self._local_write_full({
            "id": rid, "session_id": session_id, "parent_id": parent_id, "role": role,
            "content": content, "tool_name": tool_name, "tool_call_id": tool_call_id,
            "channel": channel, "metadata": metadata, "input": input_data,
            "output": output_data, "source": source or "user", "from_id": sender_id,
            "to_id": receiver_id, "session_seq": session_seq, "turn_id": turn_id,
            "turn_seq": turn_seq, "status": status,
        })
        await self._enqueue("interactions", rid)
        return rid

    async def insert_interactions_batch(self, rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return []
        # Local-first: mint ids, write the FULL rows to SQLite, enqueue for push.
        ids: List[str] = []
        seen_sessions = set()
        for r in rows:
            sid = r.get("session_id")
            if sid and sid not in seen_sessions:
                seen_sessions.add(sid)
                await self._ensure_local_session(r.get("user_id", ""), sid)
            rid = r.get("id") or str(uuid.uuid4())
            ids.append(rid)
            await self._local_write_full({
                "id": rid, "session_id": sid, "parent_id": r.get("parent_id"),
                "role": r.get("role", "tool"), "content": r.get("content", ""),
                "tool_name": r.get("tool_name"), "tool_call_id": r.get("tool_call_id"),
                "channel": r.get("channel"), "metadata": r.get("metadata"),
                "input": r.get("input"), "output": r.get("output"),
                "source": r.get("source") or "user", "from_id": r.get("from_id"),
                "to_id": r.get("to_id"), "session_seq": r.get("session_seq"),
                "turn_id": r.get("turn_id"), "turn_seq": r.get("turn_seq"),
                "status": r.get("status", "complete"),
            })
        try:
            await self._outbox().enqueue_many([("interactions", i, "upsert") for i in ids])
        except Exception as e:
            logger.warning("hybrid: batch outbox enqueue failed: %s", e)
        return ids

    async def update_interaction(
        self,
        interaction_id: str,
        *,
        content: Optional[str] = None,
        status: Optional[str] = None,
        output_data: Optional[str] = None,
        metadata: Optional[str] = None,
    ) -> bool:
        # Local-first: patch the full row in SQLite (keeps the fat output), then
        # enqueue so the background push refreshes the remote skeleton.
        await self._local_update(interaction_id, {
            "content": content, "status": status, "output": output_data, "metadata": metadata,
        })
        await self._enqueue("interactions", interaction_id)
        return True

    async def fetch_interactions(self, user_id: str, session_id: str):
        # Warm the local copy on first touch, then read the transcript LOCALLY.
        # Safety net: if the local mirror is missing (a mirror hiccup left the
        # session out of the local store), fall back to the remote authority so a
        # read NEVER hard-fails a chat — degrade, don't break.
        await self._ensure_local_session(user_id, session_id)
        try:
            return await self._local.fetch_interactions(user_id, session_id)
        except PermissionError:
            logger.warning("hybrid: local session %s absent — reading from remote", session_id)
            return await self._remote.fetch_interactions(user_id, session_id)

    async def fetch_first_user_messages(self, user_id: str, session_id: str, limit: int = 3):
        await self._ensure_local_session(user_id, session_id)
        try:
            return await self._local.fetch_first_user_messages(user_id, session_id, limit)
        except PermissionError:
            return await self._remote.fetch_first_user_messages(user_id, session_id, limit)

    # ══════════════════════════════════════════════════════════════════════════
    # Stage 3 — pre-cache agent config on chat open ("instant but double-check")
    #
    # An agent's config (its `agents` row + `agent_prompts` slots) is needed at
    # chat-open and on every turn, but unlike the append-only transcript it is
    # CENTRALLY AUTHORED and can change. So we keep a warm LOCAL replica and, on a
    # config read, do ONE cheap round-trip to the remote authority for a version
    # STAMP (the newest `updated_at` across the agent's config rows). If the stamp
    # is unchanged since we last mirrored, the heavy config is served from local
    # with no further network; if it moved (an edit here or on another device) we
    # pull the bundle fresh first. The stamp check is itself cached for a few
    # seconds so the several config reads in one turn collapse to one verification.
    #
    # Config is Synced-tier (not money/identity), so a bounded (~stamp-TTL) window
    # of staleness is acceptable (handoff §8/§9). Authority still lives on the
    # remote — writes delegate there and bump the stamp, which is what refreshes
    # us. Neither `agents` nor `agent_prompts` has a foreign key, so the local
    # mirror writes can't trip an FK.
    # ══════════════════════════════════════════════════════════════════════════

    _AGENT_STAMP_TTL = 5.0  # seconds a freshness check is trusted before re-verifying

    async def _agent_config_stamp(self, agent_id: str) -> Optional[str]:
        """One round-trip: the newest `updated_at` across the agent's config rows
        on the REMOTE authority, as a string. None on any error / missing agent →
        the caller then reads remote (correct, just not accelerated)."""
        try:
            conn = self._remote._get_conn()
            try:
                row = conn.execute(
                    "SELECT MAX(u) FROM ("
                    "SELECT updated_at AS u FROM agents WHERE id = ? "
                    "UNION ALL "
                    "SELECT updated_at AS u FROM agent_prompts WHERE agent_id = ?"
                    ") x",
                    (agent_id, agent_id),
                ).fetchone()
                return str(row[0]) if row and row[0] is not None else None
            finally:
                conn.close()
        except Exception as e:
            logger.debug("hybrid: agent config stamp for %s failed: %s", agent_id, e)
            return None

    async def _mirror_agent_config(self, agent_id: str) -> bool:
        """Pull the agent's config bundle (agents row + agent_prompts slots) from
        the remote authority into local SQLite. The prompt slots are REPLACED
        wholesale so a slot deleted on the remote also disappears locally."""
        try:
            rraw = self._remote.get_raw_client()
            arows = rraw.table("agents").select("*").eq("id", agent_id).execute().data or []
            if not arows:
                return False
            arow = {k: v for k, v in arows[0].items() if v is not None}
            prows = rraw.table("agent_prompts").select("*").eq("agent_id", agent_id).execute().data or []
            acols = list(arow.keys())
            a_set = ",".join(f"{c}=excluded.{c}" for c in acols if c != "id")
            async with self._local._write_lock:
                conn = self._local._get_conn()
                try:
                    conn.execute(
                        f"INSERT INTO agents ({','.join(acols)}) "
                        f"VALUES ({','.join('?' * len(acols))}) "
                        f"ON CONFLICT(id) DO UPDATE SET {a_set}",
                        tuple(arow.get(c) for c in acols),
                    )
                    conn.execute("DELETE FROM agent_prompts WHERE agent_id = ?", (agent_id,))
                    for pr in prows:
                        prow = {k: v for k, v in pr.items() if v is not None}
                        pcols = list(prow.keys())
                        conn.execute(
                            f"INSERT INTO agent_prompts ({','.join(pcols)}) "
                            f"VALUES ({','.join('?' * len(pcols))})",
                            tuple(prow.get(c) for c in pcols),
                        )
                    conn.commit()
                finally:
                    conn.close()
            return True
        except Exception as e:
            logger.warning("hybrid: mirroring agent config %s failed: %s", agent_id, e)
            return False

    async def _ensure_local_agent(self, agent_id: str) -> bool:
        """Make the local config replica for `agent_id` usable + fresh; return True
        if a local config read is safe. Cheap remote stamp check, TTL-cached so
        repeated reads within one turn don't each pay a round-trip."""
        if not agent_id:
            return False
        stamps = getattr(self, "_agent_stamps", None)
        if stamps is None:
            stamps = {}
            self._agent_stamps = stamps
            self._agent_checked: Dict[str, float] = {}
        now = time.monotonic()
        # Recently verified? Trust the local replica without another round-trip.
        if agent_id in stamps and (now - self._agent_checked.get(agent_id, 0.0)) < self._AGENT_STAMP_TTL:
            return True
        remote_stamp = await self._agent_config_stamp(agent_id)
        if remote_stamp is None:
            return False  # can't verify freshness → read remote
        self._agent_checked[agent_id] = now
        if stamps.get(agent_id) == remote_stamp:
            return True  # local already fresh
        if await self._mirror_agent_config(agent_id):
            stamps[agent_id] = remote_stamp
            return True
        return False

    async def get_agent_by_id(self, agent_id: str):
        if await self._ensure_local_agent(agent_id):
            try:
                res = await self._local.get_agent_by_id(agent_id)
                if res is not None:
                    return res
            except Exception as e:
                logger.debug("hybrid: local get_agent_by_id fell back: %s", e)
        return await self._remote.get_agent_by_id(agent_id)

    async def fetch_agent_by_id_with_context(self, agent_id, context_types=None, user_id=None):
        if await self._ensure_local_agent(agent_id):
            try:
                res = await self._local.fetch_agent_by_id_with_context(agent_id, context_types, user_id)
                if res is not None:
                    return res
            except Exception as e:
                logger.debug("hybrid: local fetch_agent_by_id_with_context fell back: %s", e)
        return await self._remote.fetch_agent_by_id_with_context(agent_id, context_types, user_id)

    async def resolve_prompts(self, agent_id, user_id=None):
        if await self._ensure_local_agent(agent_id):
            try:
                return await self._local.resolve_prompts(agent_id, user_id=user_id)
            except Exception as e:
                logger.debug("hybrid: local resolve_prompts fell back: %s", e)
        return await self._remote.resolve_prompts(agent_id, user_id=user_id)

    async def assemble_prompt(self, agent_id, user_id=None):
        if await self._ensure_local_agent(agent_id):
            try:
                return await self._local.assemble_prompt(agent_id, user_id=user_id)
            except Exception as e:
                logger.debug("hybrid: local assemble_prompt fell back: %s", e)
        return await self._remote.assemble_prompt(agent_id, user_id=user_id)
