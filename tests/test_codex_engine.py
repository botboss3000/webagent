import asyncio
import json
import subprocess
from pathlib import Path

from app.util.paths import project_root
from plugins.engines.codex import codex as engine


class _Db:
    def __init__(self, thread_id=None, *, forbid_native_state=False):
        self.thread_id = thread_id
        self.forbid_native_state = forbid_native_state
        self.native_reads = 0
        self.native_writes = []

    async def get_session_codex_id(self, _session_id):
        if self.forbid_native_state:
            raise AssertionError("wrapper mode must not read Codex thread state")
        self.native_reads += 1
        return self.thread_id

    async def get_session_codex_reseed(self, _session_id):
        if self.forbid_native_state:
            raise AssertionError("wrapper mode must not read Codex reseed state")
        return None

    async def set_session_codex_id(self, _session_id, thread_id):
        if self.forbid_native_state:
            raise AssertionError("wrapper mode must not write Codex thread state")
        self.native_writes.append(thread_id)

    async def insert_interaction(self, *_args, **_kwargs):
        return "interaction"


class _Input:
    def __init__(self):
        self.value = ""
        self.closed = False

    def write(self, value):
        self.value += value

    def close(self):
        self.closed = True


def _install_process(monkeypatch, captured, records=()):
    class _Proc:
        stderr = None
        returncode = None

        def __init__(self, cmd, **kwargs):
            captured.update(cmd=cmd, **kwargs)
            self.stdout = iter(json.dumps(record) + "\n" for record in records)
            self.stdin = _Input() if kwargs.get("stdin") == subprocess.PIPE else None
            captured["proc"] = self

        def wait(self):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(engine.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(engine.subprocess, "Popen", _Proc)


def _run(agent, db, *, message="current request", history=None):
    async def run():
        return [event async for event in engine.stream(
            user_id="user", session_id="session", agent_id="agent",
            user_message=message, agent_rec=agent, db=db, history=history,
        )]

    return asyncio.run(run())


def test_remote_codex_turn_ignores_stale_configured_folder(monkeypatch, tmp_path):
    captured = {}
    _install_process(monkeypatch, captured)
    agent = {"metadata": json.dumps({"codex_code": {"folder": str(tmp_path)}})}

    _run(agent, _Db())

    assert captured["cwd"] == str(Path(project_root()))
    assert "--cd" not in captured["cmd"]
    assert str(tmp_path) not in captured["cmd"]


def test_absent_context_mode_preserves_native_resume(monkeypatch):
    captured = {}
    _install_process(monkeypatch, captured)
    db = _Db("thread-native")

    _run({"metadata": json.dumps({"codex_code": {}})}, db)

    assert captured["cmd"][:3] == ["codex", "exec", "resume"]
    assert "thread-native" in captured["cmd"]
    assert "--ephemeral" not in captured["cmd"]
    assert captured["stdin"] == subprocess.DEVNULL
    assert db.native_reads == 1


def test_wrapper_is_ephemeral_uses_stdin_and_never_touches_native_state(monkeypatch):
    captured = {}
    _install_process(monkeypatch, captured, records=(
        {"type": "thread.started", "thread_id": "must-not-persist"},
        {"type": "item.completed", "item": {
            "id": "msg-1", "type": "agent_message", "text": "done",
        }},
    ))
    db = _Db(forbid_native_state=True)
    agent = {"metadata": json.dumps({"codex_code": {
        "context_mode": "webagent_wrapper",
    }})}
    history = [
        {"role": "user", "content": "prior request"},
        {"role": "assistant", "content": "prior answer"},
    ]

    events = _run(agent, db, message="current request", history=history)

    assert captured["cmd"][:3] == ["codex", "exec", "--ephemeral"]
    assert "resume" not in captured["cmd"]
    assert captured["cmd"][-1] == "current request"
    assert captured["stdin"] == subprocess.PIPE
    assert captured["proc"].stdin.closed
    assert "prior request" in captured["proc"].stdin.value
    assert "prior answer" in captured["proc"].stdin.value
    assert "User:\ncurrent request" not in captured["proc"].stdin.value
    assert any(event.get("type") == "response" and event.get("content") == "done"
               for event in events)
    assert db.native_writes == []


def test_empty_internal_turn_continues_from_history(monkeypatch):
    captured = {}
    _install_process(monkeypatch, captured, records=({
        "type": "item.completed",
        "item": {"id": "msg-1", "type": "agent_message", "text": "audit fixed"},
    },))
    agent = {"metadata": json.dumps({"codex_code": {
        "context_mode": "webagent_wrapper",
    }})}

    events = _run(
        agent, _Db(forbid_native_state=True), message=None,
        history=[{"role": "user", "content": "Audit: add the missing test."}],
    )

    assert "Continue the unfinished task" in captured["cmd"][-1]
    assert "Audit: add the missing test." in captured["proc"].stdin.value
    assert any(event.get("content") == "audit fixed" for event in events)


def test_native_internal_continuation_includes_latest_audit(monkeypatch):
    captured = {}
    _install_process(monkeypatch, captured)
    db = _Db("native-thread")

    _run(
        {"metadata": json.dumps({"codex_code": {"context_mode": "native_codex"}})},
        db, message=None,
        history=[{"role": "system", "content": "AUDIT FAILED: add regression coverage"}],
    )

    assert captured["cmd"][:3] == ["codex", "exec", "resume"]
    assert "AUDIT FAILED: add regression coverage" in captured["cmd"][-1]


def test_empty_turn_without_history_keeps_empty_message_response(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda _name: "codex")
    events = _run({"metadata": "{}"}, _Db(), message=None, history=[])
    assert events == [{
        "type": "response", "level": "agent",
        "content": "I didn't get a message to send to Codex.",
    }]


def test_switching_back_to_native_starts_fresh_and_seeds_from_webagent(monkeypatch):
    from plugins.engines.codex import context_store

    captured = {}
    _install_process(monkeypatch, captured, records=(
        {"type": "thread.started", "thread_id": "new-native-thread"},
        {"type": "item.completed", "item": {
            "id": "msg-1", "type": "agent_message", "text": "continued",
        }},
    ))
    monkeypatch.setattr(
        context_store, "note_session_mode",
        lambda _db, _sid, _mode: "webagent_wrapper",
    )
    db = _Db(thread_id="stale-native-thread")
    history = [{"role": "user", "content": "work completed while wrapped"}]

    _run({"metadata": json.dumps({"codex_code": {
        "context_mode": "native_codex",
    }})}, db, history=history)

    assert captured["cmd"][:2] == ["codex", "exec"]
    assert "resume" not in captured["cmd"]
    assert "--ephemeral" not in captured["cmd"]
    assert captured["stdin"] == subprocess.PIPE
    assert "work completed while wrapped" in captured["proc"].stdin.value
    assert db.native_reads == 0
    assert db.native_writes == ["new-native-thread", "new-native-thread"]


def test_native_mode_fence_failure_fails_safe_without_resuming_stale_thread(monkeypatch):
    from plugins.engines.codex import context_store

    captured = {}
    _install_process(monkeypatch, captured)
    monkeypatch.setattr(
        context_store, "note_session_mode",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database locked")),
    )
    db = _Db(thread_id="stale-native-thread")

    _run({"metadata": json.dumps({"codex_code": {
        "context_mode": "native_codex",
    }})}, db, history=[{"role": "user", "content": "authoritative history"}])

    assert "resume" not in captured["cmd"]
    assert "--ephemeral" not in captured["cmd"]
    assert captured["stdin"] == subprocess.PIPE
    assert db.native_reads == 0
