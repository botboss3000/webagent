import asyncio
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.agent.manager_config import legacy_manager_view, resolve_manager_loop
from app.agent.subagent_contracts import (
    CONTRACT_SPECS,
    ContractSupervisor,
    _write_state,
    contracts_available,
    _request,
    _worker_name,
    recover_orphaned_contract_checks,
    stop_turn_workers,
    validate_request,
    validate_result,
)


class _Db:
    def __init__(self, path: Path):
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _agent():
    return {"metadata": {"manager": {
        "enabled": True,
        "contracts": {
            "enabled": True,
            "engine": "subagent",
            "failure_policy": "hybrid",
            "scout": {"enabled": True},
            "edit_review": {
                "enabled": True, "policy": "blocking", "max_checks": 2,
            },
            "close_review": {
                "enabled": True, "policy": "blocking", "max_rounds": 1,
            },
        },
    }}}


def _supervisor(db):
    return ContractSupervisor(
        db=db, user_id="user-1", session_id="session-1", agent_id="agent-1",
        agent_rec=_agent(), turn_id="turn-1", generation="turn-1",
        execution_mode="auto",
    )


def test_contract_result_schema_rejects_mismatched_and_malformed_results():
    spec = CONTRACT_SPECS["edit_review"]
    request = _request(
        spec=spec, session_id="s", turn_id="t", generation="t",
        mission_digest="m", plan_revision=2, invocation=1,
        evidence={
            "review_kind": "edit_gate", "request": "change it",
            "working_context": "ctx", "edit": "edit_source a.py",
        },
    )
    valid = {
        "request_id": request["request_id"], "contract_id": spec.id,
        "version": spec.version, "decision": "revise", "reason": "unsafe edit",
        "findings": [{"message": "wrong file"}],
        "corrective_actions": ["target the requested module"], "payload": {},
    }
    assert validate_request(request, spec)
    assert not validate_request({**request, "plan_revision": 3}, spec)
    assert validate_result(json.dumps(valid), request, spec)["decision"] == "revise"
    assert validate_result(json.dumps({**valid, "request_id": "stale"}), request, spec) is None
    assert validate_result(json.dumps({**valid, "version": 99}), request, spec) is None
    assert validate_result(json.dumps({**valid, "findings": [], "corrective_actions": []}), request, spec) is None
    assert validate_result("not json", request, spec) is None


def test_contract_configuration_is_opt_in_and_managed_edit_policy_owns_gates():
    disabled = resolve_manager_loop({"metadata": {"manager": {
        "enabled": False, "contracts": {"enabled": True},
    }}}, "auto")
    assert disabled["contracts"]["enabled"] is False

    runtime = legacy_manager_view(resolve_manager_loop(_agent(), "auto"))
    assert runtime["plan_gate"] == "blocking"
    assert runtime["edit_gate"] == "blocking"
    assert runtime["max_checks_by_kind"]["edit_gate"] == 2


def test_run_scout_worker_is_named_scout():
    assert _worker_name(CONTRACT_SPECS["run_scout"]) == "Scout"
    assert _worker_name(CONTRACT_SPECS["edit_review"]) == "Contract · edit_review"


def test_read_only_contract_worker_uses_orchestration_with_hard_denies():
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    create = AsyncMock(return_value={"id": "spawn-1"})
    with patch.object(transport, "_create_spawn", new=create):
        asyncio.run(transport.create_contract_worker(
            user_id="u", orchestrator_session_id="s", orchestrator_agent_id="a",
            name="reviewer", system_prompt="review", output_contract="{}",
            permission_profile="source_read_only",
        ))
    kwargs = create.await_args.kwargs
    assert kwargs["abilities"] == ["codebase_admin"]
    assert kwargs["kind"] == "contract"
    denied = set(kwargs["forced_deny_tools"])
    assert {"db_query", "write_source", "delete_source", "run_command", "restart_server",
            "spawn_agent", "commit_and_push"}.issubset(denied)


