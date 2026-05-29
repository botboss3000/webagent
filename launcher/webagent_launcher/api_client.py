"""Thin async HTTP/SSE client for the local webagent server.

The in-TUI chat talks to the *same* running server the launcher starts —
exactly how the browser UI works — so it can run the agent loop, stream tool
calls, and read `local.db`. No direct SQLite access.

Endpoints used (all already exist in the backend):
  POST /api/v1/auth/login                         → token + user_id
  GET  /api/v1/agents/templates                   → creatable templates
  GET  /api/v1/agents?user_id=                     → user's custom agents
  POST /api/v1/agents                              → clone a template → custom agent
  GET  /api/v1/db/sessions?user_id=&agent_id=      → sessions
  GET  /api/v1/db/stream/interactions?session_id=  → rich history (tool calls)
  GET  /api/v1/db/session-messages?session_id=     → history (fallback)
  POST /api/v1/chat/stream                          → SSE live turn
  POST /api/v1/chat/interrupt                       → stop generation
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx

from .server import WEBAGENT_HOST, WEBAGENT_PORT

BASE_URL = f"http://{WEBAGENT_HOST}:{WEBAGENT_PORT}"
DB_NAME = "local.db"


class WebAgentError(Exception):
    """Any non-success response or transport failure."""


class WebAgentClient:
    """One httpx.AsyncClient + a bearer token for the whole chat session."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str = ""
        self.user_id: str = ""
        self.display_name: str = ""
        # Long read timeout: chat turns can take a while between SSE chunks.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(15.0, read=300.0),
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ── auth ───────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def login(self, username: str, password: str) -> None:
        try:
            r = await self._client.post(
                "/api/v1/auth/login",
                json={"email": username, "password": password},
            )
        except httpx.HTTPError as e:
            raise WebAgentError(f"Cannot reach server: {e}") from e
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:200]
            raise WebAgentError(f"Login failed ({r.status_code}): {detail}")
        data = r.json()
        self.token = data.get("access_token", "")
        self.user_id = data.get("user_id", "")
        self.display_name = data.get("display_name", "") or username
        if not self.token or not self.user_id:
            raise WebAgentError("Login response missing token/user_id")

    # ── agents ─────────────────────────────────────────────────────────
    async def list_custom_agents(self) -> list[dict[str, Any]]:
        r = await self._client.get(
            "/api/v1/agents",
            params={"user_id": self.user_id},
            headers=self._headers(),
        )
        if r.status_code != 200:
            return []
        return r.json().get("agents", []) or []

    async def list_templates(self) -> list[dict[str, Any]]:
        # user_id is required; include_admin=true (allowed for our admin login)
        # bypasses the discoverable filter so all built-in templates show up.
        r = await self._client.get(
            "/api/v1/agents/templates",
            params={"user_id": self.user_id, "include_admin": "true"},
            headers=self._headers(),
        )
        if r.status_code != 200:
            # Retry without admin in case the user isn't an admin.
            r = await self._client.get(
                "/api/v1/agents/templates",
                params={"user_id": self.user_id},
                headers=self._headers(),
            )
            if r.status_code != 200:
                return []
        return r.json().get("templates", []) or []

    async def create_agent(self, name: str, template_id: str) -> dict[str, Any]:
        r = await self._client.post(
            "/api/v1/agents",
            json={"user_id": self.user_id, "name": name, "template_id": template_id},
            headers=self._headers(),
        )
        if r.status_code != 200:
            raise WebAgentError(f"Create agent failed ({r.status_code})")
        return r.json().get("agent", {}) or {}

    # ── sessions ───────────────────────────────────────────────────────
    async def list_sessions(self, agent_id: Optional[str] = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {"user_id": self.user_id, "db": DB_NAME}
        if agent_id:
            params["agent_id"] = agent_id
        if self.token:
            params["token"] = self.token
        r = await self._client.get(
            "/api/v1/db/sessions", params=params, headers=self._headers()
        )
        if r.status_code != 200:
            return []
        return r.json().get("sessions", []) or []

    # ── history ────────────────────────────────────────────────────────
    async def load_history(self, session_id: str) -> list[dict[str, Any]]:
        """Rich interactions (role/content/tool_name/input/output), with a
        graceful fallback to the simpler session-messages endpoint."""
        try:
            r = await self._client.get(
                "/api/v1/db/stream/interactions",
                params={"session_id": session_id, "db": DB_NAME},
                headers=self._headers(),
            )
            if r.status_code == 200:
                rows = r.json().get("interactions", []) or []
                if rows:
                    return rows
        except httpx.HTTPError:
            pass
        # Fallback
        try:
            r = await self._client.get(
                "/api/v1/db/session-messages",
                params={"session_id": session_id, "db": DB_NAME, "token": self.token},
                headers=self._headers(),
            )
            if r.status_code == 200:
                return r.json().get("messages", []) or []
        except httpx.HTTPError:
            pass
        return []

    # ── chat ───────────────────────────────────────────────────────────
    async def upload_image(self, path: str, session_id: Optional[str] = None) -> dict[str, Any]:
        """Upload a local image file; return its attachment dict (incl. id)."""
        p = Path(path)
        if not p.is_file():
            raise WebAgentError(f"not a file: {path}")
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        data: dict[str, str] = {"user_id": self.user_id}
        if session_id:
            data["session_id"] = session_id
        files = {"file": (p.name, p.read_bytes(), mime)}
        r = await self._client.post(
            "/api/v1/upload", data=data, files=files, headers=self._headers()
        )
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:160]
            raise WebAgentError(f"upload failed ({r.status_code}): {detail}")
        return r.json().get("attachment", {}) or {}

    async def stream_chat(
        self,
        message: str,
        session_id: str,
        agent_id: Optional[str] = None,
        agent_template_id: Optional[str] = None,
        attachment_ids: Optional[list[str]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """POST the message and yield parsed SSE event dicts as they arrive."""
        payload: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
            "user_id": self.user_id,
        }
        if agent_id:
            payload["agent_id"] = agent_id
        elif agent_template_id:
            payload["agent_template_id"] = agent_template_id
        if attachment_ids:
            payload["attachment_ids"] = attachment_ids

        async with self._client.stream(
            "POST",
            "/api/v1/chat/stream",
            json=payload,
            headers=self._headers(),
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise WebAgentError(f"Chat failed ({resp.status_code}): {body[:200]!r}")
            async for line in resp.aiter_lines():
                if not line or line.startswith(":"):
                    continue  # keepalive / comment
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue

    async def interrupt(self, session_id: str) -> None:
        try:
            await self._client.post(
                "/api/v1/chat/interrupt",
                json={"session_id": session_id},
                headers=self._headers(),
            )
        except httpx.HTTPError:
            pass
