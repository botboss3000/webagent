import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.agent.manager import resolve_manager_config
from app.agent.manager_config import (
    manager_loop_for_agent,
    merge_manager_loop_update,
    resolve_manager_loop,
)
from app.api.agents import UpdateAgentRequest, _safe_agent, update_agent


def test_defaults_are_complete_and_opt_in():
    cfg = manager_loop_for_agent(None)
    assert cfg["enabled"] is False
    assert cfg["triggers"]["plan_gate"]["policy"] == "off"
    assert cfg["watchdog"]["enabled"] is False
    assert cfg["starter"]["parallel"] is True
    assert cfg["starter"]["first_response"] is True
    assert cfg["closer"]["audit_mode_contract"] is True


def test_legacy_flat_manager_remains_enabled_and_resolved():
    agent = {"metadata": {"manager": {
        "plan_gate": "blocking",
        "edit_gate": "async",
        "max_checks": 7,
        "watchdog": {"every_n_turns": 4, "on_stall": True},
    }, "manager_plan_gate_prompt": "Legacy plan opinion"}}
    canonical = manager_loop_for_agent(agent)
    assert canonical["enabled"] is True
    assert canonical["triggers"]["plan_gate"]["policy"] == "blocking"
    assert canonical["triggers"]["plan_gate"]["prompt"] == "Legacy plan opinion"
    assert canonical["budgets"]["max_checks"] == 7
    runtime = resolve_manager_config(agent)
    assert runtime["plan_gate"] == "blocking"
    assert runtime["watchdog"]["every_n_turns"] == 4


def test_partial_update_deep_merges_and_validates():
    current = {"metadata": {"manager": {
        "enabled": True,
        "triggers": {"plan_gate": {"policy": "blocking", "modes": ["plan"]}},
    }}}
    updated = merge_manager_loop_update(current, {
        "budgets": {"max_checks": 12},
        "watchdog": {"enabled": True, "action": "replan", "every_n_turns": 5},
    })
    assert updated["triggers"]["plan_gate"]["policy"] == "blocking"
    assert updated["budgets"]["max_checks"] == 12
    assert updated["watchdog"]["action"] == "replan"
    with pytest.raises(ValueError, match="policy"):
        merge_manager_loop_update(current, {
            "triggers": {"edit_gate": {"policy": "sometimes"}},
        })


def test_mode_applicability_and_override_are_independent_of_permissions():
    agent = {"metadata": {"manager": {
        "enabled": True,
        "triggers": {
            "plan_gate": {"policy": "blocking", "modes": ["plan", "auto"]},
            "edit_gate": {"policy": "async", "modes": ["auto"]},
        },
        "mode_overrides": {
            "research": {"triggers": {"plan_gate": "async", "edit_gate": "off"}},
        },
    }}}
    ask = resolve_manager_loop(agent, "ask")
    auto = resolve_manager_loop(agent, "auto")
    research = resolve_manager_loop(agent, "research")
    assert ask["triggers"]["plan_gate"]["policy"] == "off"
    assert auto["triggers"]["edit_gate"]["policy"] == "async"
    assert research["triggers"]["plan_gate"]["policy"] == "async"
    assert "permission_policy" not in research


def test_agents_api_exposes_normalized_manager_loop_and_accepts_payload():
    req = UpdateAgentRequest(
        user_id="u1",
        manager_loop={"enabled": True, "budgets": {"max_checks": 5}},
    )
    assert req.manager_loop["enabled"] is True
    row = {
        "id": "a1", "source": "custom", "template_id": "custom",
        "metadata": json.dumps({"manager": {"plan_gate": "blocking"}}),
    }
    result = _safe_agent(row)
    assert result["manager_loop"]["enabled"] is True
    assert result["manager_loop"]["triggers"]["plan_gate"]["policy"] == "blocking"