def test_edit_reviewer_is_reused_and_budget_failures_are_persisted(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)
    creates = []
    sends = []

    async def create_worker(**_kwargs):
        creates.append(1)
        return {
            "id": "spawn-1", "spawn_session_id": "worker-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        sends.append(kwargs)
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": 1, "decision": "pass", "reason": "matches the plan",
            "findings": [], "corrective_actions": [], "payload": {},
        })}

    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=True)),
    ):
        first = asyncio.run(supervisor.review_edit(
            review_kind="plan_gate", request_text="change it", working_context="ctx",
            edit="edit_source a.py", plan=["change a"], plan_revision=1, invocation=1,
        ))
        second = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx2",
            edit="edit_source b.py", plan=["change a"], plan_revision=1, invocation=2,
        ))
        over_budget = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx3",
            edit="edit_source c.py", plan=["change a"], plan_revision=1, invocation=3,
        ))

    assert first["decision"] == second["decision"] == "pass"
    assert over_budget["decision"] == "inconclusive"
    assert len(creates) == 1
    assert len(sends) == 2
    conn = db._get_conn()
    try:
        statuses = [row[0] for row in conn.execute(
            "SELECT status FROM run_contract_checks ORDER BY started_at",
        ).fetchall()]
    finally:
        conn.close()
    assert statuses == ["complete", "complete", "skipped"]


def test_close_reviewers_run_concurrently_and_do_not_share_verdicts(tmp_path):
    supervisor = _supervisor(_Db(tmp_path / "contracts.db"))
    entered = 0
    both_entered = asyncio.Event()
    seen = []

    async def execute(spec, **_kwargs):
        nonlocal entered
        entered += 1
        seen.append(spec.id)
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        return ({
            "decision": "pass", "reason": f"{spec.id} passed",
            "findings": [], "corrective_actions": [],
        }, {"id": spec.id, "spawn_session_id": spec.id + "-session"})

    supervisor._execute = execute
    supervisor._stop_worker = AsyncMock()
    result = asyncio.run(supervisor.review_close(
        request_text="ship it", checklist=["tests pass"],
        completion_evidence={"verification_events": ["pytest passed"]}, round_no=1,
    ))

    assert result["decision"] == "pass"
    assert set(seen) == {"close_alignment", "close_evidence"}
    assert supervisor._stop_worker.await_count == 2


def test_contract_availability_requires_manager_node_and_orchestration():
    class _AbilityDb:
        async def get_agent_connections(self, _agent_id):
            return [{
                "section": "ability", "connection_type": "agent_orchestration",
                "enabled": True,
            }]

    enabled = _agent()
    disabled_node = _agent()
    disabled_node["loop_logic"] = [{"node": "manager_chk", "enabled": False}]
    with patch("app.abilities.app_function_enabled", return_value=True):
        assert asyncio.run(contracts_available(_AbilityDb(), "a", enabled, "auto"))
        assert not asyncio.run(contracts_available(
            _AbilityDb(), "a", disabled_node, "auto",
        ))


def test_stale_worker_result_is_discarded_and_persisted(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)

    async def create_worker(**_kwargs):
        return {
            "id": "spawn-1", "spawn_session_id": "worker-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": 1, "decision": "block", "reason": "old generation",
            "findings": ["stale"], "corrective_actions": ["ignore"], "payload": {},
        })}

    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=False)),
    ):
        result = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx",
            edit="edit_source a.py", plan=[], plan_revision=1, invocation=1,
        ))
    assert result["decision"] == "inconclusive"
    assert "stale" in result["reason"]
    conn = db._get_conn()
    try:
        assert conn.execute(
            "SELECT status FROM run_contract_checks",
        ).fetchone()[0] == "stale"
    finally:
        conn.close()


