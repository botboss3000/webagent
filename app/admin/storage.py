"""
Storage admin API: /admin/storage/*

Endpoints for the Storage modal in the Config UI:
  - DB provider config + test + bootstrap + activate
  - Secrets vault provider switch + test
  - Data migration export / import (JSON dump)

All endpoints are admin-only. The is_admin check is performed inside each
handler so unauthenticated probes get a 403, not a 500.
"""

import json
import logging
import os
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from app.db.connection_config import (
    DBConnectionConfig,
    PROVIDERS as DB_PROVIDERS,
    PROVIDER_META,
    load_config,
    save_config,
)
from app.secrets import (
    get_secrets,
    get_mode as get_secrets_mode,
    set_mode as set_secrets_mode,
    list_providers as list_secret_providers,
    get_secrets_status,
)
from app.db.schema import render_sqlite, render_postgres, render_mysql, DIALECTS
from app.db.schema.tables import TABLE_ORDER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/storage", tags=["admin-storage"])


# ── Helpers ─────────────────────────────────────────────────────────────────


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


async def _require_admin(uid: str) -> None:
    """Ensure uid is an admin. Matches the pattern in app/admin/users.py."""
    if not uid:
        raise HTTPException(status_code=401, detail="Missing requesting_user_id")
    from app.db import get_db
    db = get_db()
    is_admin = await db.is_user_admin(uid)
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin required")


# ── Models ──────────────────────────────────────────────────────────────────


class DBConfigBody(BaseModel):
    requesting_user_id: str
    provider: str
    host: Optional[str] = None
    port: Optional[int] = None
    database: str = ""
    username: Optional[str] = None
    password: Optional[str] = None  # plaintext, stored to secrets backend on save
    password_secret_key: Optional[str] = None  # alternative: reuse an existing key
    ssl_mode: str = "require"
    schema_name: str = "public"
    supabase_url: Optional[str] = None
    supabase_service_key: Optional[str] = None  # plaintext, stored to vault
    options: Dict[str, Any] = {}


class SecretsModeBody(BaseModel):
    requesting_user_id: str
    provider: str


class ActivateBody(BaseModel):
    requesting_user_id: str


class SchemaSQLBody(BaseModel):
    requesting_user_id: str
    dialect: str = "postgres"


# ── Routes: config + status ─────────────────────────────────────────────────


@router.get("/config")
async def get_storage_config(requesting_user_id: str = Query(...)):
    """Return current DB config + secrets config + provider lists."""
    await _require_admin(requesting_user_id)

    cfg = load_config()
    return {
        "env_locked": _env_locked(),
        "db": {
            "active": cfg.to_dict(),
            "providers": list(DB_PROVIDERS),
            "provider_meta": PROVIDER_META,
        },
        "secrets": get_secrets_status(),
    }


# ── Routes: DB provider operations ──────────────────────────────────────────


@router.post("/db/test")
async def test_db_connection(body: DBConfigBody):
    """Test a connection without saving config."""
    await _require_admin(body.requesting_user_id)

    cfg = DBConnectionConfig(
        provider=body.provider,
        host=body.host,
        port=body.port,
        database=body.database,
        username=body.username,
        ssl_mode=body.ssl_mode,
        schema=body.schema_name,
        supabase_url=body.supabase_url,
        options=body.options,
    )
    errs = cfg.validate()
    if errs:
        return {"ok": False, "error": "Validation: " + "; ".join(errs)}

    # Resolve password from body (plain) — preferred during test, never saved unless /db/config called.
    password = body.password
    if not password and body.password_secret_key:
        password = await get_secrets().get(body.password_secret_key)

    if cfg.provider == "sqlite":
        # Verify file path is writable
        from app.db.local import DEFAULT_DB_PATH
        path = cfg.database or DEFAULT_DB_PATH
        try:
            d = os.path.dirname(path) or "."
            return {"ok": os.access(d, os.W_OK), "path": path, "writable": os.access(d, os.W_OK)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if cfg.dialect == "postgres":
        from app.db.postgres_backend import test_connection as pg_test
        return await pg_test(cfg, password=password)

    # MySQL not supported yet
    return {"ok": False, "error": f"Provider '{cfg.provider}' connection test not yet implemented."}


@router.post("/db/schema-sql")
async def get_schema_sql(body: SchemaSQLBody):
    """Return DDL string rendered for the requested dialect."""
    await _require_admin(body.requesting_user_id)
    if body.dialect not in DIALECTS:
        raise HTTPException(status_code=400, detail=f"Unknown dialect; must be one of {DIALECTS}")
    if body.dialect == "sqlite":
        return {"dialect": body.dialect, "ddl": render_sqlite()}
    if body.dialect == "postgres":
        return {"dialect": body.dialect, "ddl": render_postgres()}
    return {"dialect": body.dialect, "ddl": render_mysql()}


@router.post("/db/bootstrap")
async def bootstrap_schema(body: DBConfigBody):
    """Connect to the configured remote DB and CREATE TABLE all tables."""
    await _require_admin(body.requesting_user_id)

    cfg = DBConnectionConfig(
        provider=body.provider,
        host=body.host, port=body.port,
        database=body.database, username=body.username,
        ssl_mode=body.ssl_mode, schema=body.schema_name,
        supabase_url=body.supabase_url, options=body.options,
    )
    password = body.password
    if not password and body.password_secret_key:
        password = await get_secrets().get(body.password_secret_key)

    if cfg.dialect == "postgres":
        from app.db.postgres_backend import bootstrap_schema as pg_bootstrap
        return await pg_bootstrap(cfg, password=password)

    if cfg.provider == "sqlite":
        # SQLite: bootstrap means initializing LocalBackend at the configured path
        try:
            from app.db.local import LocalBackend
            lb = LocalBackend(db_path=cfg.database or None)
            return {"ok": True, "message": f"SQLite initialized at {lb._db_path}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"Bootstrap not implemented for provider '{cfg.provider}'"}


@router.post("/db/config")
async def save_db_config(body: DBConfigBody):
    """
    Persist DB connection config (and store password in active secrets backend).
    Does NOT activate it — call /db/activate to switch the live backend.
    """
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked; edit deployment env vars to change.")

    cfg = DBConnectionConfig(
        provider=body.provider,
        host=body.host, port=body.port,
        database=body.database, username=body.username,
        ssl_mode=body.ssl_mode, schema=body.schema_name,
        supabase_url=body.supabase_url, options=body.options,
    )
    errs = cfg.validate()
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))

    # Persist password (if provided) to vault under a stable key per provider.
    secrets = get_secrets()
    if body.password:
        key = body.password_secret_key or f"db_password_{cfg.provider}"
        await secrets.set(key, body.password)
        cfg.password_secret_key = key
    elif body.password_secret_key:
        cfg.password_secret_key = body.password_secret_key

    if body.supabase_service_key and cfg.provider == "supabase":
        await secrets.set("supabase_service_key", body.supabase_service_key)
        cfg.supabase_service_key_secret = "supabase_service_key"

    save_config(cfg)
    return {"ok": True, "saved": cfg.to_dict()}


