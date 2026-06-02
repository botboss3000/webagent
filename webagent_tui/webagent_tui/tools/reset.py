"""Reset the linked webAgent install back to a clean state.

The in-app equivalent of the repo's ``reset_webagent.bat``: it wipes the running
app's **userbase** (the local SQLite DB + its sidecars and the per-user generated
pages) and, opt-in, the app's **secrets**, **local logins**, **.env**, and **agent
template JSONs**. The database, the default ``admin/admin`` user, and the agents are
recreated by the app's own first-run migration on next start (provided the agent
JSONs were kept).

Safety: gated behind "Allow writes" like every mutating tool, and by default it
**backs up** everything it removes to ``temp/reset-backup-<timestamp>/`` (mirroring
the original relative paths) so a reset is reversible. It stops the manager-tracked
server first, because a live server holds the DB open (on Windows an open file can't
be deleted).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from ..env_probe import server_health
from .base import WRITES_DISABLED_MSG, ToolContext

# Files that make up the **userbase** — always processed by a reset.
_USERBASE_FILES = [
    "app/db/local.db",
    "app/db/local.db-journal",
    "app/db/local.db-wal",
    "app/db/local.db-shm",
    "app/db/local.db.preprompt-bak",
    "local.db",
]
_USERBASE_DIRS = ["visuals/users"]

# Opt-in groups (mirrors reset_webagent.bat).
_SECRETS_FILES = [
    "provider.json",
    "app-settings.json",
    "scheduler_config.json",
    "app/db_mode.json",
    "app/pages_mode.json",
    "app/db_connection.json",
    "app/secrets_mode.json",
]
_USER_FILES = ["app/auth/users.json", "app/auth/users.json.bak"]
_ENV_FILES = [".env"]


def _process(root: Path, rel: str, backup_dir: Path | None,
             results: dict[str, list[str]]) -> None:
    """Back up (move) or delete one file/dir at ``root/rel``; record the outcome."""
    src = root / rel
    if not src.exists():
        results["skipped"].append(rel)
        return
    try:
        if backup_dir is not None:
            dst = backup_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        elif src.is_dir():
            shutil.rmtree(src)
        else:
            src.unlink()
        results["ok"].append(rel)
    except OSError as e:
        results["failed"].append(f"{rel} ({e})")


async def reset_app(
    ctx: ToolContext,
    backup: bool = True,
    clear_secrets: bool = False,
    clear_users: bool = False,
    delete_env: bool = False,
    delete_agents: bool = False,
) -> str:
    """Reset the linked webAgent install. Wipes the userbase (DB + generated pages)
    always; the other groups only when their flag is set. Backs up first unless
    ``backup`` is false. Mutating."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    if ctx.project_root is None:
        return "Error: no checkout linked — link or install one first."
    root = ctx.project_root

    # 1) Stop the manager-tracked server so the DB isn't held open.
    from . import server as srv
    if srv._read_pidinfo():
        await srv.server_stop(ctx)
        await asyncio.sleep(1.0)
    still_up = await server_health(srv.PORT) == "running"

    # 2) Build the work list.
    files = list(_USERBASE_FILES)
    dirs = list(_USERBASE_DIRS)
    if clear_secrets:
        files += _SECRETS_FILES
    if clear_users:
        files += _USER_FILES
    if delete_env:
        files += _ENV_FILES
    if delete_agents:
        agents_dir = root / "app" / "context" / "agents"
        if agents_dir.is_dir():
            files += [f"app/context/agents/{p.name}"
                      for p in sorted(agents_dir.glob("*.json"))]

    backup_dir: Path | None = None
    if backup:
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = root / "temp" / f"reset-backup-{ts}"

    results: dict[str, list[str]] = {"ok": [], "failed": [], "skipped": []}

    def _run() -> None:
        if backup_dir is not None:
            backup_dir.mkdir(parents=True, exist_ok=True)
        for rel in dirs + files:
            _process(root, rel, backup_dir, results)

    await asyncio.to_thread(_run)

    ok, failed, skipped = results["ok"], results["failed"], results["skipped"]
    ctx.audit("reset_app", {
        "backup": backup, "clear_secrets": clear_secrets, "clear_users": clear_users,
        "delete_env": delete_env, "delete_agents": delete_agents,
    }, not failed, f"ok={len(ok)} failed={len(failed)} skipped={len(skipped)}")

    verb = "backed up" if backup else "deleted"
    lines = [f"[reset] {verb} {len(ok)} item(s); "
             f"{len(failed)} failed; {len(skipped)} not present."]
    if ok:
        lines.append("  removed: " + ", ".join(ok))
    if failed:
        lines.append("  FAILED (still present): " + "; ".join(failed))
        if still_up:
            lines.append("  → the webAgent server is still running and is holding the "
                         "database open. Stop it (Server ▸ Kill), then reset again.")
    if backup_dir is not None and ok:
        lines.append(f"  backup: {backup_dir}")
    lines.append("Next start recreates the database, the default admin/admin user, and the "
                 "agents from their templates" +
                 (" — but you deleted the agent JSONs, so the app will start with zero agents."
                  if delete_agents else "."))
    return "\n".join(lines)
