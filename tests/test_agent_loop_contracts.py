"""Tests for the run-contract layer (app/agent/contracts.py) and the Manager
(per-trigger contracts in app/agent/manager.py).

Covers the pure, synchronous parts: contract parsing, tool classification,
approval detection, the mechanical rules, contract state transitions, and the
Manager's config resolution / verdict parsing. The DB-touching pieces
(manager window collection, self-note injection) are exercised indirectly
through the loop and are out of scope here.
"""

import json
import unittest

from app.agent.contracts import (
    active_rules,
    check_plan_before_edit,
    check_tool_cap,
    check_verify_after_edit,
    contract_prompt_block,
    error_strike,
    is_commit_tool,
    is_edit_tool,
    is_verify_tool,
    mark_edit_executed,
    mark_tool_outcome,
    mark_tool_scheduled,
    mark_verify_executed,
    mechanical_check,
    new_contract_state,
    parse_contract,
    user_approved_plan,
)
from app.agent.manager import (
    _format_manager_prompt,
    manager_feedback_message,
    _parse_manager_verdict,
    run_manager_check,
    resolve_manager_config,
    trigger_enabled,
)
from app.agent.loop_executor import LoopConfig


class ContractParseTests(unittest.TestCase):
    def test_missing_config_is_empty(self):
        self.assertEqual(parse_contract(None), {})
        self.assertEqual(parse_contract({}), {})
        self.assertEqual(parse_contract({"metadata": "{}"}), {})

    def test_json_object_config(self):
        agent = {"metadata": {"contract": {
            "require_plan_before_edit": True,
            "require_verify_after_edit": True,
            "max_tool_errors": 3,
            "max_tool_calls_per_turn": 12,
        }}}
        contract = parse_contract(agent)
        self.assertTrue(contract["require_plan_before_edit"])
        self.assertTrue(contract["require_verify_after_edit"])
        self.assertEqual(contract["max_tool_errors"], 3)
        self.assertEqual(contract["max_tool_calls_per_turn"], 12)

    def test_json_string_config(self):
        agent = {"metadata": json.dumps({"contract": json.dumps({
            "require_plan_before_edit": True,
        })})}
        contract = parse_contract(agent)
        self.assertTrue(contract["require_plan_before_edit"])

    def test_broken_config_fails_open(self):
        self.assertEqual(parse_contract({"metadata": {"contract": "not json"}}), {})
        self.assertEqual(parse_contract({"metadata": {"contract": 42}}), {})
        self.assertEqual(parse_contract({"metadata": "not json"}), {})

    def test_active_rules_and_prompt_block(self):
        contract = {"require_plan_before_edit": True, "require_verify_after_edit": False,
                    "max_tool_errors": 0, "max_tool_calls_per_turn": 5}
        self.assertEqual(active_rules(contract), ["require_plan_before_edit", "max_tool_calls_per_turn"])
        block = contract_prompt_block(contract)
        self.assertIn("FIRST file edit", block)
        self.assertNotIn("verify it before moving on", block)  # inactive rule absent
        self.assertEqual(contract_prompt_block({}), "")


class ToolClassificationTests(unittest.TestCase):
    def test_edit_and_commit_tools(self):
        self.assertTrue(is_edit_tool("write_source"))
        self.assertTrue(is_edit_tool("edit_source"))
        self.assertTrue(is_edit_tool("patch_source"))
        self.assertTrue(is_edit_tool("delete_source"))
        self.assertTrue(is_edit_tool("resolve_conflict"))
        self.assertFalse(is_edit_tool("read_source"))
        self.assertFalse(is_edit_tool("run_command"))
        self.assertTrue(is_commit_tool("commit_and_push"))
        self.assertFalse(is_commit_tool("git_tool"))

    def test_verify_tools(self):
        self.assertTrue(is_verify_tool("read_source"))
        self.assertTrue(is_verify_tool("search_source"))
        self.assertFalse(is_verify_tool("write_source"))

    def test_verify_tools_respect_arguments(self):
        self.assertTrue(is_verify_tool("git_tool", {"operation": "diff"}))
        self.assertFalse(is_verify_tool("git_tool", {"operation": "push"}))
        self.assertTrue(is_verify_tool("run_command", {"command": "git status"}))
        self.assertFalse(is_verify_tool("run_command", {"command": "rm -rf /"}))
        self.assertTrue(is_verify_tool("http_request", {"method": "GET"}))
        self.assertFalse(is_verify_tool("http_request", {"method": "POST"}))
        self.assertTrue(is_verify_tool("browser_action", {"action": "get_text"}))
        self.assertFalse(is_verify_tool("browser_action", {"action": "click"}))