def test_orphaned_persistent_reviewer_is_recreated_for_same_request(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)
    _write_state(
        db, "session-1", "turn-1", reviewer_spawn_id="dead-spawn",
        reviewer_session_id="dead-session", plan_revision=1,
    )
    sends = []

    async def create_worker(**_kwargs):
        return {
            "id": "fresh-spawn", "spawn_session_id": "fresh-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        sends.append(kwargs["spawn_id"])
        if kwargs["spawn_id"] == "dead-spawn":
            raise RuntimeError("orphaned after restart")
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": 1, "decision": "pass", "reason": "fresh review",
            "findings": [], "corrective_actions": [], "payload": {},
        })}

    supervisor._stop_worker = AsyncMock()
    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=True)),
    ):
        result = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx",
            edit="edit_source a.py", plan=[], plan_revision=1, invocation=1,
        ))
    assert result["decision"] == "pass"
    assert sends == ["dead-spawn", "fresh-spawn"]
    assert supervisor._stop_worker.await_count == 1


def test_worker_ownership_is_durable_before_first_request(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)

    async def create_worker(**_kwargs):
        return {
            "id": "owned-spawn", "spawn_session_id": "owned-session",
            "granted_abilities": ["codebase_admin"],
        }

    async def send_request(**kwargs):
        conn = db._get_conn()
        try:
            check = conn.execute(
                "SELECT status,worker_spawn_id,worker_session_id "
                "FROM run_contract_checks"
            ).fetchone()
            state = conn.execute(
                "SELECT reviewer_spawn_id,reviewer_session_id "
                "FROM run_contract_state"
            ).fetchone()
        finally:
            conn.close()
        assert tuple(check) == ("running", "owned-spawn", "owned-session")
        assert tuple(state) == ("owned-spawn", "owned-session")
        envelope = json.loads(kwargs["request"].split("\n", 1)[1])
        return {"status": "done", "reply": json.dumps({
            "request_id": envelope["request_id"], "contract_id": "edit_review",
            "version": 1, "decision": "pass", "reason": "exact edit approved",
            "findings": [], "corrective_actions": [], "payload": {},
        })}

    with (
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
        patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=send_request),
        patch("app.agent.run_fence.side_effects_allowed", new=AsyncMock(return_value=True)),
    ):
        result = asyncio.run(supervisor.review_edit(
            review_kind="edit_gate", request_text="change it", working_context="ctx",
            edit=json.dumps({"tool_call_id": "tc-1", "tool": "write_source",
                             "args": {"path": "a.py", "content": "x"}}),
            plan=[], plan_revision=1, invocation=1,
        ))
    assert result["decision"] == "pass"


def test_absolute_deadline_includes_worker_creation(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)

    async def hung_create(**_kwargs):
        await asyncio.sleep(10)

    started = time.monotonic()
    with patch(
        "plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker",
        new=hung_create,
    ):
        result, _worker = asyncio.run(supervisor._execute(
            CONTRACT_SPECS["edit_review"], timeout_override=1,
            evidence={
                "review_kind": "edit_gate", "request": "change it",
                "working_context": "ctx", "edit": "{}", "plan_revision": 1,
            },
        ))
    assert time.monotonic() - started < 2.5
    assert result["decision"] == "inconclusive"
    conn = db._get_conn()
    try:
        assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "timeout"
    finally:
        conn.close()


def test_cancelled_review_is_terminal_and_worker_is_stopped(tmp_path):
    async def scenario():
        db = _Db(tmp_path / "contracts.db")
        supervisor = _supervisor(db)
        entered = asyncio.Event()

        async def create_worker(**_kwargs):
            return {
                "id": "cancel-spawn", "spawn_session_id": "cancel-session",
                "granted_abilities": ["codebase_admin"],
            }

        async def hung_send(**_kwargs):
            entered.set()
            await asyncio.Event().wait()

        supervisor._stop_worker = AsyncMock()
        with (
            patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.create_worker", new=create_worker),
            patch("plugins.abilities.Core.agent_orchestration.agent_orchestration.send_worker_request", new=hung_send),
        ):
            task = asyncio.create_task(supervisor.review_edit(
                review_kind="edit_gate", request_text="change it", working_context="ctx",
                edit="{}", plan=[], plan_revision=1, invocation=1,
            ))
            await entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        conn = db._get_conn()
        try:
            assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "cancelled"
            state = conn.execute(
                "SELECT reviewer_spawn_id,reviewer_session_id,status FROM run_contract_state"
            ).fetchone()
            assert tuple(state) == (None, None, "stopped")
        finally:
            conn.close()
        supervisor._stop_worker.assert_awaited()

    asyncio.run(scenario())


