import asyncio
from pathlib import Path

from app.agent.execution_modes import (
    accumulated_contract,
    execution_modes_for_agent,
    normalize_execution_modes,
    normalize_mode_id,
    resolve_execution_mode,
)
from app.agent.output_closer import (
    _collect_run_mode_context,
    _read_only_execution_violation,
    _resolve_audit_config,
)
from app.db.local import LocalBackend


def test_defaults_include_prompt_policy_and_plan_contract():
    modes = execution_modes_for_agent(None)
    assert [mode["id"] for mode in modes] == ["ask", "plan", "auto"]
    assert modes[0]["permission_policy"] == "read_only"
    assert len(modes[0]["contract"]["checklist"]) == 3
    assert any("No requested implementation" in item["label"]
               for item in modes[0]["contract"]["checklist"])
    assert modes[1]["prompt"].startswith("You are in PLAN mode")
    assert modes[1]["contract"]["require_plan_document"] is True
    assert len(modes[1]["contract"]["checklist"]) >= 2
    assert any("No requested implementation" in item["label"]
               for item in modes[1]["contract"]["checklist"])
    assert modes[2]["permission_policy"] == "write"


def test_custom_modes_are_added_without_removing_defaults():
    modes = normalize_execution_modes([
        {"id": "plan", "label": "Architect", "permission_policy": "read_only"},
        {
            "id": "research", "label": "Research", "permission_policy": "read_only",
            "prompt": "Investigate and cite evidence.", "checklist": ["Cite the evidence."],
        },
    ])
    assert [mode["id"] for mode in modes] == ["ask", "plan", "auto", "research"]
    assert modes[1]["label"] == "Architect"
    assert modes[3]["prompt"] == "Investigate and cite evidence."
    assert modes[3]["contract"]["checklist"][0]["label"] == "Cite the evidence."
    assert all(mode["id"] in {m["id"] for m in modes} for mode in modes[:3])


def test_invalid_policy_fails_safe_and_mode_ids_support_legacy_aliases():
    assert normalize_mode_id("read") == "plan"
    assert normalize_mode_id("write") == "ask"
    modes = normalize_execution_modes([{"id": "review", "permission_policy": "root"}])
    assert modes[-1]["permission_policy"] == "read_only"
    assert resolve_execution_mode({"metadata": {"execution_modes": modes}}, "missing")["id"] == "ask"


def test_mode_completion_contract_layers_over_agent_checklist():
    agent = {
        "metadata": {
            "audit_checklist": {"checklist": ["General requirement"], "max_rounds": 2},
            "execution_modes": [{
                "id": "research", "label": "Research", "permission_policy": "write",
                "checklist": ["Cite the evidence"], "max_rounds": 1, "send_back": True,
            }],
        }
    }
    checklist, rounds, send_back = _resolve_audit_config(agent, "research")
    assert checklist == ["General requirement", "Cite the evidence"]
    assert rounds == 1
    assert send_back is True


def test_plan_and_ask_contracts_carry_forward_when_session_switches_to_auto():
    contract = accumulated_contract(None, ["ask", "plan", "auto"], "auto")
    assert contract["mode_ids"] == ["ask", "plan", "auto"]
    labels = [item["label"] for item in contract["checklist"]]
    assert any("concrete proposal" in label for label in labels)
    assert any("persistent Plan Overview" in label for label in labels)
    assert any("tracked against the active plan" in label for label in labels)
    assert not any("No requested implementation" in label for label in labels)

    checklist, _, _ = _resolve_audit_config(
        None, "auto", ["ask", "plan", "auto"],
    )
    assert checklist == labels


def test_read_only_mode_audit_is_proposal_or_plan_not_prior_execution():
    agent = {"metadata": {
        "audit_checklist": ["The requested implementation was completed."],
    }}

    ask_contract = accumulated_contract(agent, ["auto", "ask"], "ask")
    assert ask_contract["mode_ids"] == ["ask"]
    ask_labels = [item["label"] for item in ask_contract["checklist"]]
    assert any("concrete proposal" in item for item in ask_labels)
    assert any("No requested implementation" in item for item in ask_labels)
    assert not any("requested work is complete" in item for item in ask_labels)

    checklist, rounds, send_back = _resolve_audit_config(
        agent, "ask", ["auto", "ask"],
    )
    assert checklist == ask_labels
    assert "The requested implementation was completed." not in checklist
    assert rounds == 0
    assert send_back is False

    plan_checklist, _, _ = _resolve_audit_config(
        agent, "plan", ["ask", "plan"],
    )
    assert any("persistent Plan Overview" in item for item in plan_checklist)
    assert any("Plan mode ends with an execution-ready plan" in item
               for item in plan_checklist)
    assert "The requested implementation was completed." not in plan_checklist


def test_read_only_execution_failure_is_terminal_not_sent_back_to_execute_more():
    assert _read_only_execution_violation([
        "No requested implementation or other mutating action was executed; Plan mode ends with an execution-ready plan."
    ]) is True
    assert _read_only_execution_violation([
        "The persistent Plan Checklist contains concrete steps."
    ]) is False


