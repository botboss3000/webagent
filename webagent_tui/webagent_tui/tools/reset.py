"""Reset the linked webAgent install back to a clean state.

The in-app equivalent of the repo's ``reset_webagent.bat``: it wipes the running
app's **userbase** (the live database + the per-user generated pages) and, opt-in,
the app's **secrets**, **local logins**, **.env**, and **agent template JSONs**.
The database, the default ``admin/admin`` user, and the agents are recreated by
the app's own first-run migration on next start (provided the agent JSONs were kept).

**Backend-aware (active backend only).** Reset wipes whatever database
``app/db_connection.json`` selects:

* **Postgres-family** (``postgres`` / ``neon`` / ``gcp_cloud_sql``) — the app's real
  backend on this machine. Reset connects through the checkout's own virtualenv
  (so password/secret resolution matches the app exactly), drops the schema, and
  recreates it empty; the app rebuilds tables + seeds on next start. This wipe is
  **not backed up** — dropping the schema is irreversible. The stray SQLite files
  are left untouched (SQLite is not the active backend).
* **SQLite** — wipes ``local.db`` (+ its ``-journal``/``-wal``/``-shm``/
  ``.preprompt-bak`` sidecars and any stray root ``local.db``). These files ARE
  backed up first (unless ``backup`` is false), so a SQLite reset is reversible.

The generated pages dir (``visuals/users/``) is filesystem state, independent of
the backend, so it is always wiped (and backed up with the other files).

Safety: gated behind "Allow writes" like every mutating tool. The manager-tracked
server is stopped first, because a live server holds the database open (on Windows
an open SQLite file can't be deleted, and live Postgres sessions can block a schema
drop).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from ..env_probe import server_health
from .base import WRITES_DISABLED_MSG, ToolContext

# Postgres-family providers — these use the live Postgres backend, not SQLite.
_PG_PROVIDERS = {"postgres", "neon", "gcp_cloud_sql"}

# SQLite userbase files — processed only when SQLite is the active backend.
_USERBASE_FILES = [
    "app/db/local.db",
    "app/db/local.db-journal",
    "app/db/local.db-wal",
    "app/db/local.db-shm",
    "app/db/local.db.preprompt-bak",
    "local.db",
]
# Generated pages live on disk regardless of backend → always part of the wipe.
_USERBASE_DIRS = ["visuals/users"]

# Opt-in groups (mirrors reset_webagent.bat). These are filesystem files,
# independent of which database backend is active.
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


# In-checkout script that drops the active Postgres schema. Run via the checkout's
# own venv with cwd=project_root, so .env + db_connection.json + the secrets vault
# resolve exactly as the running app would resolve them. Prints one status line:
#   OK:schema:<schema>@<host>:<port>/<db>   full DROP SCHEMA + recreate succeeded
#   OK:tables:<n>:<schema>@...              fell back to dropping <n> tables
#   NOTPG:<provider>                        active backend isn't Postgres
#   ERR:<stage>:<detail>                    something went wrong
_PG_WIPE_SCRIPT = r"""
import sys
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(Path('.env')))
except Exception:
    pass
try:
    from app.db.connection_config import load_config
    from app.db import _resolve_pg_password, _PG_PROVIDERS
    from app.db.postgres_backend import make_conninfo
except Exception as e:
    print('ERR:import:' + repr(e)); sys.exit(3)
cfg = load_config()
prov = getattr(cfg, 'provider', 'sqlite')
if prov not in _PG_PROVIDERS:
    print('NOTPG:' + str(prov)); sys.exit(2)
schema = getattr(cfg, 'schema', None) or 'public'
loc = '%s@%s:%s/%s' % (schema, cfg.host, cfg.port, cfg.database)
try:
    import psycopg
except Exception as e:
    print('ERR:psycopg:' + repr(e)); sys.exit(3)
conninfo = make_conninfo(cfg, _resolve_pg_password(cfg))
try:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        try:
            conn.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)
            conn.execute('CREATE SCHEMA "%s"' % schema)
            try:
                conn.execute('GRANT ALL ON SCHEMA "%s" TO CURRENT_USER' % schema)
                conn.execute('GRANT ALL ON SCHEMA "%s" TO public' % schema)
            except Exception:
                pass
            print('OK:schema:' + loc)
        except Exception:
            # Managed Postgres may forbid dropping the schema itself; fall back
            # to dropping every table in it (data is what matters; the app
            # recreates tables with CREATE TABLE IF NOT EXISTS on next start).
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
            ).fetchall()
            n = 0
            for (t,) in rows:
                conn.execute('DROP TABLE IF EXISTS "%s"."%s" CASCADE' % (schema, t))
                n += 1
            print('OK:tables:%d:%s' % (n, loc))
except Exception as e:
    print('ERR:connect:' + repr(e)); sys.exit(4)
