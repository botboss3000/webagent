import asyncio
import json
import sqlite3

import app.abilities
import app.db
from app.agent import output_closer


class _AgentDB:
    def __init__(self, metadata):
        self.metadata = metadata
        self.agent_reads = 0
        self.insert_calls = 0

    async def get_agent_by_id(self, _agent_id):
        self.agent_reads += 1
        return {"id": "agent-1", "metadata": json.dumps(self.metadata)}

    async def insert_interaction(self, *_args, **_kwargs):
        self.insert_calls += 1
        raise AssertionError("disabled Closer must not persist an interaction")


def test_agent_closer_toggle_defaults_enabled_and_only_false_disables():
    assert output_closer._agent_closer_enabled(None)
    assert output_closer._agent_closer_enabled({"metadata": "{}"})
    assert output_closer._agent_closer_enabled(
        {"metadata": json.dumps({"codex_code": {"closer_enabled": True}})})
    assert not output_closer._agent_closer_enabled(
        {"metadata": json.dumps({"codex_code": {"closer_enabled": False}})})


def test_disabled_agent_skips_live_closer_before_any_run_work(monkeypatch):
    db = _AgentDB({"codex_code": {"closer_enabled": False}})
    monkeypatch.setattr(app.abilities, "app_function_enabled", lambda _name: True)
    monkeypatch.setattr(
        output_closer,
        "_run_stopped_by_user",
        lambda *_args: (_ for _ in ()).throw(AssertionError("run work started")),
    )

    asyncio.run(output_closer.run_output_closer(
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        final_asst_id="assistant-1",
        db=db,
    ))

    assert db.agent_reads == 1
    assert db.insert_calls == 0


def test_disabling_during_llm_suppresses_summary_persistence(monkeypatch):
    class _LiveToggleDB(_AgentDB):
        async def get_agent_by_id(self, _agent_id):
            self.agent_reads += 1
            enabled = self.agent_reads == 1
            return {
                "id": "agent-1",
                "metadata": json.dumps({
                    "codex_code": {"closer_enabled": enabled},
                }),
            }

    db = _LiveToggleDB({})

    async def _resolve(_user_id=None):
        return "model", "provider", object()

    async def _attempt(*_args, **_kwargs):
        return "finished summary", [], object()

    async def _note(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app.abilities, "app_function_enabled", lambda _name: True)
    monkeypatch.setattr(output_closer, "_run_stopped_by_user", lambda *_args: False)
    monkeypatch.setattr(
        output_closer, "_resolve_original_parent", lambda _db, parent_id: parent_id)
    monkeypatch.setattr(
        output_closer,
        "_collect_span_messages",
        lambda *_args: (["Assistant: done"], "request", [{"role": "assistant"}]),
    )
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _resolve)
    monkeypatch.setattr(output_closer, "_resolve_audit_config", lambda _rec: (None, 0, False))
    monkeypatch.setattr(output_closer, "_attempt_closer_call", _attempt)
    monkeypatch.setattr(output_closer, "_emit_progress_note", _note)

    asyncio.run(output_closer.run_output_closer(
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        final_asst_id="assistant-1",
        parent_interaction_id="user-message-1",
        db=db,
    ))

    assert db.agent_reads >= 2
    assert db.insert_calls == 0


def test_disabled_agent_suppresses_audit_send_back(monkeypatch):
    class _AuditDB(_AgentDB):
        async def next_session_seq(self, *_args):
            raise AssertionError("disabled audit must not allocate a sequence")

    db = _AuditDB({"codex_code": {"closer_enabled": False}})

    asyncio.run(output_closer._send_audit_back(
        db=db,
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        channel=None,
        final_asst_id="assistant-1",
        feedback="finish it",
        missing=["tests"],
        round_no=1,
        max_rounds=2,
    ))

    assert db.agent_reads == 1
    assert db.insert_calls == 0


def test_recovery_sweep_skips_disabled_agents(monkeypatch):
    db = _AgentDB({"codex_code": {"closer_enabled": False}})
    closer_calls = []

    async def _resolve(_user_id=None):
        return "model", "provider", object()

    async def _run(**kwargs):
        closer_calls.append(kwargs)

    monkeypatch.setattr(app.db, "get_db", lambda: db)
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _resolve)
    monkeypatch.setattr(output_closer, "run_output_closer", _run)
    monkeypatch.setattr(output_closer, "_find_final_rows_without_summary", lambda *_args: [{
        "id": "assistant-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "parent_id": "user-message-1",
        "metadata": "{}",
    }])

    attempted = asyncio.run(output_closer._sweep_once())

    assert attempted == 0
    assert db.agent_reads == 1
    assert closer_calls == []


class _DurableToggleDB(_AgentDB):
    def __init__(self, path):
        super().__init__({"codex_code": {"closer_enabled": False}})
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _seed_recovery_candidate(db):
    conn = db._get_conn()
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT);
        CREATE TABLE session_runs (
          assistant_interaction_id TEXT, stop_cause TEXT
        );
        CREATE TABLE interactions (
          id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
          parent_id TEXT, channel TEXT, metadata TEXT, created_at TEXT,
          source TEXT, status TEXT
        );
        INSERT INTO sessions VALUES ('session-1','user-1','agent-1');
        INSERT INTO interactions VALUES (
          'assistant-1','session-1','assistant','done','user-1',NULL,
          '{"message_phase": "final"}',datetime('now','-1 hour'),
          'agent','complete'
        );
        """
    )
    conn.commit()
    conn.close()


def test_disabled_recovery_candidate_is_durably_ineligible_after_reenable(
    monkeypatch, tmp_path,
):
    db = _DurableToggleDB(tmp_path / "closer-disabled.db")
    _seed_recovery_candidate(db)

    async def _resolve(_user_id=None):
        return "model", "provider", object()

    real_finder = output_closer._find_final_rows_without_summary
    monkeypatch.setattr(app.db, "get_db", lambda: db)
    monkeypatch.setattr(output_closer, "_resolve_fast_llm", _resolve)
    monkeypatch.setattr(output_closer, "_find_final_rows_without_summary", lambda *_args: [{
        "id": "assistant-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "parent_id": "user-1",
        "metadata": '{"message_phase": "final"}',
    }])

    assert asyncio.run(output_closer._sweep_once()) == 0
    conn = db._get_conn()
    stamped = json.loads(conn.execute(
        "SELECT metadata FROM interactions WHERE id='assistant-1'"
    ).fetchone()["metadata"])
    conn.close()
    assert stamped["closer_skipped_disabled"] is True

    # Once enabled, the real recovery query still excludes the intentional
    # no-Closer turn, rather than backfilling it later.
    db.metadata = {"codex_code": {"closer_enabled": True}}
    assert real_finder(db, 10) == []
