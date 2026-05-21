"""Google Cloud Scheduler backend — stub.

This is the extension point for delegating scheduled job firing to Google
Cloud Scheduler. The intended flow is:

1. On ``sync_tasks``, push (or update) a job per ``agent_automations`` row
   that targets a public webhook on this server.
2. Google Cloud Scheduler hits that webhook on schedule.
3. The webhook handler resolves the automation row and runs the same
   executor used by ``LocalScheduler``.

Not yet implemented — the App Config UI shows this as "coming soon".
"""

from __future__ import annotations

import logging
from typing import Optional

from app.scheduler.base import SchedulerBackend

logger = logging.getLogger(__name__)


class GoogleScheduler(SchedulerBackend):
    name = "google"

    async def start(self) -> None:
        logger.info("GoogleScheduler.start() — stub (no-op).")

    async def stop(self) -> None:
        logger.info("GoogleScheduler.stop() — stub (no-op).")

    async def sync_tasks(self, agent_id: Optional[str] = None) -> None:
        logger.info("GoogleScheduler.sync_tasks(%s) — stub.", agent_id)

    async def run_now(self, automation_id: str) -> dict:
        return {"ok": False, "error": "GoogleScheduler is not yet implemented."}

    async def get_status(self) -> dict:
        return {
            "provider": self.name,
            "running": False,
            "note": "Google Cloud Scheduler integration is not yet implemented.",
        }
