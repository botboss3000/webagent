from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch


class RecoveryErrorDetailTests(unittest.TestCase):
    def test_exhausted_notice_keeps_last_concrete_failure(self):
        from app.agent import runner

        session_id = "session-1"
        db = Mock()
        db.run_state_get = AsyncMock(return_value={
            "session_id": session_id,
            "user_id": "admin",
            "agent_id": "agent-1",
            "origin": "web",
            "stop_cause": "crash",
            "resume_attempts": 3,
            "error": "LLM call failed: Connection error.",
        })
        db.run_state_mark_failed = AsyncMock()
        db.next_session_seq = AsyncMock(return_value=42)
        db.insert_interaction = AsyncMock()
        manager = Mock()
        manager.is_running.return_value = False

        with patch("app.db.get_db", return_value=db), \
             patch("app.agent.run_manager.get_run_manager", return_value=manager), \
             patch.object(runner, "_classify_resume", AsyncMock(return_value=("ok", ""))), \
             patch.object(runner, "_effective_max", return_value=3):
            launched = asyncio.run(runner.resume_one(session_id))

        self.assertFalse(launched)
        db.run_state_mark_failed.assert_awaited_once_with(session_id, None)
        content = db.insert_interaction.await_args.kwargs["content"]
        self.assertIn("Last failure: LLM call failed: Connection error.", content)
        self.assertIn("retry budget was exhausted", content)


if __name__ == "__main__":
    unittest.main()
