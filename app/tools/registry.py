"""
Dynamic tool registry for webAgent agent.

Replaces the hardcoded tools with database-backed dynamic loading.
Provides the real implementation of create_tool (writes Python code to the DB)
and tool rating/scoring utilities.
"""

import json
import logging
import re
from typing import Dict, Any, List, Optional

from app.db import get_db

logger = logging.getLogger(__name__)


# ── Tool code safety scanner ─────────────────────────────────────────────────
# Prevents create_tool from injecting code that touches the agent's own
# codebase (filesystem, shell, DB, code execution). Only external-facing
# tools (HTTP APIs, data processing, etc.) are allowed.

BLOCKED_IMPORTS = [
    "os",
    "subprocess",
    "shutil",
    "pathlib",
    "sqlite3",
    "builtins",
    "importlib",
    "aiofiles",
    "io",
    "tempfile",
    "fileinput",
    "filecmp",
    "zipfile",
    "tarfile",
    "gzip",
    "bz2",
    "lzma",
]

# Regex: matches `import os`, `import   os`, `from os import`, `from   os import`
_IMPORT_RE = re.compile(
    r"""(?:^|[\n;])\s*(?:import|from)\s+([a-zA-Z0-9_.]+)""",
    re.MULTILINE,
)

# Inline/call patterns: os., subprocess., shutil.rmtree, open(, exec(, eval(
_CALL_PATTERNS = [
    r"""\bos\.""",
    r"""\bsubprocess\.""",
    r"""\bshutil\.""",
    r"""\bpathlib\.""",
    r"""\bsqlite3\.""",
    r"""\bbuiltins\.""",
    r"""\bimportlib\.""",
    r"""\baiofiles\.""",
    r"""\bopen\s*\(""",
    r"""\bexec\s*\(""",
    r"""\beval\s*\(""",
    r"""\bcompile\s*\(""",
    r"""__import__\s*\(""",
]
_CALL_RE = re.compile("|".join(_CALL_PATTERNS))


def _check_tool_code_safety(code: str) -> Optional[str]:
    """
    Scan tool code for dangerous imports and patterns.
    Returns error message string if unsafe, None if safe.
    """
    # Check imports
    for match in _IMPORT_RE.finditer(code):
        mod = match.group(1).split(".")[0]  # top-level module name
        if mod in BLOCKED_IMPORTS:
            return (
                f"Unsafe import rejected: '{match.group(0).strip()}' "
                f"(module '{mod}' provides filesystem/shell/DB access). "
                f"create_tool is for external tools only (HTTP APIs, data processing). "
                f"Allowed imports exclude: os, subprocess, shutil, pathlib, sqlite3, "
                f"builtins, importlib, aiofiles, io, tempfile, and archive modules."
            )

    # Check call patterns (e.g. `os.`, `open(`, `exec(`)
    call_match = _CALL_RE.search(code)
    if call_match:
        return (
            f"Unsafe pattern rejected: '{call_match.group().strip()}' "
            f"provides filesystem/shell/DB access. "
            f"create_tool is for external tools only."
        )

    return None


# ── Built-in tool: create_tool ────────────────────────────────────────────────

async def create_tool(
    name: str,
    description: str,
    parameters: dict,
    code: str,
    user_id: str = "",
) -> dict:
    """
    [REQUIRES CONFIRMATION] Create or update a tool in the tools table.

    Stores the full executable code directly. If a tool with the same name
    exists for this user, it is replaced (upsert by name + created_by).

    ⚠ You MUST ask the user to review the code and get explicit approval before calling this.

    Args:
        name: Tool identifier (e.g. 'check_email')
        description: What the tool does (shown to model)
        parameters: JSON Schema describing tool inputs
        code: Full Python async function code
        user_id: Creator's user ID (injected by loader)

    Returns:
        dict with tool name, id, status
    """
    if not user_id:
        return {"status": "error", "message": "user_id is required"}

    # Safety scan — reject code that touches the agent's codebase
    safety_error = _check_tool_code_safety(code)
    if safety_error:
        logger.warning("Tool '%s' rejected by safety scanner: %s", name, safety_error[:80])
        return {"status": "blocked", "tool_name": name, "message": safety_error}

    client = get_db().get_raw_client()

    # Check if tool already exists for this user
    existing = (
        client.table("tools")
        .select("id")
        .eq("name", name)
        .eq("created_by", user_id)
        .limit(1)
        .execute()
    )

    params_json = json.dumps(parameters) if isinstance(parameters, dict) else parameters

    if existing.data:
        # Update existing tool
        tool_id = existing.data[0]["id"]
        client.table("tools").update({
            "code": code,
            "description": description,
            "parameters": params_json,
            "updated_at": "now()",
        }).eq("id", tool_id).execute()
        logger.info(f"Updated tool {name} for user {user_id}")
        return {
            "status": "success",
            "tool_name": name,
            "tool_id": tool_id,
            "message": f"Tool '{name}' updated. You can now call it.",
        }
    else:
        # Insert new tool
        row = {
            "name": name,
            "code": code,
            "description": description,
            "parameters": params_json,
            "language": "python",
            "status": "active",
            "created_by": user_id,
        }
        resp = client.table("tools").insert(row).execute()

        if resp.data and len(resp.data) > 0:
            created = resp.data[0]
            logger.info(f"Created tool {name} for user {user_id}")
            return {
                "status": "success",
                "tool_name": name,
                "tool_id": created["id"],
                "message": f"Tool '{name}' created. You can now call it.",
            }
        else:
            logger.error(f"Failed to insert tool {name}")
            return {"status": "error", "message": f"Failed to create tool '{name}'"}


