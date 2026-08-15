import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.loop import (
    _assistant_output,
    _bounded_turn_snapshot,
    _canonical_tool_signature,
)


class AgentLoopGuardrailTests(unittest.TestCase):
    def test_canonical_tool_signature_ignores_json_order_and_applies_defaults(self):
        schema = {
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            }
        }
        implicit = _canonical_tool_signature(
            "session_search", '{"query":"chat"}', schema
        )
        explicit = _canonical_tool_signature(
            "session_search", '{"limit":10, "query":"chat"}', schema
        )
        self.assertEqual(implicit, explicit)

    def test_intermediate_assistant_output_is_minimal(self):
        raw = _assistant_output(
            tool_calls=[{"function": {"name": "search", "arguments": "{}"}}]
        )
        payload = json.loads(raw)
        self.assertEqual(set(payload), {"role", "tool_calls"})
        self.assertNotIn("_sent_messages", payload)
        self.assertNotIn("_sent_tools", payload)
        self.assertNotIn("content", payload)

    def test_final_snapshot_is_bounded_and_keeps_newest_messages(self):
        messages = [
            {"role": "user", "content": f"old-{i}-" + ("x" * 4000)}
            for i in range(100)
        ]
        snapshot = _bounded_turn_snapshot(messages, [], max_bytes=20_000)
        encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), 20_000)
        self.assertTrue(snapshot["_sent_messages"][-1]["content"].startswith("old-99-"))
        self.assertEqual(snapshot["_snapshot_truncated"]["messages_total"], 100)

    def test_final_snapshot_serialization_is_linear(self):
        messages = [
            {"role": "user", "content": f"message-{i}-" + ("x" * 2000)}
            for i in range(500)
        ]
        with patch("app.agent.loop.json.dumps", wraps=json.dumps) as dumps:
            _bounded_turn_snapshot(messages, [], max_bytes=20_000)
        self.assertLessEqual(dumps.call_count, len(messages) + 1)


class TempDbSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_visualizer_sequence_backfill_uses_active_temp_db(self):
        from app.api import chat
        from app.db.local import LocalBackend

        with tempfile.TemporaryDirectory() as td:
            db = LocalBackend(db_path=str(Path(td) / "optimizer.db"))
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
                    ("optimizer-test", "admin", "test", "{}"),
                )
                conn.commit()
            finally:
                conn.close()

            interaction_id = await db.insert_interaction(
                "admin", "optimizer-test", role="assistant", content="hello"
            )
            await chat._emit_to_visualizers(
                "optimizer-test",
                {
                    "type": "db",
                    "role": "assistant",
                    "id": interaction_id,
                    "session_seq": 42,
                    "turn_id": "turn-1",
                    "turn_seq": 7,
                },
                db_override=db,
            )
            # A later completion/update event has its own replay sequence but
            # must not renumber the already-created interaction.
            await chat._emit_to_visualizers(
                "optimizer-test",
                {
                    "type": "db",
                    "op": "update_interaction",
                    "role": "assistant",
                    "id": interaction_id,
                    "session_seq": 99,
                    "turn_id": "turn-1",
                    "turn_seq": 64,
                },
                db_override=db,
            )

            conn = db._get_conn()
            try:
                row = conn.execute(
                    "SELECT session_seq, turn_id, turn_seq FROM interactions WHERE id=?",
                    (interaction_id,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(tuple(row), (42, "turn-1", 7))
