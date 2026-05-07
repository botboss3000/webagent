"""
Built-in source management tools injected by loader.py.

Gives the agent full filesystem access: read, write, edit, delete, shell commands.
Delete this file (along with source.py) to lock down the agent.
"""

import logging

from app.tools.loader import ToolInfo

logger = logging.getLogger(__name__)

BASE = "http://localhost:8080"


async def _api_get(path: str, params: dict = None):
    import httpx
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        r = await c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


async def _api_post(path: str, json_data: dict):
    import httpx
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        r = await c.post(path, json=json_data)
        return r


def inject_source_tools(tools: dict, user_id: str) -> None:
    """Inject filesystem tools into the tools dict."""

    # ── read_source: no confirmation needed, read only ──
    async def _read_source(path: str) -> str:
        """Read file contents. Safe — no changes made."""
        data = await _api_get("/admin/source/read", {"path": path})
        return data["content"]

    tools["read_source"] = ToolInfo(
        name="read_source",
        handler=_read_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)"},
            },
            "required": ["path"],
        },
    )

    # ── write_source: REQUIRES user confirmation before calling ──
    async def _write_source(path: str, content: str) -> str:
        """[REQUIRES CONFIRMATION] Overwrite a file with new content. Ask the user before calling."""
        resp = await _api_post("/admin/source/write", {
            "path": path, "content": content, "create_backup": True,
        })
        if resp.status_code != 200:
            return f"Error: {resp.json().get('detail', 'unknown error')}"
        return resp.json()["message"]

    tools["write_source"] = ToolInfo(
        name="write_source",
        handler=_write_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)"},
                "content": {"type": "string", "description": "Full content to write to the file"},
            },
            "required": ["path", "content"],
        },
    )

    # ── edit_source: REQUIRES user confirmation before calling ──
    async def _edit_source(path: str, old_text: str, new_text: str) -> str:
        """[REQUIRES CONFIRMATION] Edit a file by replacing exact text. Ask the user before calling."""
        try:
            data = await _api_get("/admin/source/read", {"path": path})
            current = data["content"]
        except Exception:
            return f"Error: could not read {path}. Does it exist?"

        if old_text not in current:
            return f"Error: old_text not found in {path}"

        new_content = current.replace(old_text, new_text, 1)
        if new_content == current:
            return f"Error: replacement produced no change"

        resp = await _api_post("/admin/source/write", {
            "path": path, "content": new_content, "create_backup": True,
        })
        if resp.status_code != 200:
            return f"Error: {resp.json().get('detail', 'unknown error')}"
        result = resp.json()
        bkp = result.get("backup_path", "none")
        return f"{result['message']}. Backup: {bkp}"

    tools["edit_source"] = ToolInfo(
        name="edit_source",
        handler=_edit_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "old_text": {"type": "string", "description": "Exact text to find and replace (must match exactly)"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    )

    # ── delete_source: REQUIRES user confirmation before calling ──
    async def _delete_source(path: str, recursive: bool = False) -> str:
        """[REQUIRES CONFIRMATION] Delete a file or directory. Ask the user before calling."""
        resp = await _api_post("/admin/source/delete", {
            "path": path, "recursive": recursive,
        })
        if resp.status_code != 200:
            return f"Error: {resp.json().get('detail', 'unknown error')}"
        return resp.json()["message"]

    tools["delete_source"] = ToolInfo(
        name="delete_source",
        handler=_delete_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file or directory to delete"},
                "recursive": {"type": "boolean", "description": "Set true to delete directories", "default": False},
            },
            "required": ["path"],
        },
    )

    # ── run_command: REQUIRES user confirmation before calling ──
    async def _run_command(command: str, timeout: int = 30) -> dict:
        """[REQUIRES CONFIRMATION] Run a shell command. Ask the user before calling."""
        resp = await _api_post("/admin/source/exec", {
            "command": command, "timeout": timeout,
        })
        return resp.json()

    tools["run_command"] = ToolInfo(
        name="run_command",
        handler=_run_command,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
            },
            "required": ["command"],
        },
    )

    # ── restart_server: REQUIRES user confirmation before calling ──
    async def _restart_server() -> str:
        """[REQUIRES CONFIRMATION] Restart the web agent server. Ask the user before calling."""
        import httpx
        async with httpx.AsyncClient(base_url=BASE, timeout=5) as c:
            try:
                resp = await c.post("/api/v1/restart")
                return f"Server restarting: {resp.json().get('message', 'ok')}"
            except Exception as e:
                return f"Restart signal sent: {e}"

    tools["restart_server"] = ToolInfo(
        name="restart_server",
        handler=_restart_server,
        parameters={"type": "object", "properties": {}, "required": []},
    )

    logger.info("Injected filesystem tools (read/write/edit/delete/command/restart)")