"""


def _active_provider(root: Path) -> str:
    """Which database backend the app will use — mirrors
    ``app.db.connection_config.load_config`` enough to pick the wipe path.

    Postgres-family providers are selected by ``app/db_connection.json`` and take
    precedence over the cloud/local ``db_mode.json`` switch (see ``get_db``)."""
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        return os.environ.get("WEBAGENT_DB_PROVIDER", "sqlite") or "sqlite"
    try:
        data = json.loads((root / "app" / "db_connection.json").read_text(encoding="utf-8"))
        return str(data.get("provider") or "sqlite")
    except (OSError, json.JSONDecodeError, ValueError):
        return "sqlite"


async def _wipe_postgres(root: Path) -> tuple[bool, str]:
    """Drop + recreate the active Postgres schema via the checkout's venv.
    Returns (ok, detail). No backup — dropping the schema is irreversible."""
    from .server import _NO_WINDOW, _venv_python

    py = _venv_python(root)
    if py is None:
        return False, ("no virtualenv (.venv) in the checkout, so Postgres can't be "
                       "reached to wipe it — build the environment (setup_environment) first")

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(py), "-c", _PG_WIPE_SCRIPT],
            cwd=str(root), capture_output=True, text=True, timeout=120, **_NO_WINDOW,
        )

    try:
        proc = await asyncio.to_thread(_run)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"failed to launch the Postgres wipe: {e}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    last = out.splitlines()[-1] if out else ""
    if last.startswith("OK"):
        return True, last
    return False, (last or err or f"exit {proc.returncode}")


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
    """Reset the linked webAgent install. Wipes the userbase (active database +
    generated pages) always; the other groups only when their flag is set.

    The database wipe targets whatever backend ``app/db_connection.json`` selects:
    a Postgres-family backend is wiped by dropping its schema (NOT backed up —
    irreversible), and SQLite is wiped by removing its files (backed up unless
    ``backup`` is false). ``backup`` also governs the generated-pages dir and the
    opt-in file groups. Mutating."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    if ctx.project_root is None:
        return "Error: no checkout linked — link or install one first."
    root = ctx.project_root

    # 1) Stop the manager-tracked server so the DB isn't held open (SQLite file
    #    locks on Windows; live Postgres sessions that could block a schema drop).
    from . import server as srv
    if srv._read_pidinfo():
        await srv.server_stop(ctx)
        await asyncio.sleep(1.0)
    still_up = await server_health(srv.PORT) == "running"

    is_pg = _active_provider(root) in _PG_PROVIDERS

    # 2) Build the filesystem work list. SQLite userbase files are processed only
    #    when SQLite is the active backend (Postgres leaves them untouched). The
    #    generated-pages dir is always wiped (disk state, backend-independent).
    files: list[str] = []
    dirs = list(_USERBASE_DIRS)
    if not is_pg:
        files += _USERBASE_FILES
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

    # 3) Wipe the live Postgres schema (no backup), if Postgres is the backend.
    pg_ok: bool | None = None
    pg_detail = ""
    if is_pg:
        pg_ok, pg_detail = await _wipe_postgres(root)

    ok, failed, skipped = results["ok"], results["failed"], results["skipped"]
    ctx.audit("reset_app", {
        "backend": "postgres" if is_pg else "sqlite",
        "pg_ok": pg_ok, "backup": backup, "clear_secrets": clear_secrets,
        "clear_users": clear_users, "delete_env": delete_env, "delete_agents": delete_agents,
    }, (not failed) and (pg_ok is not False), f"ok={len(ok)} failed={len(failed)} skipped={len(skipped)}")

    lines: list[str] = []
    if is_pg:
        if pg_ok:
            lines.append(f"[reset] Postgres wiped ({pg_detail}) — not backed up (irreversible).")
        else:
            lines.append(f"[reset] Postgres wipe FAILED: {pg_detail}")
            if still_up:
                lines.append("  → the webAgent server is still running and may be holding "
                             "connections open. Stop it (Server ▸ Kill), then reset again.")

    verb = "backed up" if backup else "deleted"
    lines.append(f"[reset] {verb} {len(ok)} file item(s); "
                 f"{len(failed)} failed; {len(skipped)} not present.")
    if ok:
        lines.append("  removed: " + ", ".join(ok))
    if failed:
        lines.append("  FAILED (still present): " + "; ".join(failed))
        if still_up and not is_pg:
            lines.append("  → the webAgent server is still running and is holding the "
                         "database open. Stop it (Server ▸ Kill), then reset again.")
    if backup_dir is not None and ok:
        lines.append(f"  backup: {backup_dir}")
    lines.append("Next start recreates the database, the default admin/admin user, and the "
                 "agents from their templates" +
                 (" — but you deleted the agent JSONs, so the app will start with zero agents."
                  if delete_agents else "."))
    return "\n".join(lines)
