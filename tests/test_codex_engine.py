import asyncio
import json
from pathlib import Path

from app.util.paths import project_root
from plugins.engines.codex import codex as engine


class _Db:
    async def get_session_codex_id(self, _session_id):
        return None

    async def insert_interaction(self, *_args, **_kwargs):
        return "interaction"


def test_remote_codex_turn_ignores_stale_configured_folder(monkeypatch, tmp_path):
    captured = {}

    class _Proc:
        stdout = iter(())
        stderr = None

        def __init__(self, cmd, **kwargs):
            captured.update(cmd=cmd, **kwargs)

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(engine.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(engine.subprocess, "Popen", _Proc)
    agent = {"metadata": json.dumps({"codex_code": {"folder": str(tmp_path)}})}

    async def run():
        return [event async for event in engine.stream(
            user_id="user", session_id="session", agent_id="agent",
            user_message="hello", agent_rec=agent, db=_Db(),
        )]

    asyncio.run(run())

    live_repo = str(Path(project_root()))
    assert captured["cwd"] == live_repo
    assert captured["cmd"][captured["cmd"].index("--cd") + 1] == live_repo
    assert str(tmp_path) not in captured["cmd"]