class ApprovalDetectionTests(unittest.TestCase):
    def test_original_request_is_never_approval(self):
        messages = [{"role": "user", "content": "ok, build the feature"}]
        self.assertFalse(user_approved_plan(messages))

    def test_new_approval_after_request_counts(self):
        messages = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan."},
            {"role": "user", "content": "go ahead"},
        ]
        self.assertTrue(user_approved_plan(messages))

    def test_latest_message_must_be_approval(self):
        messages = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan."},
            {"role": "user", "content": "actually change the approach"},
        ]
        self.assertFalse(user_approved_plan(messages))

    def test_word_boundary_matching(self):
        messages = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan."},
            {"role": "user", "content": "looking at the code"},
        ]
        self.assertFalse(user_approved_plan(messages))


class MechanicalCheckTests(unittest.TestCase):
    def test_plan_before_edit_blocks_auto_without_approval(self):
        contract = {"require_plan_before_edit": True}
        state = new_contract_state()
        msg = check_plan_before_edit(contract, state, "write_source", "auto")
        self.assertIsNotNone(msg)
        self.assertIn("plan", msg.lower())
        # Plan/Ask modes pass — the mode gate owns confirmation there.
        self.assertIsNone(check_plan_before_edit(contract, state, "write_source", "plan"))
        self.assertIsNone(check_plan_before_edit(contract, state, "write_source", "ask"))
        # Non-edit tools never trip it.
        self.assertIsNone(check_plan_before_edit(contract, state, "read_source", "auto"))

    def test_plan_before_edit_passes_after_edit_executed(self):
        contract = {"require_plan_before_edit": True}
        state = new_contract_state()
        mark_edit_executed(state)
        self.assertIsNone(check_plan_before_edit(contract, state, "write_source", "auto"))

    def test_verify_after_edit_blocks_until_verify(self):
        contract = {"require_verify_after_edit": True}
        state = new_contract_state()
        mark_edit_executed(state)  # edit ran → awaiting verify
        self.assertIsNotNone(check_verify_after_edit(contract, state, "write_source", {}))
        self.assertIsNone(check_verify_after_edit(contract, state, "read_source", {}))
        mark_verify_executed(state)
        self.assertIsNone(check_verify_after_edit(contract, state, "write_source", {}))

    def test_tool_cap(self):
        contract = {"max_tool_calls_per_turn": 2}
        state = new_contract_state()
        mark_tool_scheduled(state)
        mark_tool_scheduled(state)
        self.assertIsNotNone(check_tool_cap(contract, state))
        self.assertIsNone(check_tool_cap({"max_tool_calls_per_turn": 0}, state))

    def test_error_strike_fires_once(self):
        contract = {"max_tool_errors": 2}
        state = new_contract_state()
        mark_tool_outcome(state, False)
        mark_tool_outcome(state, False)
        msg1 = error_strike(contract, state)
        self.assertIsNotNone(msg1)
        self.assertTrue(state["error_strike_sent"])
        self.assertIsNone(error_strike(contract, state))  # once only

    def test_mechanical_check_orders_rules(self):
        contract = {"require_plan_before_edit": True, "require_verify_after_edit": True}
        state = new_contract_state()
        # First edit in auto: plan rule fires first.
        msg = mechanical_check(contract, state, tool_name="write_source",
                               tool_args={}, execution_mode="auto")
        self.assertIsNotNone(msg)
        self.assertIn("plan", msg.lower())

    def test_no_contract_never_blocks(self):
        state = new_contract_state()
        self.assertIsNone(mechanical_check({}, state, tool_name="write_source",
                                           tool_args={}, execution_mode="auto"))


