import asyncio
import json
from unittest.mock import patch


class _FakeProcess:
    def __init__(self, *, reply=None, hang=False):
        self.returncode = None
        self.reply = reply
        self.hang = hang
        self.stdin_payload = b""
        self.terminated = False
        self.killed = False

    async def communicate(self, payload):
        self.stdin_payload = payload
        if self.hang:
            await asyncio.Event().wait()
        self.returncode = 0
        return json.dumps({
            "status": "done", "reply": self.reply or "{}", "duration_ms": 7,
        }).encode(), b"child log"

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_contract_turn_uses_stdin_and_clean_json_stdout_boundary():
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    async def scenario():
        proc = _FakeProcess(reply='{"decision":"pass"}')
        command = []

        async def create(*args, **kwargs):
            command.extend(args)
            assert kwargs["stdin"] is asyncio.subprocess.PIPE
            assert kwargs["stdout"] is asyncio.subprocess.PIPE
            assert kwargs["stderr"] is asyncio.subprocess.PIPE
            return proc

        with (
            patch.object(asyncio, "create_subprocess_exec", new=create),
            patch.object(transport, "_spawns_update"),
            patch.object(transport, "_spawns_heartbeat"),
            patch.object(transport, "_heartbeat_loop", new=lambda _sid: asyncio.sleep(3600)),
        ):
            result = await transport.send_contract_request(
                user_id="secret-user", agent_id="agent-1", spawn_id="spawn-1",
                spawn_session_id="worker-session", request="secret-request",
                permission_profile="source_read_only", generation="turn-1", timeout=2,
            )
        assert result["status"] == "done"
        assert result["reply"] == '{"decision":"pass"}'
        assert "secret-user" not in " ".join(str(part) for part in command)
        assert "secret-request" not in " ".join(str(part) for part in command)
        payload = json.loads(proc.stdin_payload)
        assert payload["user_id"] == "secret-user"
        assert payload["permission_profile"] == "source_read_only"
        assert payload["deadline_monotonic"] > 0

    asyncio.run(scenario())


def test_hung_contract_process_is_terminated_and_marked_timeout():
    from plugins.abilities.Core.agent_orchestration import agent_orchestration as transport

    async def scenario():
        proc = _FakeProcess(hang=True)
        statuses = []

        async def create(*_args, **_kwargs):
            return proc

        def update(_spawn_id, **values):
            statuses.append(values.get("status"))

        with (
            patch.object(asyncio, "create_subprocess_exec", new=create),
            patch.object(transport, "_spawns_update", new=update),
            patch.object(transport, "_spawns_heartbeat"),
            patch.object(transport, "_heartbeat_loop", new=lambda _sid: asyncio.sleep(3600)),
        ):
            try:
                await transport.send_contract_request(
                    user_id="u", spawn_id="timeout-spawn",
                    spawn_session_id="worker-session", request="review",
                    timeout=0.01,
                )
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError("hung process did not time out")
        assert proc.terminated
        assert statuses[-2:] == ["stopping", "timeout"]
        assert "timeout-spawn" not in transport._CONTRACT_PROCESSES
        assert "timeout-spawn" not in transport._LIVE_RUNS

    asyncio.run(scenario())


def test_process_local_permission_profiles_remove_bootstrap_tools(monkeypatch):
    from app.agent.contract_permissions import clamp_runtime_agent, filter_tools

    monkeypatch.setenv("WEBAGENT_CONTRACT_SUBPROCESS", "1")
    monkeypatch.setenv("WEBAGENT_CONTRACT_PERMISSION_PROFILE", "tool_free")
    tools = {name: object() for name in (
        "memory", "load_tool", "read_source", "write_source", "spawn_agent",
    )}
    assert filter_tools(tools) == {}

    monkeypatch.setenv("WEBAGENT_CONTRACT_PERMISSION_PROFILE", "source_read_only")
    assert set(filter_tools(tools)) == {"read_source"}
    runtime = clamp_runtime_agent({
        "metadata": {"manager": {"enabled": True}},
        "loop_logic": [{"node": "interrupt_chk", "enabled": True}],
    })
    nodes = {item["node"]: item["enabled"] for item in runtime["loop_logic"]}
    assert nodes["memory_search"] is False
    assert nodes["memory_save"] is False
    assert nodes["manager_chk"] is False
    assert runtime["metadata"]["manager"]["enabled"] is False
