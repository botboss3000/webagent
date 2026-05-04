"""Unit tests for DB → OpenAI message history mapping."""

import json
import unittest
from datetime import datetime, timezone

from app.agent.session_history import TOOL_MARKER, interactions_to_openai_messages
from app.models.schemas import InteractionRecord


def _ir(
    iid: str,
    role: str,
    content: str,
    *,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
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
        input=None,
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


if __name__ == "__main__":
    unittest.main()
