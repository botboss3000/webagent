"""Per-instance device worker — the loop that makes a shared DB act like a fleet.

Runs on EVERY instance (NOT leader-gated — unlike the scheduler / event runtime,
which run in exactly one elected worker). Each tick it:

  1. Heartbeats this device's presence row (so other devices see it online).
  2. Reclaims any expired-lease jobs (a crashed claimer's work returns to pending).
  3. Atomically claims pending jobs addressed to THIS device (or broadcast), and
     runs each — building a session and running the agent loop locally, so the job
     executes with this machine's own tools / browser / files.

A short poll gives a responsive floor; ``kick()`` wakes the loop immediately when
a job is enqueued locally (or a future remote nudge arrives), so same-box
dispatch is instant rather than poll-latency.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from app.devices import identity

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.5      # seconds between ticks (responsiveness floor)
HEARTBEAT_EVERY = 15     # seconds between presence heartbeats
LEASE_SECONDS = 600      # a claimed job's lease before it's reclaimable
CLAIM_LIMIT = 4          # max jobs claimed per tick
MAX_JOB_AGE_SECONDS = 86400   # staleness cap: a job that has waited longer than
                              # this (e.g. a 'wait' target that stayed offline for
                              # a day) is skipped rather than fired absurdly late


class DeviceWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._kick = asyncio.Event()
        self._last_heartbeat = 0.0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="device_worker")
        logger.info("Device worker started (id=%s, label=%s, poll=%ss)",
                    identity.device_id(), identity.device_label(), POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def kick(self) -> None:
        """Wake the loop now (a job was just enqueued for us)."""
        try:
            self._kick.set()
        except Exception:
            pass

    async def _run(self) -> None:
        await asyncio.sleep(3)  # let startup settle
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Device worker tick failed: %s", e)
            # Wait for the next poll OR an immediate kick, whichever comes first.
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            finally:
                self._kick.clear()

    async def _tick(self) -> None:
        from app.db import get_db
        db = get_db()

        # 1. Heartbeat presence (throttled).
        now = time.monotonic()
        if self._last_heartbeat == 0.0 or (now - self._last_heartbeat) >= HEARTBEAT_EVERY:
            try:
                await db.device_heartbeat(
                    identity.device_id(),
                    label=identity.device_label(),
                    capabilities=identity.capabilities(),
                )
                self._last_heartbeat = now
            except Exception as e:
                logger.debug("device heartbeat failed: %s", e)

        # 2. Reclaim jobs whose claimer crashed mid-run.
        try:
            n = await db.reclaim_expired_device_jobs()
            if n:
                logger.info("Reclaimed %d expired device job(s)", n)
        except Exception as e:
            logger.debug("reclaim failed: %s", e)

        # 3. Claim + run jobs addressed to this device.
        try:
            jobs = await db.claim_device_jobs(
                identity.device_id(), limit=CLAIM_LIMIT, lease_seconds=LEASE_SECONDS)
        except Exception as e:
            err = str(e)
            if "no such table" in err or "does not exist" in err:
                return  # tables not migrated yet — try again next tick
            raise
        for job in jobs:
            asyncio.create_task(self._run_job(job), name=f"device_job_{job['id'][:8]}")

    async def _run_job(self, job: dict) -> None:
        from app.db import get_db
        db = get_db()
        job_id = job["id"]
        session_id = None
        try:
            # Staleness cap — never fire a job that has waited absurdly long
            # (the per-automation 'wait' offline policy holds jobs for an
            # offline target; this stops a day-old one running out of the blue).
            created = job.get("created_at")
            if created:
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(str(created))).total_seconds()
                except Exception:
                    age = 0
                if age > MAX_JOB_AGE_SECONDS:
                    await db.finish_device_job(
                        job_id, status="skipped",
                        error=f"stale: waited {int(age)}s (> {MAX_JOB_AGE_SECONDS}s); not run")
                    logger.info("Device job %s skipped (stale: %ds old)", job_id, int(age))
                    return
            session_id, reply = await self._execute(db, job)
            await db.finish_device_job(
                job_id, status="done", result_excerpt=reply or "", session_id=session_id)
            logger.info("Device job %s completed on %s", job_id, identity.device_id())
        except Exception as e:
            logger.exception("Device job %s failed: %s", job_id, e)
            try:
                await db.finish_device_job(
                    job_id, status="error", error=str(e)[:500], session_id=session_id)
            except Exception:
                pass

    async def _execute(self, db, job: dict) -> Tuple[Optional[str], str]:
        """Run the job's prompt on the target agent locally. Mirrors the minimal
        event-executor path: create a session, post the prompt, run the agent
        loop. Returns (session_id, reply_text)."""
        from app.agent.loop import run_agent_loop_buffered
        from app.agent.prompts import build_system_prompt

        owner_user_id = job.get("owner_user_id")
        agent_id = job.get("agent_id")
        prompt_text = job.get("prompt") or ""

        # Job payload carries optional routing hints from the sender.
        payload = job.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except Exception:
                payload = {}
        payload = payload or {}
        # A chat hand-off names the sender's existing session so the reply lands
        # back in that same conversation; an automation/broadcast job has none.
        run_in_session = (payload.get("run_in_session") or "").strip() or None
        execution_mode = payload.get("execution_mode") or "ask"

        agent = await db.get_agent_by_id(agent_id) if agent_id else None
        if not agent:
            raise RuntimeError(f"agent {agent_id!r} not found on this device")

        if run_in_session:
            # Run inside the sender's session. The user turn was already recorded
            # on the sending device and the reply is appended here over the shared
            # DB — so DON'T create a session or re-insert the user turn below.
            session_id = run_in_session
        else:
            session_id = f"dev-{uuid.uuid4().hex[:12]}"
            title = (job.get("target_label") or "Device job")[:60]
            meta = {"source": "device_job",
                    "from_instance": job.get("created_by_instance"),
                    "device": {"id": identity.device_id(), "label": identity.device_label()}}
            # Create the session (raw insert, like the event executor; SQL fallback
            # only on backends that expose a sqlite-style connection).
            try:
                raw = db.get_raw_client()
                raw.table("sessions").insert({
                    "id": session_id,
                    "user_id": owner_user_id,
                    "title": title,
                    "agent_id": agent_id,
                    "metadata": json.dumps(meta),
                }).execute()
            except Exception:
                if not hasattr(db, "_get_conn"):
                    raise
                conn = db._get_conn()
                try:
                    conn.execute(
                        "INSERT INTO sessions (id, user_id, title, agent_id, metadata) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (session_id, owner_user_id, title, agent_id, json.dumps(meta)),
                    )
                    conn.commit()
                finally:
                    conn.close()

        # System prompt with the agent's own resolved prompt slots (best-effort).
        context_docs = []
        try:
            resolved = await db.resolve_prompts(agent_id, user_id=owner_user_id)
            context_docs = [
                {"id": s["slot_name"], "context_type": s["slot_name"],
                 "title": s["slot_name"], "content": s["content"], "tags": []}
                for s in resolved
                if (s.get("content") or "").strip() and s.get("slot_name") != "automation"
            ]
        except Exception:
            context_docs = []
        overlay = [{"id": "_device_job", "content": (
            "[DEVICE JOB] Another of the user's devices handed this task to you to "
            "run with your own local tools. Created by instance: "
            f"{job.get('created_by_instance')}.")}]
        system_prompt = await build_system_prompt(
            overlay + context_docs, brain_context=None,
            user_id=owner_user_id, agent_id=agent_id)

        # Record the synthetic user turn so the run appears in the session — but
        # only for jobs that minted their own session. A chat hand-off's user
        # turn already exists in the sender's session (re-inserting = a dupe).
        if not run_in_session:
            try:
                await db.insert_interaction(
                    owner_user_id, session_id, role="user", content=prompt_text,
                    channel="device", sender_id=owner_user_id, receiver_id=agent_id,
                    source="device")
            except Exception:
                pass

        raw_allowed = agent.get("allowed_tools", [])
        if isinstance(raw_allowed, str):
            try:
                raw_allowed = json.loads(raw_allowed)
            except Exception:
                raw_allowed = []

        reply = await run_agent_loop_buffered(
            user_id=owner_user_id,
            session_id=session_id,
            user_message=prompt_text,
            system_prompt=system_prompt,
            agent_id=agent_id,
            history=None,
            channel="device",
            timeout_seconds=600,
            db=db,
            agent_template_id=agent.get("template_id"),
            allowed_tools=raw_allowed or None,
            max_turns=agent.get("max_turn_count", 0),
            execution_mode=execution_mode,
        )
        return session_id, reply or ""


_WORKER: Optional[DeviceWorker] = None


def get_worker() -> DeviceWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = DeviceWorker()
    return _WORKER


def kick() -> None:
    """Module-level convenience: wake this instance's worker immediately."""
    try:
        get_worker().kick()
    except Exception:
        pass


async def start_device_worker() -> None:
    await get_worker().start()


async def stop_device_worker() -> None:
    if _WORKER is not None:
        await _WORKER.stop()