async def browser_open(url: str) -> str:
    """
    Open a browser to a given URL, typically for authentication flows.

    Args:
        url: The URL to open
    """
    import webbrowser
    webbrowser.open(url)
    return f"Browser opened to {url}"


# ── Tool rating / scoring ─────────────────────────────────────────────────────


async def get_tool_rating(tool_name: str, user_id: Optional[str] = None) -> dict:
    """
    Get the success rating for a tool based on interactions metadata.

    Rating = positive / (positive + negative) where:
      - positive = successful executions
      - negative = failed executions

    Args:
        tool_name: The tool name to score
        user_id: Optional — if provided, scoped to that user

    Returns:
        dict with tool_name, total, positive, negative, rating (0-100)
    """
    import json
    client = get_db().get_raw_client()
    query = (
        client.table("interactions")
        .select("metadata")
        .eq("role", "tool")
        .eq("tool_name", tool_name)
    )
    if user_id:
        query = query.eq("user_id", user_id)

    try:
        resp = query.execute()
        rows = resp.data or []
        total = 0
        positive = 0
        for r in rows:
            try:
                meta = json.loads(r["metadata"]) if r.get("metadata") else {}
                total += 1
                if meta.get("success"):
                    positive += 1
            except (json.JSONDecodeError, TypeError):
                pass
        negative = total - positive
        rating = round(100.0 * positive / total, 1) if total > 0 else None

        return {
            "tool_name": tool_name,
            "total_executions": total,
            "positive": positive,
            "negative": negative,
            "rating": rating,
        }
    except Exception as e:
        logger.error(f"Error getting tool rating for {tool_name}: {e}")
        return {"tool_name": tool_name, "total_executions": 0, "positive": 0, "negative": 0, "rating": None}


async def get_top_tools(user_id: Optional[str] = None, min_executions: int = 5, limit: int = 10) -> List[dict]:
    """
    Get the highest-rated tools.

    Args:
        user_id: Optional user scope
        min_executions: Minimum number of executions to qualify
        limit: Max results

    Returns:
        List of {tool_name, total_executions, positive, negative, rating}
    """
    import json
    client = get_db().get_raw_client()
    query = client.table("interactions").select("tool_name, metadata").eq("role", "tool")
    if user_id:
        query = query.eq("user_id", user_id)

    try:
        resp = query.execute()
        rows = resp.data or []

        stats: Dict[str, dict] = {}
        for r in rows:
            name = r.get("tool_name", "?")
            if not name:
                continue
            if name not in stats:
                stats[name] = {"tool_name": name, "total": 0, "positive": 0}
            stats[name]["total"] += 1
            try:
                meta = json.loads(r["metadata"]) if r.get("metadata") else {}
                if meta.get("success"):
                    stats[name]["positive"] += 1
            except (json.JSONDecodeError, TypeError):
                pass

        scored = []
        for s in stats.values():
            if s["total"] < min_executions:
                continue
            negative = s["total"] - s["positive"]
            rating = round(100.0 * s["positive"] / s["total"], 1)
            scored.append({
                "tool_name": s["tool_name"],
                "total_executions": s["total"],
                "positive": s["positive"],
                "negative": negative,
                "rating": rating,
            })

        scored.sort(key=lambda x: x["rating"], reverse=True)
        return scored[:limit]
    except Exception as e:
        logger.error(f"Error getting top tools: {e}")
        return []


# ── Tool listing ──────────────────────────────────────────────────────────────


async def list_user_tools(user_id: str) -> List[Dict[str, Any]]:
    """List all active tools for a user."""
    client = get_db().get_raw_client()
    try:
        resp = (
            client.table("tools")
            .select("id, name, description, parameters, language, status, created_at, updated_at")
            .eq("created_by", user_id)
            .eq("status", "active")
            .order("created_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.error(f"Error listing tools for {user_id}: {e}")
        return []
