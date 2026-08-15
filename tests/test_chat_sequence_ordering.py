import sqlite3
import unittest

from app.api.db_viewer import (
    _interaction_cursor_values,
    _interaction_order_key,
    _interaction_order_by,
    _interaction_message_phase,
)


class ChatSequenceOrderingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE interactions ("
            "id TEXT PRIMARY KEY, session_id TEXT, session_seq INTEGER, "
            "created_at TEXT)"
        )
        # Insert timestamps/rowids in an order that disagrees with event order.
        self.conn.executemany(
            "INSERT INTO interactions VALUES (?, 's', ?, ?)",
            [
                ("legacy", None, "2025-01-01 00:00:00"),
                ("third", 30, "2025-01-01 00:00:01"),
                ("first", 10, "2025-01-01 00:00:03"),
                ("second", 20, "2025-01-01 00:00:02"),
            ],
        )
        self.order_key = _interaction_order_key("rowid")
        self.order_desc = _interaction_order_by("rowid", "DESC")

    def tearDown(self):
        self.conn.close()

    def test_sequence_is_authoritative_over_timestamp_and_insert_order(self):
        rows = self.conn.execute(
            f"SELECT id FROM interactions ORDER BY {self.order_key}"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["legacy", "first", "second", "third"])

    def test_cursor_pages_without_duplicates_or_omissions(self):
        anchor = self.conn.execute(
            "SELECT session_seq, created_at, rowid FROM interactions WHERE id='second'"
        ).fetchone()
        newer = self.conn.execute(
            f"SELECT id FROM interactions WHERE ({self.order_key}) > (?, ?, ?, ?) "
            f"ORDER BY {self.order_key}",
            _interaction_cursor_values(*anchor),
        ).fetchall()
        older = self.conn.execute(
            f"SELECT id FROM interactions WHERE ({self.order_key}) < (?, ?, ?, ?) "
            f"ORDER BY {self.order_key}",
            _interaction_cursor_values(*anchor),
        ).fetchall()
        self.assertEqual([row[0] for row in older], ["legacy", "first"])
        self.assertEqual([row[0] for row in newer], ["third"])

    def test_explicit_message_phase_survives_persistence_projection(self):
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"main"}', "assistant", "complete", "{}"
            ),
            "main",
        )
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"final"}', "assistant", "complete", "{}"
            ),
            "final",
        )
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"progress"}', "assistant", "complete", "{}"
            ),
            "progress",
        )

    def test_legacy_tool_assistant_is_progress_not_final(self):
        output = '{"tool_calls":[{"id":"call-1"}]}'
        self.assertEqual(
            _interaction_message_phase(None, "assistant", "complete", output),
            "progress",
        )

    def test_terminal_status_overrides_stale_pending_metadata(self):
        self.assertEqual(
            _interaction_message_phase(
                '{"message_phase":"pending"}', "assistant", "error", "{}"
            ),
            "terminal",
        )

    def test_newest_window_reverses_every_order_component(self):
        newest = self.conn.execute(
            f"SELECT id FROM interactions ORDER BY {self.order_desc} LIMIT 2"
        ).fetchall()
        self.assertEqual([row[0] for row in newest], ["third", "second"])

    def test_order_key_rejects_untrusted_identifier(self):
        with self.assertRaises(ValueError):
            _interaction_order_key("rowid; DROP TABLE interactions")


if __name__ == "__main__":
    unittest.main()
