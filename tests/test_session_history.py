"""Unit tests for DB → OpenAI message history mapping."""

import json
import unittest
from datetime import datetime, timezone

from app.agent.session_history import (
    TOOL_MARKER,
    build_openai_history_from_session,
    interactions_to_openai_messages,
)
from app.agent.compaction import maybe_compact
from app.models.schemas import InteractionRecord


def _ir(
    iid: str,
    role: str,
    content: str,
    *,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    output: str | None = None,
) -> InteractionRecord:
    return InteractionRecord(
        id=iid,
        session_id="sess",
        parent_id=None,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        metadata=None,
        output=output,
        created_at=datetime.now(timezone.utc),
    )


class InteractionsToOpenaiMessagesTests(unittest.TestCase):
    def test_user_only(self) -> None:
        rows = [_ir("1", "user", "hello")]
        out = interactions_to_openai_messages(rows)
        self.assertEqual(out, [{"role": "user", "content": "hello"}])

    def test_exclude_current_user(self) -> None:
        rows = [_ir("1", "user", "first"), _ir("2", "user", "second")]
        out = interactions_to_openai_messages(rows, exclude_interaction_ids={"2"})
        self.assertEqual(out, [{"role": "user", "content": "first"}])

    def test_omits_internal_memory_tools(self) -> None:
        rows = [
            _ir("1", "user", "hi"),
            _ir("2", "tool", '{"query":"hi"}', tool_name="memory_search", tool_call_id=None),
        ]
        out = interactions_to_openai_messages(rows)
        self.assertEqual(out, [{"role": "user", "content": "hi"}])

    def test_assistant_with_tool_calls_and_tool_results(self) -> None:
        spec = [{"name": "web_search", "args": json.dumps({"q": "cats"})}]
        assistant_content = "I'll search." + TOOL_MARKER + json.dumps(spec)
        rows = [
            _ir("u", "user", "find cats"),
            _ir("a", "assistant", assistant_content),
            _ir(
                "t",
                "tool",
                '{"results":[]}',
                tool_name="web_search",
                tool_call_id="call_abc",
            ),
        ]
        out = interactions_to_openai_messages(rows)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0], {"role": "user", "content": "find cats"})
        self.assertEqual(out[1]["role"], "assistant")
        self.assertEqual(out[1]["content"], "I'll search.")
        self.assertEqual(len(out[1]["tool_calls"]), 1)
        self.assertEqual(out[1]["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(out[1]["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(out[2]["role"], "tool")
        self.assertEqual(out[2]["tool_call_id"], "call_abc")

    def test_evicted_tool_output_remains_a_valid_result_pair(self) -> None:
        tool_calls = [{
            "id": "call_evicted",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"q":"cats"}'},
        }]
        notice = (
            "Stored tool output removed to manage storage. The original tool request "
            "is retained. Re-run only if the operation is safe and current output is needed."
        )
        rows = [
            _ir("a", "assistant", "", output=json.dumps({"tool_calls": tool_calls})),
            _ir("t", "tool", notice, tool_name="web_search", tool_call_id="call_evicted"),
        ]

        out = interactions_to_openai_messages(rows)

        self.assertEqual(out[0]["tool_calls"], tool_calls)
        self.assertEqual(out[1], {
            "role": "tool", "content": notice, "tool_call_id": "call_evicted",
        })


class LargeSessionAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_compacted_prefix_is_never_materialized(self) -> None:
        tail = [_ir("tail-user", "user", "latest request")]

        class FakeDB:
            full_fetches = 0
            suffix_offsets = []

            async def count_interactions(self, user_id, session_id):
                return 1_000_000

            async def get_session_segments(self, user_id, session_id):
                return [{
                    "seq": 0,
                    "start_index": 0,
                    "end_index": 999_999,
                    "summary": "bounded earlier context",
                    "token_estimate": 3,
                    "topic": "Earlier work",
                    "tier": 1,
                }]

            async def fetch_interactions_from_offset(self, user_id, session_id, offset):
                self.suffix_offsets.append(offset)
                return tail

            async def fetch_interactions(self, user_id, session_id):
                self.full_fetches += 1
                raise AssertionError("raw compacted prefix must not be fetched")

        db = FakeDB()
        out = await build_openai_history_from_session(db, "user", "session")

        self.assertEqual(db.full_fetches, 0)
        self.assertEqual(db.suffix_offsets, [999_999])
        self.assertEqual(out[-1], {"role": "user", "content": "latest request"})
        self.assertIn("bounded earlier context", out[0]["content"])

    async def test_compaction_check_reads_only_uncovered_suffix(self) -> None:
        tail = [_ir("tail-user", "user", "small hot tail")]

        class FakeDB:
            full_fetches = 0
            suffix_offsets = []

            async def count_interactions(self, user_id, session_id):
                return 1_000_000

            async def get_session_segments(self, user_id, session_id):
                return [{
                    "seq": 0,
                    "start_index": 0,
                    "end_index": 999_999,
                    "summary": "earlier context",
                    "token_estimate": 3,
                    "topic": "Earlier work",
                    "tier": 1,
                }]

            async def fetch_interactions_from_offset(self, user_id, session_id, offset):
                self.suffix_offsets.append(offset)
                return tail

            async def fetch_interactions(self, user_id, session_id):
                self.full_fetches += 1
                raise AssertionError("compaction check loaded summarized prefix")

        db = FakeDB()
        changed = await maybe_compact(db, "user", "session", {
            "enabled": True,
            "compaction_enabled": True,
            "token_limit": 1_000_000,
            "compact_threshold": 0.85,
            "tail_fraction": 0.30,
        })

        self.assertIsNone(changed)
        self.assertEqual(db.full_fetches, 0)
        self.assertEqual(db.suffix_offsets, [999_999])


if __name__ == "__main__":
    unittest.main()