def test_explicit_manager_model_uses_matching_roster_credentials():
    from app.agent.manager import _resolve_llm

    roster = {
        "default": {"model": "standard", "provider": "p1", "base_url": "u1", "api_key": "k1"},
        "racers": [{
            "model": "fast", "provider": "p2", "base_url": "u2", "api_key": "k2",
            "enabled": True, "text_capable": True,
        }],
    }
    with (
        patch("app.admin.settings.load_llm_capabilities_for_user", new=AsyncMock(return_value=roster)),
        patch("app.agent.output_closer._build_client", return_value=("fast", "p2", "client")) as build,
    ):
        resolved = asyncio.run(_resolve_llm("u1", "fast", {"id": "agent"}))
    assert resolved == ("fast", "p2", "client")
    build.assert_called_once_with("fast", "u2", "k2", "p2")


def test_manager_effort_retries_without_hint_and_observe_does_not_inject():
    from app.agent.manager import run_manager_check

    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("reasoning unsupported")
        message = SimpleNamespace(content='{"verdict":"off_track","reason":"drift","suggestion":"focus"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    agent = {"metadata": {"manager": {
        "enabled": True,
        "effort": "low",
        "watchdog": {"enabled": True, "every_n_turns": 1, "action": "observe"},
    }}}
    inject = AsyncMock()
    stamp = AsyncMock()
    with (
        patch("app.abilities.app_function_enabled", return_value=True),
        patch("app.agent.run_fence.interaction_turn_id", return_value="turn-1"),
        patch("app.agent.run_fence.register_current_one_shot"),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=True)),
        patch("app.agent.manager._collect_manager_span", return_value=(["User: task"], "task")),
        patch("app.agent.manager._resolve_llm", new=AsyncMock(return_value=("fast", "p", client))),
        patch("app.agent.manager._resolve_effort", new=AsyncMock(return_value="low")),
        patch("app.agent.manager._inject_manager_note", new=inject),
        patch("app.agent.manager._stamp_manager_check", new=stamp),
    ):
        verdict = asyncio.run(run_manager_check(
            "watchdog", user_id="u", session_id="s", final_asst_id="a",
            db=object(), agent_rec=agent, execution_mode="auto",
        ))
    assert verdict["verdict"] == "off_track"
    assert calls[0]["extra_body"] == {"reasoning": {"effort": "low"}}
    assert "extra_body" not in calls[1]
    inject.assert_not_awaited()
    stamp.assert_awaited_once()


def test_install_admin_can_persist_live_shared_default_manager_config():
    async def scenario():
        db = SimpleNamespace(
            is_user_admin=AsyncMock(return_value=True),
            get_agent_by_id=AsyncMock(return_value={
                "id": "shared_default", "source": "custom", "metadata": "{}",
            }),
            update_agent_fields=AsyncMock(return_value={
                "id": "shared_default", "source": "custom",
                "metadata": json.dumps({"manager": {"enabled": True,
                    "contracts": {"enabled": True}}}),
            }),
        )
        req = UpdateAgentRequest(
            user_id="admin-user",
            manager_loop={"enabled": True, "contracts": {"enabled": True}},
        )
        request = Request({"type": "http", "headers": []})
        with (
            patch("app.api.agents.get_db", return_value=db),
            patch("app.api.agents.assert_caller_is", new=AsyncMock(return_value="admin-user")),
        ):
            result = await update_agent("shared_default", req, request)
        assert result["agent"]["manager_loop"]["contracts"]["enabled"] is True
        assert db.update_agent_fields.await_args.kwargs["allow_install_admin"] is True

    asyncio.run(scenario())


def test_non_admin_cannot_update_live_shared_default():
    async def scenario():
        db = SimpleNamespace(is_user_admin=AsyncMock(return_value=False))
        req = UpdateAgentRequest(user_id="member", manager_loop={"enabled": True})
        request = Request({"type": "http", "headers": []})
        with (
            patch("app.api.agents.get_db", return_value=db),
            patch("app.api.agents.assert_caller_is", new=AsyncMock(return_value="member")),
        ):
            with pytest.raises(HTTPException) as exc:
                await update_agent("shared_default", req, request)
        assert exc.value.status_code == 403
        assert not hasattr(db, "update_agent_fields")

    asyncio.run(scenario())
