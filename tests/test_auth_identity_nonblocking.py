from __future__ import annotations

import asyncio
import threading
import time

from app.auth import identity


def test_caller_identity_revocation_check_runs_off_event_loop(monkeypatch) -> None:
    started = threading.Event()
    worker_threads = []
    downstream = []

    def slow_decode(_token: str):
        worker_threads.append(threading.get_ident())
        started.set()
        time.sleep(0.15)
        return {"user_id": "verified-user", "sub": "verified-user"}

    async def app(scope, _receive, _send):
        downstream.append(scope["state"].get("auth_payload"))

    monkeypatch.setattr(identity, "decode_token", slow_decode)
    middleware = identity.CallerIdentityMiddleware(app)

    async def scenario():
        caller_thread = threading.get_ident()
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer test-token")],
            "query_string": b"",
        }
        task = asyncio.create_task(middleware(scope, None, None))
        for _ in range(20):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        await asyncio.sleep(0.01)
        assert not task.done()
        await task
        return caller_thread

    caller_thread = asyncio.run(scenario())
    assert worker_threads[0] != caller_thread
    assert downstream == [{"user_id": "verified-user", "sub": "verified-user"}]