@router.post("/db/activate")
async def activate_db(body: ActivateBody):
    """
    Switch the live backend to the saved config. Refuses on backends not yet
    implemented (raw postgres data path is stubbed until ported).
    """
    await _require_admin(body.requesting_user_id)
    cfg = load_config()

    # Map provider → existing app.db backend mode
    if cfg.provider == "sqlite":
        from app.db import set_db_mode, get_db_stats
        set_db_mode("local")
        return {"ok": True, "mode": "local", "stats": await get_db_stats()}

    if cfg.provider == "supabase":
        # Hydrate env vars from saved config so SupabaseBackend.__init__ can read them
        if cfg.supabase_url:
            os.environ["SUPABASE_URL"] = cfg.supabase_url
        if cfg.supabase_service_key_secret:
            secret = await get_secrets().get(cfg.supabase_service_key_secret)
            if secret:
                os.environ["SUPABASE_SERVICE_ROLE_KEY"] = secret
        from app.db import set_db_mode, get_db_stats
        set_db_mode("cloud")
        return {"ok": True, "mode": "cloud", "stats": await get_db_stats()}

    # Raw postgres / mysql / cloud SQL: data path not yet implemented
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "error": (
                f"Provider '{cfg.provider}' can be tested and bootstrapped but is not yet "
                "wired as a live backend. The full data-method port is pending. "
                "Use Supabase or SQLite for live workloads in the meantime."
            ),
        },
    )


# ── Routes: secrets vault ───────────────────────────────────────────────────


@router.get("/secrets/status")
async def secrets_status(requesting_user_id: str = Query(...)):
    await _require_admin(requesting_user_id)
    return get_secrets_status()


@router.post("/secrets/mode")
async def set_secrets_provider(body: SecretsModeBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.provider not in list_secret_providers():
        raise HTTPException(status_code=400, detail=f"Unknown provider {body.provider}")
    try:
        set_secrets_mode(body.provider)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "provider": body.provider}


@router.post("/secrets/test")
async def test_secrets(requesting_user_id: str = Query(...)):
    await _require_admin(requesting_user_id)
    backend = get_secrets()
    return await backend.test_connection()


# ── Routes: data migration ──────────────────────────────────────────────────


@router.post("/migrate/export")
async def export_data(requesting_user_id: str = Query(...)):
    """
    Stream a JSON dump of all tables from the current active backend.
    Caller saves the response to a file; that file is the migration payload.
    """
    await _require_admin(requesting_user_id)
    from app.db.migration import iter_export_json
    return StreamingResponse(
        iter_export_json(),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="webagent-export.json"'
        },
    )


@router.post("/migrate/import")
async def import_data(request: Request, requesting_user_id: str = Query(...)):
    """
    Load a JSON dump into the current active backend.
    Body: the previously-exported JSON document.
    """
    await _require_admin(requesting_user_id)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be valid JSON")
    from app.db.migration import import_payload
    result = await import_payload(payload)
    return result


@router.get("/migrate/info")
async def migrate_info(requesting_user_id: str = Query(...)):
    """Return table order + estimated row counts from current backend."""
    await _require_admin(requesting_user_id)
    from app.db import get_db
    db = get_db()
    raw = db.get_raw_client()
    counts = {}
    for tbl in TABLE_ORDER:
        try:
            res = raw.table(tbl).select("id").execute()
            counts[tbl] = len(res.data or [])
        except Exception:
            counts[tbl] = None
    return {"tables": TABLE_ORDER, "counts": counts}
