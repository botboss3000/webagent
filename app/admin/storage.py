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
from app.pages_store import (
    get_mode as get_pages_mode,
    set_mode as set_pages_mode,
    list_modes as list_pages_modes,
    get_status as get_pages_status,
)
from app.encryption import (
    get_encryption,
    get_level as get_enc_level,
    set_level as set_enc_level,
    get_status as get_enc_status,
    list_levels as list_enc_levels,
)
from app.encryption.vault_keys import get_vault_keys
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


class PagesModeBody(BaseModel):
    requesting_user_id: str
    mode: str


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
        "pages": get_pages_status(),
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


# ── Routes: page store ─────────────────────────────────────────────────────


@router.get("/pages/status")
async def pages_status(requesting_user_id: str = Query(...)):
    await _require_admin(requesting_user_id)
    return get_pages_status()


@router.post("/pages/mode")
async def set_pages_store_mode(body: PagesModeBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.mode not in list_pages_modes():
        raise HTTPException(status_code=400, detail=f"Unknown mode {body.mode}")
    try:
        set_pages_mode(body.mode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mode": body.mode}


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


# ── Routes: encryption ──────────────────────────────────────────────────────


class EncLevelBody(BaseModel):
    requesting_user_id: str
    level: str
    settings: Optional[Dict[str, Any]] = None
    confirm: bool = False  # required when level == "none" (decrypts data)


class EncRotateDekBody(BaseModel):
    requesting_user_id: str
    user_id: str


class EncSimpleBody(BaseModel):
    requesting_user_id: str


def _enc_safety_check(level: str) -> Optional[str]:
    """
    Refuse to enable per-tenant encryption when wrapped DEKs would land in
    the very DB we're trying to protect (inline_db secrets + remote DB).
    Returns an error string if unsafe, else None.
    """
    if level not in ("field", "kms"):
        return None
    secrets_provider = get_secrets_mode()
    if secrets_provider != "inline_db":
        return None
    try:
        db_cfg = load_config()
        db_provider = (db_cfg.provider or "sqlite").lower()
    except Exception:
        db_provider = "sqlite"
    if db_provider == "sqlite":
        return None
    return (
        f"Refusing to enable '{level}' encryption: secrets vault is 'inline_db' while "
        f"the database backend is '{db_provider}'. Wrapped encryption keys would live "
        f"in the same remote DB they're protecting. Switch the secrets vault to "
        f"'os_keyring', 'gcp_secret_manager', or 'aws_secrets_manager' first."
    )


@router.get("/encryption/config")
async def encryption_config(requesting_user_id: str = Query(...)):
    """Return current encryption level + status + safety warnings."""
    await _require_admin(requesting_user_id)
    status = get_enc_status()
    secrets_provider = get_secrets_mode()
    try:
        db_cfg = load_config()
        db_provider = db_cfg.provider
    except Exception:
        db_provider = "sqlite"
    warn = None
    if secrets_provider == "inline_db" and db_provider != "sqlite":
        warn = (
            "Secrets vault is 'inline_db' but the database backend is remote. "
            "Per-tenant encryption keys would live in the same DB they protect. "
            "Switch the secrets vault before enabling encryption."
        )
    return {
        **status,
        "secrets_provider": secrets_provider,
        "db_provider": db_provider,
        "warning": warn,
    }


@router.post("/encryption/level")
async def encryption_set_level(body: EncLevelBody):
    """Switch encryption level and reset the cached DB wrapper."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.level not in list_enc_levels():
        raise HTTPException(status_code=400, detail=f"Unknown level {body.level}")

    err = _enc_safety_check(body.level)
    if err:
        raise HTTPException(status_code=400, detail=err)

    if body.level == "none" and not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "Switching to 'none' leaves existing encrypted rows undecryptable through the "
                "decorator path. Re-confirm with confirm=true after running "
                "/admin/storage/encryption/decrypt-all (which restores plaintext)."
            ),
        )

    try:
        set_enc_level(body.level, body.settings or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Force the DB factory to re-evaluate the wrapper.
    from app.db import reset_db_instance
    reset_db_instance()
    return {"ok": True, "level": body.level, "settings": body.settings or {}}


@router.post("/encryption/kek/generate")
async def encryption_generate_kek(body: EncSimpleBody):
    """Generate (or replace) the active KEK in the vault. Use only when none exists."""
    await _require_admin(body.requesting_user_id)
    vkm = get_vault_keys()
    existing = await vkm.get_kek("active")
    if existing is not None:
        return {
            "ok": False,
            "error": "An active KEK already exists. Use /encryption/kek/rotate to replace it.",
        }
    await vkm.generate_kek()
    return {"ok": True, "message": "Active KEK generated and stored in vault."}


@router.post("/encryption/kek/rotate")
async def encryption_rotate_kek(body: EncSimpleBody):
    """Rotate the KEK; re-wraps every DEK in the vault. Row data untouched."""
    await _require_admin(body.requesting_user_id)
    vkm = get_vault_keys()
    try:
        result = await vkm.rotate_kek()
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/encryption/dek/rotate")
async def encryption_rotate_dek(body: EncRotateDekBody):
    """Rotate one tenant's DEK and re-encrypt all of their rows."""
    await _require_admin(body.requesting_user_id)
    from app.encryption.migration import rotate_dek
    return await rotate_dek(body.user_id)


@router.post("/encryption/migrate")
async def encryption_migrate(body: EncSimpleBody):
    """Walk all tenants and encrypt any plaintext sensitive values. Idempotent."""
    await _require_admin(body.requesting_user_id)
    from app.encryption.migration import encrypt_all
    return await encrypt_all()


@router.post("/encryption/decrypt-all")
async def encryption_decrypt_all(body: EncSimpleBody):
    """Decrypt every encrypted row back to plaintext. Required before level='none'."""
    await _require_admin(body.requesting_user_id)
    from app.encryption.migration import decrypt_all
    return await decrypt_all()


@router.get("/encryption/test")
async def encryption_test(requesting_user_id: str = Query(...)):
    """End-to-end health check: encrypt + decrypt a probe value for a synthetic tenant."""
    await _require_admin(requesting_user_id)
    enc = get_encryption()
    return await enc.health()


@router.get("/encryption/tenants")
async def encryption_tenants(requesting_user_id: str = Query(...)):
    """List every tenant that has key material, with their active + total DEK versions."""
    await _require_admin(requesting_user_id)
    vkm = get_vault_keys()
    return {"tenants": await vkm.list_tenants()}