class ManagerConfigTests(unittest.TestCase):
    def test_defaults_all_off(self):
        cfg = resolve_manager_config(None)
        self.assertEqual(cfg["plan_gate"], "off")
        self.assertEqual(cfg["edit_gate"], "off")
        self.assertEqual(cfg["commit_gate"], "off")
        self.assertEqual(cfg["watchdog"], "off")
        self.assertEqual(cfg["max_checks"], 9)
        self.assertEqual(cfg["max_checks_by_kind"], {
            "plan_gate": 1, "edit_gate": 4, "watchdog": 3, "commit_gate": 1,
        })
        self.assertEqual(cfg["max_blocks"], 3)
        self.assertFalse(trigger_enabled(cfg, "plan_gate"))
        self.assertFalse(trigger_enabled(cfg, "watchdog"))

    def test_blocking_and_async_gates(self):
        agent = {"metadata": {"manager": {
            "plan_gate": "blocking", "edit_gate": "async",
            "commit_gate": "blocking", "watchdog": {
                "every_n_turns": 4, "on_errors": 3, "on_stall": True,
                "cooldown_turns": 2, "error_window": 8,
            },
            "max_checks": 7,
            "max_checks_by_kind": {
                "plan_gate": 2, "edit_gate": 6, "watchdog": 5, "commit_gate": 2,
            },
            "max_blocks": 4,
        }}}
        cfg = resolve_manager_config(agent)
        self.assertEqual(cfg["plan_gate"], "blocking")
        self.assertEqual(cfg["edit_gate"], "async")
        self.assertEqual(cfg["commit_gate"], "blocking")
        self.assertEqual(cfg["watchdog"], {
            "every_n_turns": 4, "on_errors": 3, "on_stall": True,
            "cooldown_turns": 2, "error_window": 8,
        })
        self.assertEqual(cfg["max_checks"], 7)
        self.assertEqual(cfg["max_checks_by_kind"], {
            "plan_gate": 2, "edit_gate": 6, "watchdog": 5, "commit_gate": 2,
        })
        self.assertEqual(cfg["max_blocks"], 4)
        self.assertTrue(trigger_enabled(cfg, "plan_gate"))
        self.assertTrue(trigger_enabled(cfg, "edit_gate"))
        self.assertTrue(trigger_enabled(cfg, "commit_gate"))
        self.assertTrue(trigger_enabled(cfg, "watchdog"))

    def test_broken_config_fails_open(self):
        cfg = resolve_manager_config({"metadata": {"manager": "not json"}})
        self.assertEqual(cfg["plan_gate"], "off")

    def test_verdict_parse_tolerant(self):
        v = _parse_manager_verdict(
            '```json\n{"verdict": "block", "reason": "off plan", "feedback": "fix it"}\n```',
            "edit_gate")
        self.assertIsNotNone(v)
        self.assertEqual(v["verdict"], "block")
        self.assertEqual(v["feedback"], "fix it")

    def test_verdict_parse_rejects_wrong_kind_and_garbage(self):
        self.assertIsNone(_parse_manager_verdict('{"verdict": "approve"}', "watchdog"))
        self.assertIsNone(_parse_manager_verdict("not json at all", "edit_gate"))
        self.assertIsNone(_parse_manager_verdict("", "edit_gate"))

    def test_verdict_contract_requires_reason_and_actionable_guidance(self):
        self.assertIsNone(_parse_manager_verdict(
            '{"verdict": "approve", "feedback": "unused"}', "edit_gate"))
        self.assertIsNone(_parse_manager_verdict(
            '{"verdict": "block", "reason": "off plan"}', "edit_gate"))
        self.assertIsNone(_parse_manager_verdict(
            '{"verdict": "off_track", "reason": "looping"}', "watchdog"))
        self.assertIsNotNone(_parse_manager_verdict(
            '{"verdict": "approve", "reason": "scope matches"}', "commit_gate"))
        self.assertIsNotNone(_parse_manager_verdict(
            '{"verdict": "stuck", "reason": "looping", '
            '"suggestion": "inspect the caller"}', "watchdog"))

    def test_bad_custom_prompt_falls_back_to_matching_kind(self):
        formatted = _format_manager_prompt(
            "commit_gate", "bad {unknown_placeholder}", "ship it",
            ["Assistant: verified"], '{"changed_paths":["app/a.py"]}',
        )
        self.assertIn("reviewing a commit", formatted)
        self.assertIn("STRUCTURED COMMIT EVIDENCE", formatted)
        self.assertIn("do not claim that you independently", formatted)
        self.assertNotIn("reviewing an agent's PLAN", formatted)

    def test_verdict_parse_buried_in_prose(self):
        v = _parse_manager_verdict(
            'Sure, here: {"verdict": "on_track", "reason": "progressing", "suggestion": ""}',
            "watchdog")
        self.assertIsNotNone(v)
        self.assertEqual(v["verdict"], "on_track")

    def test_on_stall_only_enables_watchdog(self):
        cfg = resolve_manager_config({"metadata": {"manager": {
            "watchdog": {"on_stall": True, "every_n_turns": 0, "on_errors": 0}
        }}})
        self.assertTrue(trigger_enabled(cfg, "watchdog"))

    def test_actionable_feedback_formats_for_active_loop(self):
        note = manager_feedback_message({
            "kind": "watchdog", "verdict": "off_track",
            "reason": "Repeated the same search.",
            "suggestion": "Inspect the caller instead.",
        })
        self.assertIn("MANAGER WATCHDOG", note)
        self.assertIn("Inspect the caller", note)
        self.assertEqual(manager_feedback_message({
            "kind": "watchdog", "verdict": "on_track"
        }), "")

    def test_manager_loop_node_is_independent_from_contract_node(self):
        cfg = LoopConfig([
            {"node": "contract_chk", "enabled": False},
            {"node": "manager_chk", "enabled": True},
        ])
        self.assertFalse(cfg.is_enabled("contract_chk"))
        self.assertTrue(cfg.is_enabled("manager_chk"))


class ManagerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_check_index_enforces_configured_cap(self):
        from unittest.mock import patch

        with patch("app.abilities.app_function_enabled", return_value=True):
            verdict = await run_manager_check(
                "watchdog",
                user_id="user", session_id="session", final_asst_id="assistant",
                db=object(), max_checks=2, check_index=3,
            )
        self.assertIsNone(verdict)

    async def test_explicit_kind_check_index_enforces_kind_cap(self):
        from unittest.mock import patch

        with patch("app.abilities.app_function_enabled", return_value=True):
            verdict = await run_manager_check(
                "watchdog",
                user_id="user", session_id="session", final_asst_id="assistant",
                db=object(), max_checks=9, check_index=1,
                kind_max_checks=3, kind_check_index=4,
            )
        self.assertIsNone(verdict)

    async def test_retries_share_one_total_timeout_budget(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch

        calls = 0

        async def slow_completion(**_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=slow_completion,
        )))
        loop = asyncio.get_running_loop()
        started = loop.time()
        with (
            patch("app.abilities.app_function_enabled", return_value=True),
            patch("app.agent.manager._LLM_TIMEOUT", 0.03),
            patch("app.agent.manager._LLM_RETRY_BACKOFF_S", 0),
            patch("app.agent.manager._collect_manager_span",
                  return_value=(["User: test"], "test")),
            patch("app.agent.manager._resolve_llm",
                  return_value=("model", "provider", client)),
        ):
            verdict = await run_manager_check(
                "watchdog", user_id="user", session_id="session",
                final_asst_id="assistant", db=object(),
            )
        elapsed = loop.time() - started
        self.assertIsNone(verdict)
        # The one-call assertion proves the first timeout consumed the shared
        # deadline. Leave room for slow Windows CI/import bookkeeping outside
        # the patched 30 ms model call.
        self.assertLess(elapsed, 0.5)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
