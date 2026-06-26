"""Persistent client for driving + observing the RUNNING webAgent app.

One long-lived ``WebAppClient`` owned by the TUI. It logs into the local server
as a user (default ``admin``/``admin``), holds a **WebSocket subscription** to
``/api/v1/agent/ws`` so it sees the same live event stream the web UI does, and
sends messages via ``POST /api/v1/chat/send``. It also exposes small HTTP helpers
for the admin config surface (app settings, the LLM auth key/provider, agent and
session lists).

This talks to the app over its normal HTTP + WebSocket API — the same one the
browser uses — so it can only do what a logged-in user can (no direct DB access).
The stream is multi-participant: messages typed in the browser AND from this TUI
both arrive here, and both render into one shared transcript.

Design notes:
- ``run_stream(on_event)`` is a long-running coroutine the app launches as a
  Textual worker. It (re)connects, replays missed events via the ``resume`` map,
  and calls the async ``on_event`` for every non-ping frame.
- ``send()`` returns the ``turn_id``; ``await_response()`` blocks until the stream
  delivers the matching final ``response`` (or ``error``) — that's how the
  Manager's ``webapp_send`` tool gets a synchronous reply out of an async stream.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

PORT = 8080
_BASE = f"http://127.0.0.1:{PORT}"
_WS_URL = f"ws://127.0.0.1:{PORT}/api/v1/agent/ws"

EventCB = Callable[[dict], Awaitable[None]]


class WebAppError(Exception):
    """A user-facing problem talking to the app (down, auth, bad request)."""


_STANDALONE: "Optional[WebAppClient]" = None


def get_client(ctx: Any = None) -> "WebAppClient":
    """Return the app-owned WebAppClient if the ToolContext carries one, else a
    lazily-created module-level singleton (used by standalone tool calls / tests)."""
    client = getattr(ctx, "webapp_client", None)
    if client is not None:
        return client
    global _STANDALONE
    if _STANDALONE is None:
        _STANDALONE = WebAppClient()
    return _STANDALONE


class WebAppClient:
    def __init__(self, username: str = "admin", password: str = "admin") -> None:
        self.username = username
        self.password = password
        self.base_url = _BASE
        self.token: str = ""
        self.user_id: str = ""
        self.connected: bool = False              # WS currently open + subscribed
        self._last_seq: dict[str, int] = {}       # session_id -> last session_seq seen (resume)
        self._pending: dict[str, asyncio.Future] = {}  # turn_id -> future(reply text)
        self._ws: Any = None
        self._stop = False

    # ── auth ──────────────────────────────────────────────────────────────────
    async def login(self) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as c:
                r = await c.post(f"{self.base_url}/api/v1/auth/login",
                                 json={"email": self.username, "password": self.password})
        except httpx.ConnectError:
            raise WebAppError(f"the webAgent server isn't reachable on {self.base_url}. "
                              "Start it first (server controls), then try again.")
        except httpx.HTTPError as e:
            raise WebAppError(f"contacting the server failed: {e}")
        if r.status_code == 401:
            raise WebAppError(f"login refused for '{self.username}' (bad username/password). "
                              "The default local admin is admin/admin.")
        if r.status_code == 403:
            raise WebAppError(f"account '{self.username}' is not approved for login.")
        if r.status_code >= 400:
            raise WebAppError(f"login failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        self.token = data.get("access_token", "")
        self.user_id = data.get("user_id", "")
        if not self.token or not self.user_id:
            raise WebAppError(f"login response missing token/user_id: {str(data)[:200]}")

    async def ensure_login(self) -> None:
        if not self.token or not self.user_id:
            await self.login()

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    # ── HTTP helpers (admin surface) ────────────────────────────────────────────
    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        import httpx
        await self.ensure_login()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as c:
                r = await c.get(f"{self.base_url}{path}", params=params or {},
                                headers=self._auth_headers())
        except httpx.ConnectError:
            raise WebAppError(f"the webAgent server isn't reachable on {self.base_url}.")
        except httpx.HTTPError as e:
            raise WebAppError(f"{path} failed: {e}")
        if r.status_code == 401:                  # token expired — one retry
            await self.login()
            return await self._get(path, params)
        if r.status_code >= 400:
            raise WebAppError(f"{path} ({r.status_code}): {r.text[:200]}")
        return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        import httpx
        await self.ensure_login()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as c:
                r = await c.post(f"{self.base_url}{path}", json=body, headers=self._auth_headers())
        except httpx.ConnectError:
            raise WebAppError(f"the webAgent server isn't reachable on {self.base_url}.")
        except httpx.HTTPError as e:
            raise WebAppError(f"{path} failed: {e}")
        if r.status_code == 401:
            await self.login()
            return await self._post(path, body)
        if r.status_code >= 400:
            raise WebAppError(f"{path} ({r.status_code}): {r.text[:300]}")
        return r.json()

    async def list_agents(self) -> list[dict]:
        data = await self._get("/api/v1/agents", {"user_id": self.user_id})
        return data.get("agents", []) if isinstance(data, dict) else []

    async def list_sessions(self, agent_id: str = "", limit: int = 20) -> list[dict]:
        params = {"user_id": self.user_id, "limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        data = await self._get("/api/v1/db/sessions", params)
        return data.get("sessions", []) if isinstance(data, dict) else []

    async def get_app_settings(self) -> dict:
        return await self._get("/admin/settings/app")

    async def set_app_settings(self, settings: dict) -> dict:
        return await self._post("/admin/settings/app", settings)

    async def get_provider(self) -> dict:
        return await self._get("/admin/settings/provider")

    async def set_provider(self, cfg: dict) -> dict:
        return await self._post("/admin/settings/provider", cfg)

    async def provider_presets(self) -> dict:
        return await self._get("/admin/settings/providers")

    # ── send + await reply ──────────────────────────────────────────────────────
    async def send(self, session_id: str, message: str, agent_id: str = "") -> dict:
        """Fire-and-forget send; returns {status, session_id, turn_id}. Output
        streams back over the WebSocket."""
        body = {"user_id": self.user_id, "session_id": session_id, "message": message}
        if agent_id:
            body["agent_id"] = agent_id
        return await self._post("/api/v1/chat/send", body)

    async def chat_sync(self, session_id: str, message: str, agent_id: str = "",
                        timeout: float = 300.0) -> str:
        """Synchronous one-shot chat via POST /api/v1/chat — returns the reply text
        directly (no WebSocket needed). Used by the standalone app_chat tool."""
        import httpx
        await self.ensure_login()
        body = {"user_id": self.user_id, "session_id": session_id, "message": message}
        if agent_id:
            body["agent_id"] = agent_id
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as c:
                r = await c.post(f"{self.base_url}/api/v1/chat", json=body,
                                 headers=self._auth_headers())
                if r.status_code == 401:
                    await self.login()
                    body["user_id"] = self.user_id
                    r = await c.post(f"{self.base_url}/api/v1/chat", json=body,
                                     headers=self._auth_headers())
        except httpx.ConnectError:
            raise WebAppError(f"the webAgent server isn't reachable on {self.base_url}.")
        except httpx.ReadTimeout:
            raise WebAppError(f"the app agent didn't reply within {int(timeout)}s on session {session_id}.")
        except httpx.HTTPError as e:
            raise WebAppError(f"chat failed: {e}")
        if r.status_code == 400 and "No agent" in r.text:
            raise WebAppError("this user has no agent assigned — create/select one first.")
        if r.status_code >= 400:
            raise WebAppError(f"chat ({r.status_code}): {r.text[:300]}")
        data = r.json()
        return data.get("reply") or data.get("response") or "(empty reply)"

    async def send_and_wait(self, session_id: str, message: str, agent_id: str = "",
                            timeout: float = 300.0) -> str:
        """Send and block until the app agent's final reply arrives over the stream.

        Requires the stream (run_stream) to be running so the future can resolve.
        Returns the reply text, or raises WebAppError on error/timeout."""
        result = await self.send(session_id, message, agent_id)
        turn_id = result.get("turn_id", "")
        if not turn_id:
            raise WebAppError(f"send did not return a turn id: {str(result)[:200]}")
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[turn_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise WebAppError(f"the app agent didn't finish within {int(timeout)}s "
                              f"(session {session_id}); it may still be working.")
        finally:
            self._pending.pop(turn_id, None)

    # ── live stream ───────────────────────────────────────────────────────────
    async def run_stream(self, on_event: EventCB) -> None:
        """Long-running: keep a subscribed WebSocket open and dispatch every event
        to ``on_event``. Reconnects with backoff, replaying missed events. Launch as
        a worker; call ``stop()`` to end it."""
        import websockets
        self._stop = False
        backoff = 1.0
        while not self._stop:
            try:
                await self.ensure_login()
            except WebAppError:
                await asyncio.sleep(min(backoff, 10.0))
                backoff = min(backoff * 2, 10.0)
                continue
            try:
                async with websockets.connect(_WS_URL, max_size=None, open_timeout=10) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({
                        "mode": "user_subscriber",
                        "user_id": self.user_id,
                        "token": self.token,
                        "resume": dict(self._last_seq),
                    }))
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            ev = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        etype = ev.get("type")
                        if etype == "ping":
                            continue
                        if etype == "subscribed":
                            self.connected = True
                            await on_event(ev)
                            continue
                        # Track the per-session sequence for resume on reconnect.
                        sid = ev.get("session_id")
                        seq = ev.get("session_seq")
                        if isinstance(sid, str) and isinstance(seq, int):
                            if seq > self._last_seq.get(sid, -1):
                                self._last_seq[sid] = seq
                        # Resolve any pending send_and_wait future.
                        self._resolve_pending(ev)
                        await on_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # connection dropped / handshake error — reconnect
                self.connected = False
                try:
                    await on_event({"type": "_ws_status", "ok": False, "detail": str(e)})
                except Exception:
                    pass
                if self._stop:
                    break
                await asyncio.sleep(min(backoff, 10.0))
                backoff = min(backoff * 2, 10.0)
        self.connected = False
        self._ws = None

    def _resolve_pending(self, ev: dict) -> None:
        etype = ev.get("type")
        turn_id = ev.get("turn_id")
        if etype == "response" and turn_id and turn_id in self._pending:
            fut = self._pending.get(turn_id)
            if fut and not fut.done():
                fut.set_result(ev.get("content") or "")
        elif etype == "error":
            # An error event may not carry our turn_id; fail the newest pending
            # future for the same session so a blocked send doesn't hang forever.
            sid = ev.get("session_id")
            msg = ev.get("message") or "the app agent reported an error"
            if turn_id and turn_id in self._pending:
                fut = self._pending.get(turn_id)
                if fut and not fut.done():
                    fut.set_exception(WebAppError(msg))
            elif sid:
                for tid, fut in list(self._pending.items()):
                    if not fut.done():
                        fut.set_exception(WebAppError(msg))
                        break

    async def stop(self) -> None:
        self._stop = True
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:
            pass
        self.connected = False