def test_run_scoped_contract_handles_both_mode_switch_directions():
    agent = {"metadata": {
        "audit_checklist": ["Generic implementation requirement"],
    }}

    # Ask/Plan preparation carries forward into a final Auto execution, while
    # the mode-local no-execution clauses do not.
    to_auto, _, _ = _resolve_audit_config(
        agent, "auto", ["ask", "plan", "auto"],
        run_scoped=True, executed_modes=["auto"],
    )
    assert "Generic implementation requirement" in to_auto
    assert any("concrete proposal" in item for item in to_auto)
    assert any("persistent Plan Overview" in item for item in to_auto)
    assert not any("No requested implementation" in item for item in to_auto)

    # Auto work remains auditable after a prospective switch to Ask, and Ask's
    # no-execution clause applies only while Ask was active.
    to_ask, _, _ = _resolve_audit_config(
        agent, "ask", ["auto", "ask"],
        run_scoped=True, executed_modes=["auto"],
    )
    assert "Generic implementation requirement" in to_ask
    assert any("requested work is complete" in item for item in to_ask)
    assert any("while Ask mode was active" in item for item in to_ask)

    # Merely starting in Auto without mutating anything does not make a later
    # Ask proposal satisfy an implementation checklist.
    unused_auto, _, _ = _resolve_audit_config(
        agent, "ask", ["auto", "ask"],
        run_scoped=True, executed_modes=[],
    )
    assert "Generic implementation requirement" not in unused_auto
    assert not any("requested work is complete" in item for item in unused_auto)


def test_run_mode_context_reconstructs_footer_switch_and_mutation_mode(tmp_path):
    db = LocalBackend(str(tmp_path / "mode-context.db"), seed=False)
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, metadata, status, created_at, updated_at) "
            "VALUES ('session-1', 'user-1', '{}', 'active', '', '')"
        )
        conn.commit()
    finally:
        conn.close()
    parent = asyncio.run(db.insert_interaction(
        "user-1", "session-1", role="user", content="change it",
        session_seq=1,
    ))
    asyncio.run(db.insert_interaction(
        "user-1", "session-1", role="tool", content="ok",
        tool_name="patch_source",
        metadata='{"success": true, "execution_mode": "auto", "mutating": true}',
        session_seq=2,
    ))
    asyncio.run(db.add_execution_mode_notice(
        "user-1", "session-1", "ask", initiator="user",
    ))
    final = asyncio.run(db.insert_interaction(
        "user-1", "session-1", role="assistant", content="proposal",
        session_seq=4,
    ))

    context = _collect_run_mode_context(
        db, "session-1", parent, final, "auto", "ask",
    )
    assert context["timeline"] == ["auto", "ask"]
    assert context["final_mode"] == "ask"
    assert context["executed_modes"] == ["auto"]
    assert context["mutations"] == {"auto": 1}
    assert "auto (1 mutating action(s))" in context["summary"]


def test_manager_closer_controls_sources_rounds_and_plan_requirement():
    agent = {"metadata": {
        "audit_checklist": ["Agent-wide item"],
        "manager": {
            "enabled": True,
            "closer": {
                "audit_agent_checklist": False,
                "audit_mode_contract": True,
                "require_plan_document": True,
                "require_manager_clear": True,
                "max_rounds": 2,
                "send_back": False,
            },
        },
    }}
    checklist, rounds, send_back = _resolve_audit_config(agent, "plan")
    assert "Agent-wide item" not in checklist
    assert any("persistent plan document" in item for item in checklist)
    assert any("actionable Manager feedback" in item for item in checklist)
    assert rounds == 2
    assert send_back is False


def test_session_mode_history_is_persisted_in_first_use_order(tmp_path):
    db = LocalBackend(str(tmp_path / "modes.db"), seed=False)
    conn = db._get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (id, user_id, metadata, status, created_at, updated_at) "
            "VALUES ('session-1', 'user-1', '{}', 'active', '', '')"
        )
        conn.commit()
    finally:
        conn.close()
    asyncio.run(db.set_session_execution_mode("session-1", "ask"))
    asyncio.run(db.set_session_execution_mode("session-1", "plan"))
    asyncio.run(db.set_session_execution_mode("session-1", "auto"))
    asyncio.run(db.set_session_execution_mode("session-1", "plan"))
    assert asyncio.run(db.get_session_execution_mode_history("session-1")) == [
        "ask", "plan", "auto",
    ]


def test_agents_config_and_chat_footer_use_dynamic_modes():
    root = Path(__file__).resolve().parents[1]
    config = (root / "ui/main-panel/agents/js/tab-config.js").read_text(encoding="utf-8")
    chat = (root / "ui/chat/js/chat-ui.js").read_text(encoding="utf-8")
    assert "Prompt injection" in config
    assert "Completion contract" in config
    assert "separate JSON object" in config
    assert "Require persistent Plan document + checklist" in config
    assert "Add mode" in config
    assert "_activeAgent()?.execution_modes" in chat
    assert "cycle = modes.map(mode => mode.id)" in chat
