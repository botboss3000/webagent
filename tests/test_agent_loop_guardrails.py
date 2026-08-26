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
from app.agent.run_health import RunHealthTracker


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

    def test_run_health_detects_two_tool_oscillation_once(self):
        tracker = RunHealthTracker()
        event = None
        calls = (("read_source", "read|a"), ("search_source", "search|x")) * 3
        for tool, signature in calls:
            event = tracker.record_request(tool, turn=2, signature=signature) or event
        self.assertIsNotNone(event)
        self.assertEqual(event["reason"], "alternating_tool_loop")
        self.assertEqual(event["evidence"]["pattern"], "read_source<->search_source")
        self.assertIsNone(tracker.record_request(
            "read_source", turn=2, signature="read|a"
        ))

    def test_run_health_allows_alternating_tools_with_new_arguments(self):
        tracker = RunHealthTracker()
        events = [
            tracker.record_request(tool, turn=1, signature=signature)
            for tool, signature in (
                ("read_source", "read|a"), ("search_source", "search|a"),
                ("read_source", "read|b"), ("search_source", "search|b"),
                ("read_source", "read|c"), ("search_source", "search|c"),
            )
        ]
        self.assertTrue(all(event is None for event in events))

    def test_run_health_uses_sliding_error_window_and_cooldown(self):
        tracker = RunHealthTracker(error_window=4)
        self.assertIsNone(tracker.record_outcome(
            False, turn=1, tool="run_command", error_type="timeout", threshold=3
        ))
        tracker.record_outcome(True, turn=1, tool="read_source", threshold=3)
        tracker.record_outcome(False, turn=2, tool="run_command", threshold=3)
        event = tracker.record_outcome(
            False, turn=2, tool="run_command", error_type="timeout", threshold=3
        )
        self.assertEqual(event["evidence"]["failures_in_window"], 3)
        self.assertTrue(tracker.allow_watchdog(turn=2, cooldown_turns=2))
        self.assertFalse(tracker.allow_watchdog(turn=3, cooldown_turns=2))
        self.assertTrue(tracker.allow_watchdog(turn=4, cooldown_turns=2))


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
