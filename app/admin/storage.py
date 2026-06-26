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
from app.canvas_store import (
    get_mode as get_canvases_mode,
    set_mode as set_canvases_mode,
    list_modes as list_canvases_modes,
    get_status as get_canvases_status,
)
from app.attachments_store import (
    get_mode as get_attachments_mode,
    set_mode as set_attachments_mode,
    list_modes as list_attachments_modes,
    get_provider_config as get_attachments_provider_config,
    set_provider_config as set_attachments_provider_config,
    get_status as get_attachments_status,
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
    """Ensure uid is an admin. Matches the pattern in app/admin/users.py.

    Routes through the shared chokepoint so 'open' access mode grants the
    bootstrap admin to a tokenless tunnel caller; non-open modes still require a
    real DB admin id. See app.auth.identity.resolve_admin_uid."""
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


async def _test_supabase(supabase_url: Optional[str], service_key_plain: Optional[str]) -> dict:
    """Reachability check for the Supabase provider.

    Supabase connects over its REST API (Project URL + service-role key), NOT a
    raw Postgres TCP connection, so the generic asyncpg test does not apply. We
    ping the PostgREST root and confirm the service key is accepted.
    """
    url = (supabase_url or "").rstrip("/")
    if not url:
        return {"ok": False, "error": "Supabase requires a Project URL (e.g. https://xxxx.supabase.co)."}
    # Key from the form if entered, else the saved service-role key in the vault.
    key = service_key_plain
    if not key:
        try:
            saved = load_config()
            ref = saved.supabase_service_key_secret or "supabase_service_key"
            key = await get_secrets().get(ref)
        except Exception:
            key = None
    if not key:
        return {"ok": False, "error": "Supabase service-role key not provided and none saved in the vault. Enter the key and retry."}
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx not installed; cannot test Supabase reachability."}
    rest_root = f"{url}/rest/v1/"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(rest_root, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    except Exception as e:
        return {"ok": False, "error": f"Could not reach {rest_root}: {type(e).__name__}: {e}"}
    if resp.status_code < 400:
        return {"ok": True, "endpoint": rest_root, "status": resp.status_code,
                "note": "Supabase REST endpoint reachable and the service key was accepted."}
    if resp.status_code in (401, 403):
        return {"ok": False, "status": resp.status_code,
                "error": "Reached Supabase, but the service-role key was rejected. Check the key."}
    return {"ok": False, "status": resp.status_code,
            "error": f"Unexpected response {resp.status_code} from {rest_root}."}


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


class CanvasesModeBody(BaseModel):
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
        "canvases": get_canvases_status(),
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

    if cfg.provider == "supabase":
        # Supabase is REST (URL + service key), not raw Postgres — test the REST
        # endpoint, not a TCP connection it doesn't use.
        return await _test_supabase(body.supabase_url, body.supabase_service_key)

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

    # Raw Postgres family (postgres / aws_rds / gcp_cloud_sql / azure_postgres /
    # neon): all share the live asyncpg backend, routed purely by dialect.
    if cfg.dialect == "postgres":
        # Resolve the password from the vault and stash it in the process env so
        # both this activation and cold restarts (which re-read the vault) work.
        if cfg.password_secret_key:
            pw = await get_secrets().get(cfg.password_secret_key)
            if pw:
                os.environ["WEBAGENT_DB_PASSWORD"] = pw
        try:
            from app.db import reset_db_instance, get_db, get_db_stats
            reset_db_instance()
            backend = get_db()  # builds PostgresBackend (raises/falls back on failure)
            if type(backend).__name__ not in ("_PostgresBackend",) and not hasattr(backend, "_pool"):
                # get_db fell back (bad creds / unreachable) — surface the problem.
                return JSONResponse(status_code=502, content={
                    "ok": False,
                    "error": "Postgres activation failed — check host/credentials and that the server is reachable.",
                })
            return {"ok": True, "provider": cfg.provider, "stats": await get_db_stats()}
        except Exception as e:
            return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})

    # MySQL / other: not wired as a live backend yet.
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "error": (
                f"Provider '{cfg.provider}' can be tested and bootstrapped but is not yet "
                "wired as a live backend."
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


# ── Routes: canvas store ─────────────────────────────────────────────────────


@router.get("/canvases/status")
async def canvases_status(requesting_user_id: str = Query(...)):
    await _require_admin(requesting_user_id)
    return get_canvases_status()


@router.post("/canvases/mode")
async def set_canvases_store_mode(body: CanvasesModeBody):
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.mode not in list_canvases_modes():
        raise HTTPException(status_code=400, detail=f"Unknown mode {body.mode}")
    try:
        set_canvases_mode(body.mode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mode": body.mode}


# ── Routes: attachment store ───────────────────────────────────────────────


class AttachmentsModeBody(BaseModel):
    requesting_user_id: str
    mode: str


class AttachmentsProviderConfigBody(BaseModel):
    requesting_user_id: str
    provider: str
    config: Dict[str, Any] = {}


@router.get("/attachments/status")
async def attachments_status(requesting_user_id: str = Query(...)):
    """Return the active attachment backend + persisted provider configs + counts."""
    await _require_admin(requesting_user_id)
    status = get_attachments_status()
    # Add per-backend attachment counts so admins can see where existing files live.
    counts: Dict[str, int] = {m: 0 for m in status.get("available", [])}
    try:
        from app.db import get_db
        db = get_db()
        try:
            raw = db.get_raw_client()
            res = raw.table("attachments").select("storage_provider").execute()
            for row in (res.data or []):
                prov = (row or {}).get("storage_provider") or "local"
                counts[prov] = counts.get(prov, 0) + 1
        except Exception:
            # Backends without a generic raw client / select fall through silently.
            pass
    except Exception:
        pass
    status["counts"] = counts
    return status


@router.post("/attachments/mode")
async def set_attachments_store_mode(body: AttachmentsModeBody):
    """Switch the active attachment backend (admin only, global)."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.mode not in list_attachments_modes():
        raise HTTPException(status_code=400, detail=f"Unknown mode {body.mode}")
    try:
        set_attachments_mode(body.mode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mode": body.mode}


@router.post("/attachments/provider-config")
async def set_attachments_provider_cfg(body: AttachmentsProviderConfigBody):
    """Persist provider-specific config (bucket, region, prefix, etc.)."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    if body.provider not in list_attachments_modes():
        raise HTTPException(status_code=400, detail=f"Unknown provider {body.provider}")
    try:
        set_attachments_provider_config(body.provider, body.config or {})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "provider": body.provider, "config": get_attachments_provider_config(body.provider)}


@router.post("/attachments/test")
async def test_attachments_backend(body: AttachmentsModeBody):
    """Construct the configured backend and write+read+delete a probe object."""
    await _require_admin(body.requesting_user_id)
    if body.mode not in list_attachments_modes():
        raise HTTPException(status_code=400, detail=f"Unknown mode {body.mode}")
    from app.attachments_store import get_store_for_provider
    try:
        store = get_store_for_provider(body.mode)
    except Exception as e:
        return {"ok": False, "error": f"Could not construct backend: {e}"}

    if body.mode == "browser":
        return {"ok": True, "note": "Browser backend stores bytes in the client; nothing to test server-side."}

    probe = b"webagent-attachments-probe"
    try:
        result = await store.store(
            user_id="__probe__",
            file_bytes=probe,
            filename="probe.txt",
            mime_type="text/plain",
        )
        path = result["storage_path"]
        got = await store.read(path)
        await store.delete(path)
        if got != probe:
            return {"ok": False, "error": "Probe round-trip mismatch."}
        return {"ok": True, "storage_path": path, "url_sample": result.get("public_url", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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

    # Refuse not-yet-wired levels (full_db / kms placeholders, or a method whose
    # optional dependency is missing) loudly. Without this the level would save,
    # then get_encryption() would silently fall back to NoEncryption — a green
    # "success" that leaves data unencrypted (false sense of security).
    from app.encryption import probe_level
    probe_err = probe_level(body.level, body.settings or {})
    if probe_err:
        return JSONResponse(status_code=501, content={
            "ok": False,
            "error": f"Encryption level '{body.level}' is not available yet: {probe_err}",
        })

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


# ── Full-database (SQLCipher) encryption at rest — opt-in, per-database ────────
# Whole-file encryption of each SQLite database (main/vault/logs/recordings/wiki),
# keyed by a per-file key held in the keyring vault. Distinct from the per-tenant
# 'field' level above (which only encrypts the credential secret column). The
# file migration runs at startup; toggling here sets intent + needs a restart.
# See app/db/db_crypto.py.

class FullDbEncBody(BaseModel):
    requesting_user_id: str
    db_id: str
    enabled: bool


@router.get("/encryption/full-db/status")
async def full_db_status(requesting_user_id: str = Query(...)):
    """Per-database at-rest encryption state + whether it can be enabled here."""
    await _require_admin(requesting_user_id)
    from app.db import db_crypto
    return db_crypto.status()


@router.post("/encryption/full-db/set")
async def full_db_set(body: FullDbEncBody):
    """Turn whole-file encryption on/off for one database. Applied on next restart."""
    await _require_admin(body.requesting_user_id)
    if _env_locked():
        raise HTTPException(status_code=403, detail="Config is env-locked.")
    from app.db import db_crypto
    if body.db_id not in db_crypto.DB_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown database '{body.db_id}'")
    if body.enabled:
        ok, reason = db_crypto.can_enable()
        if not ok:
            # Refuse loudly (engine missing / no keyring) instead of a fake success.
            return JSONResponse(status_code=501, content={"ok": False, "error": reason})
        # Mint the per-file key now so the keyring is ready and any error surfaces
        # here rather than mid-migration at boot.
        try:
            from app.db import db_keys
            db_keys.get_or_create_raw_key(body.db_id)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": f"Key setup failed: {e}"})
    db_crypto.set_enabled(body.db_id, body.enabled)
    return {
        "ok": True,
        "db_id": body.db_id,
        "enabled": body.enabled,
        "restart_required": True,
        "message": (
            "Will be encrypted at rest on the next server restart."
            if body.enabled else
            "Will be decrypted back to plaintext on the next server restart."
        ),
        "status": db_crypto.status(),
    }


# ── Config library: write the default config files from in-code templates ─────
# Deterministic seed (no LLM), admin-triggered, preview-first, never overwrites.
# Backs the "Config Files" row in the Data Management → Maintenance group.
# See app/util/config_seed.py.

class ConfigSeedBody(BaseModel):
    requesting_user_id: str


@router.get("/config-library/plan")
async def config_library_plan(requesting_user_id: str = Query(...)):
    """Preview which default config files a seed run would create vs skip."""
    await _require_admin(requesting_user_id)
    from app.util.config_seed import plan
    return plan()


@router.post("/config-library/seed")
async def config_library_seed(body: ConfigSeedBody):
    """Write the default for every missing config file (never overwrites existing)."""
    await _require_admin(body.requesting_user_id)
    from app.util.config_seed import seed_missing
    return seed_missing()