def test_stop_turn_workers_uses_running_check_ownership(tmp_path):
    db = _Db(tmp_path / "contracts.db")
    supervisor = _supervisor(db)
    spec = CONTRACT_SPECS["close_alignment"]
    request = _request(
        spec=spec, session_id="session-1", turn_id="turn-1", generation="turn-1",
        mission_digest="m", plan_revision=0, invocation=1,
        evidence={"request": "ship", "checklist": [], "completion_evidence": {}},
    )
    from app.agent.subagent_contracts import _record_start, _record_worker
    _record_start(
        db, request_id=request["request_id"], session_id="session-1", turn_id="turn-1",
        spec=spec, request_hash=request["request_hash"], evidence={},
    )
    _record_worker(
        db, request_id=request["request_id"], spawn_id="audit-spawn",
        spawn_session_id="audit-session",
    )
    stop = AsyncMock()
    with patch(
        "plugins.abilities.Core.agent_orchestration.agent_orchestration.stop_worker",
        new=stop,
    ):
        asyncio.run(stop_turn_workers(db, "session-1", "turn-1"))
    stop.assert_awaited_once_with(
        spawn_id="audit-spawn", spawn_session_id="audit-session",
    )
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT status,decision FROM run_contract_checks"
        ).fetchone()
        assert tuple(row) == ("cancelled", "inconclusive")
    finally:
        conn.close()


def test_startup_recovery_cancels_checks_for_stopped_parent(tmp_path):
    class StoppedDb(_Db):
        async def run_state_get(self, _session_id):
            return {"turn_id": "turn-1", "stop_cause": "user_stop"}

    db = StoppedDb(tmp_path / "contracts.db")
    spec = CONTRACT_SPECS["edit_review"]
    request = _request(
        spec=spec, session_id="session-1", turn_id="turn-1", generation="turn-1",
        mission_digest="m", plan_revision=1, invocation=1,
        evidence={"review_kind": "edit_gate", "request": "change",
                  "working_context": "ctx", "edit": "{}"},
    )
    from app.agent.subagent_contracts import _record_start, _record_worker
    _record_start(
        db, request_id=request["request_id"], session_id="session-1", turn_id="turn-1",
        spec=spec, request_hash=request["request_hash"], evidence={},
    )
    _record_worker(
        db, request_id=request["request_id"], spawn_id="old-spawn",
        spawn_session_id="old-session",
    )
    assert asyncio.run(recover_orphaned_contract_checks(db)) == 1
    conn = db._get_conn()
    try:
        assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "cancelled"
    finally:
        conn.close()


def test_startup_recovery_leaves_active_unreserved_request_for_redispatch(tmp_path):
    class ActiveDb(_Db):
        async def run_state_get(self, _session_id):
            return {"turn_id": "turn-1", "stop_cause": "server_restart"}

    db = ActiveDb(tmp_path / "contracts.db")
    spec = CONTRACT_SPECS["edit_review"]
    request = _request(
        spec=spec, session_id="session-1", turn_id="turn-1", generation="turn-1",
        mission_digest="m", plan_revision=1, invocation=1,
        evidence={"review_kind": "edit_gate", "request": "change",
                  "working_context": "ctx", "edit": "{}"},
    )
    from app.agent.subagent_contracts import _record_start
    _record_start(
        db, request_id=request["request_id"], session_id="session-1", turn_id="turn-1",
        spec=spec, request_hash=request["request_hash"], evidence={},
    )
    assert asyncio.run(recover_orphaned_contract_checks(db)) == 0
    conn = db._get_conn()
    try:
        assert conn.execute("SELECT status FROM run_contract_checks").fetchone()[0] == "running"
    finally:
        conn.close()
