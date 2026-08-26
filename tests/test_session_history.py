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
from app.agent.context_control import estimate_tokens
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


class OffTaskToolHidingTests(unittest.TestCase):
    """Closed-task tool results degrade to a placeholder in the model payload;
    current-task results stay in full. The DB keeps full output either way."""

    def setUp(self) -> None:
        import app.agent.session_history as sh
        self._sh = sh
        self._prev = sh._HIDE_OFF_TASK_TOOL_OUTPUTS
        sh._HIDE_OFF_TASK_TOOL_OUTPUTS = True

    def tearDown(self) -> None:
        self._sh._HIDE_OFF_TASK_TOOL_OUTPUTS = self._prev

    @staticmethod
    def _asst(iid: str, tool_id: str, arguments: str = '{"q":"x"}') -> InteractionRecord:
        calls = [{
            "id": tool_id, "type": "function",
            "function": {"name": "web_search", "arguments": arguments},
        }]
        return _ir(iid, "assistant", "", output=json.dumps({"tool_calls": calls}))

    def test_old_task_tool_output_replaced_with_placeholder(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "login source: " + "x" * 500,
                tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "now write a poem about the sea"),
            self._asst("a2", "call_new"),
            _ir("t2", "tool", "poem draft", tool_name="generate",
                tool_call_id="call_new"),
        ]
        out = interactions_to_openai_messages(rows)

        old = next(m for m in out if m.get("tool_call_id") == "call_old")
        new = next(m for m in out if m.get("tool_call_id") == "call_new")
        old_call = next(tc for m in out for tc in m.get("tool_calls", [])
                        if tc.get("id") == "call_old")
        old_args = json.loads(old_call["function"]["arguments"])
        self.assertIn("[tool result hidden — completed in an earlier task]", old["content"])
        self.assertIn("read_source", old["content"])
        self.assertIn("Result interaction: t1", old["content"])
        self.assertNotIn("login source", old["content"])
        self.assertEqual(old_args["_context_reduced"], True)
        self.assertEqual(old_args["interaction_id"], "a1")
        self.assertEqual(old_args["tool_call_id"], "call_old")
        self.assertEqual(new["content"], "poem draft")

    def test_excluded_live_user_still_closes_the_previous_task(self) -> None:
        huge_arguments = json.dumps({"document": "secret old input " + "z" * 5000})
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old", huge_arguments),
            _ir("t1", "tool", "old source payload",
                tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated poem about the sea"),
        ]

        out = interactions_to_openai_messages(
            rows, exclude_interaction_ids={"u2"},
        )

        call = out[1]["tool_calls"][0]
        reduced = json.loads(call["function"]["arguments"])
        self.assertTrue(reduced["_context_reduced"])
        self.assertEqual(reduced["original_argument_chars"], len(huge_arguments))
        self.assertNotIn("secret old input", call["function"]["arguments"])
        self.assertIn("Result interaction: t1", out[2]["content"])

    def test_same_task_tool_outputs_stay_in_full(self) -> None:
        # "yes go ahead" is a reaction → same task; nothing may be hidden.
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_1"),
            _ir("t1", "tool", "first result payload",
                tool_name="read_source", tool_call_id="call_1"),
            _ir("u2", "user", "yes go ahead"),
            self._asst("a2", "call_2"),
            _ir("t2", "tool", "second result payload",
                tool_name="read_source", tool_call_id="call_2"),
        ]
        out = interactions_to_openai_messages(rows)

        contents = [m["content"] for m in out if m.get("role") == "tool"]
        self.assertEqual(contents, ["first result payload", "second result payload"])
        calls = [tc for m in out for tc in m.get("tool_calls", [])]
        self.assertEqual(
            [tc["function"]["arguments"] for tc in calls],
            ['{"q":"x"}', '{"q":"x"}'],
        )

    def test_recent_runs_bounds_evidence_inside_one_long_task(self) -> None:
        rows = []
        prompts = [
            "fix the login bug", "yes go ahead", "yes go ahead",
            "yes go ahead",
        ]
        for index, prompt in enumerate(prompts, 1):
            rows.extend([
                _ir(f"u{index}", "user", prompt),
                self._asst(f"a{index}", f"call_{index}"),
                _ir(f"t{index}", "tool", f"full result {index}",
                    tool_name="read_source", tool_call_id=f"call_{index}"),
            ])

        out = interactions_to_openai_messages(rows, evidence_settings={
            "tool_evidence_policy": "recent_runs",
            "full_evidence_runs": 2,
        })

        contents = {
            m["tool_call_id"]: m["content"] for m in out if m.get("role") == "tool"
        }
        self.assertIn("full-evidence run window", contents["call_1"])
        self.assertIn("full-evidence run window", contents["call_2"])
        self.assertEqual(contents["call_3"], "full result 3")
        self.assertEqual(contents["call_4"], "full result 4")

    def test_budget_keeps_current_run_even_when_it_exceeds_budget(self) -> None:
        rows = []
        for index, prompt in enumerate(
                ["fix the login bug", "yes go ahead"], 1):
            rows.extend([
                _ir(f"u{index}", "user", prompt),
                self._asst(f"a{index}", f"call_{index}"),
                _ir(f"t{index}", "tool", str(index) * 5000,
                    tool_name="read_source", tool_call_id=f"call_{index}"),
            ])

        out = interactions_to_openai_messages(rows, evidence_settings={
            "tool_evidence_policy": "token_budget",
            "full_evidence_token_budget": 1000,
        })
        contents = {
            m["tool_call_id"]: m["content"] for m in out if m.get("role") == "tool"
        }
        self.assertIn("full-evidence run window", contents["call_1"])
        self.assertEqual(contents["call_2"], "2" * 5000)

    def test_all_policy_keeps_closed_task_evidence(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "login source", tool_name="read_source",
                tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated poem about the sea"),
        ]
        out = interactions_to_openai_messages(
            rows, evidence_settings={"tool_evidence_policy": "all"})
        old = next(m for m in out if m.get("tool_call_id") == "call_old")
        self.assertEqual(old["content"], "login source")

    def test_context_estimate_counts_the_reduced_provider_payload(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old", json.dumps({"source": "x" * 8000})),
            _ir("t1", "tool", "y" * 8000, tool_name="read_source",
                tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated poem about the sea"),
        ]
        full = interactions_to_openai_messages(
            rows, evidence_settings={"tool_evidence_policy": "all"})
        reduced = interactions_to_openai_messages(
            rows, evidence_settings={"tool_evidence_policy": "current_task"})

        # Context Control's composer/pipeline gauge receives this exact message
        # list, so its estimate reflects compacted arguments and results.
        self.assertLess(estimate_tokens(reduced), estimate_tokens(full) // 10)

    def test_anaphoric_followup_keeps_prior_tool_output_in_full(self) -> None:
        rows = [
            _ir("u1", "user", "summarize the project README"),
            self._asst("a1", "call_1"),
            _ir("t1", "tool", "project setup details",
                tool_name="read_source", tool_call_id="call_1"),
            _ir("u2", "user", "What does that imply for local setup?"),
        ]

        out = interactions_to_openai_messages(rows)

        tool_result = next(m for m in out if m.get("tool_call_id") == "call_1")
        self.assertEqual(tool_result["content"], "project setup details")
        self.assertEqual(
            out[1]["tool_calls"][0]["function"]["arguments"],
            '{"q":"x"}',
        )

    def test_synthetic_user_rows_never_open_a_boundary(self) -> None:
        # An orchestration wake-up mid-task must not split the task.
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_1"),
            _ir("t1", "tool", "first result payload",
                tool_name="read_source", tool_call_id="call_1"),
            _ir("u2", "user", "[orchestration event] helper finished",
                tool_call_id=None),
            self._asst("a2", "call_2"),
            _ir("t2", "tool", "second result payload",
                tool_name="read_source", tool_call_id="call_2"),
        ]
        out = interactions_to_openai_messages(rows)

        contents = [m["content"] for m in out if m.get("role") == "tool"]
        self.assertEqual(contents, ["first result payload", "second result payload"])

    def test_disabled_keeps_everything_in_full(self) -> None:
        self._sh._HIDE_OFF_TASK_TOOL_OUTPUTS = False
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "login source " + "y" * 300,
                tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "now write a poem about the sea"),
            self._asst("a2", "call_new"),
            _ir("t2", "tool", "poem draft", tool_name="generate",
                tool_call_id="call_new"),
        ]
        out = interactions_to_openai_messages(rows)

        old = next(m for m in out if m.get("tool_call_id") == "call_old")
        self.assertIn("login source", old["content"])
        old_call = next(tc for m in out for tc in m.get("tool_calls", [])
                        if tc.get("id") == "call_old")
        self.assertEqual(old_call["function"]["arguments"], '{"q":"x"}')


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
            "compact_target_tokens": 850_000,
            "verbatim_tail_tokens": 300_000,
        })

        self.assertIsNone(changed)
        self.assertEqual(db.full_fetches, 0)
        self.assertEqual(db.suffix_offsets, [999_999])


if __name__ == "__main__":
    unittest.main()
