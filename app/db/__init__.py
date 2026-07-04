"""
Database backend factory for WebAgent.

Provides get_db() which returns the current StorageBackend instance
(Cloud/Supabase or Local/SQLite) based on the persisted mode.

Mode is stored in a small mode.json file so it survives restarts.
"""

import json
import logging
import os
from typing import Optional
from app.db.interface import StorageBackend

logger = logging.getLogger(__name__)

_db_instance: Optional[StorageBackend] = None
_db_mode: Optional[str] = None  # "cloud" or "local"

# Where we persist the mode choice
_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE_FILE = os.path.join(_AGENT_DIR, "db_mode.json")


def _load_saved_mode() -> Optional[str]:
    """Load the persisted mode from disk."""
    try:
        if os.path.exists(_MODE_FILE):
            with open(_MODE_FILE, "r") as f:
                data = json.load(f)
                return data.get("mode")
    except Exception as e:
        logger.warning("Failed to load db_mode.json: %s", e)
    return None


def _save_mode(mode: str) -> None:
    """Persist the current mode to disk (skip when env-locked for Cloud Run)."""
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        logger.info("Config is env-locked; skipping db_mode.json write")
        return
    try:
        with open(_MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        logger.warning("Failed to save db_mode.json: %s", e)


def get_mode() -> str:
    """
    Get the current database mode.

    Returns:
        "cloud" or "local"

    Env override: WEBAGENT_DB_MODE wins when WEBAGENT_CONFIG_SOURCE=env.
    """
    global _db_mode
    if _db_mode is None:
        if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
            env_mode = os.environ.get("WEBAGENT_DB_MODE", "").strip().lower()
            if env_mode in ("cloud", "local"):
                _db_mode = env_mode
                return _db_mode
        _db_mode = _load_saved_mode() or "cloud"
    return _db_mode


def set_db_mode(mode: str) -> None:
    """
    Switch the database mode.
    
    This resets the cached backend instance. The next call to get_db()
    will return the new backend. Mode is persisted to disk.
    
    Args:
        mode: "cloud" or "local"
    
    Raises:
        ValueError: If mode is not "cloud" or "local"
    """
    global _db_mode, _db_instance
    if mode not in ("cloud", "local"):
        raise ValueError(f"Invalid mode '{mode}'. Must be 'cloud' or 'local'.")
    
    _db_mode = mode
    _db_instance = None  # Force re-create on next get_db()
    _save_mode(mode)
    logger.info("Database mode switched to '%s'", mode)


def _maybe_wrap_encryption(backend: StorageBackend) -> StorageBackend:
    """
    If the active encryption level is non-trivial, wrap the backend in an
    EncryptedStorageBackend so sensitive fields are transparently encrypted
    on write and decrypted on read.
    """
    try:
        from app.encryption import get_level, get_encryption
        level = get_level()
        if level == "none":
            return backend
        from app.db.interface import EncryptedStorageBackend
        enc = get_encryption()
        wrapped = EncryptedStorageBackend(backend, enc)
        logger.info("Wrapped backend with EncryptedStorageBackend (level=%s)", level)
        return wrapped
    except Exception as e:
        logger.warning("Encryption wrap failed (%s); using backend unwrapped", e)
        return backend


def _maybe_wrap_hybrid(remote: StorageBackend) -> StorageBackend:
    """When the hybrid local-first layer is switched on, wrap the reachable remote
    (Postgres) authority in a HybridBackend that also holds a local SQLite hot
    store. Off by default and only ever applied to a live remote — a local-only
    install never reaches here, so it is a strict no-op there.

    Ordering: the encryption shim wraps the RESULT of this (Enc(Hybrid(local,
    remote))), so field-level secret encryption still sees a single backend and
    authoritative auth_element reads resolve against the remote."""
    try:
        from app.db.hybrid import hybrid_enabled, HybridBackend
        if not hybrid_enabled():
            return remote
        from app.db.local import LocalBackend
        local = LocalBackend()
        logger.info("Wrapped remote backend with HybridBackend (local-first hot store)")
        return HybridBackend(local, remote)
    except Exception as e:
        logger.warning("Hybrid wrap failed (%s); using remote unwrapped", e)
        return remote


def reset_db_instance() -> None:
    """Drop the cached backend so the next get_db() rebuilds it.

    Used after changing the encryption level so the wrapper is re-evaluated.
    """
    global _db_instance
    _db_instance = None


_PG_PROVIDERS = ("postgres", "neon", "gcp_cloud_sql")


# ── Connection health ────────────────────────────────────────────────────────
# Records whether the LAST get_db() build actually reached the database the admin
# signed into, or silently fell back to THIS device's local SQLite copy. Surfaced
# to the Database & Devices page so a fallback shows a clear "couldn't reach the
# shared database" banner instead of failing quietly (the local copy always
# probes green, which is exactly what made the failure invisible before).
_conn_health: dict = {
    "ok": True, "degraded": False,
    "intended": None, "actual": None,
    "reason": None, "detail": None, "message": None,
}

_DEGRADED_MESSAGES = {
    "no_password": (
        "Couldn't reach the shared database — there's no saved password for it on "
        "this device, so the app is running on this device's local copy. Open the "
        "Database page and sign in (Activate) to reconnect."
    ),
    "no_credentials": (
        "Couldn't reach the shared database — its connection details aren't set on "
        "this device, so the app is running on this device's local copy. Open the "
        "Database page and sign in (Activate) to reconnect."
    ),
    "connect_failed": (
        "Couldn't reach the shared database, so the app is running on this device's "
        "local copy. Check the connection and sign in again on the Database page."
    ),
}


def get_connection_health() -> dict:
    """Return a copy of the current connection-health record.

    ``degraded`` is True when the saved config points at a shared remote database
    but get_db() couldn't reach it and dropped to local SQLite. ``message`` is a
    ready-to-show one-liner; ``detail`` carries the underlying error."""
    return dict(_conn_health)


def is_remote_db() -> bool:
    """True only when the app is ACTUALLY connected to a remote shared database
    (Postgres / Supabase), False for local SQLite — including a degraded fallback
    to the local copy. Used to lengthen background-poller intervals on remote
    backends, where every round-trip is a costly network hop."""
    actual = _conn_health.get("actual")
    if actual:
        return actual != "local"
    # Health not yet recorded (pre-get_db) — infer from the saved provider.
    try:
        from app.db.connection_config import load_config
        prov = getattr(load_config(), "provider", "sqlite")
        return prov in _PG_PROVIDERS or prov == "supabase"
    except Exception:
        return False


def _mark_healthy(actual: str) -> None:
    _conn_health.update(ok=True, degraded=False, intended=actual, actual=actual,
                        reason=None, detail=None, message=None)


def _mark_degraded(intended: str, reason: str, detail: str = "") -> None:
    msg = _DEGRADED_MESSAGES.get(reason, _DEGRADED_MESSAGES["connect_failed"])
    _conn_health.update(ok=False, degraded=True, intended=intended, actual="local",
                        reason=reason, detail=(detail or "")[:300], message=msg)
    logger.error("DB connection degraded: intended=%s reason=%s detail=%s",
                 intended, reason, (detail or "")[:200])


def _resolve_pg_password(cfg) -> str:
    """Resolve the Postgres password synchronously at cold start.

    Order:
      1. WEBAGENT_DB_PASSWORD env (the warm/activate path stashes it here).
      2. The database-independent credential store (OS keyring / file fallback).
         This is the durable source: it survives self-restarts and does NOT live
         inside the database we are trying to reach. We try the saved
         ``password_secret_key`` and, if that is unset (older configs), the
         conventional ``db_password_<provider>`` name so such configs self-heal.
      3. Best-effort read from the configurable secrets vault (legacy fallback).

    get_db() is synchronous, so steps 1-2 use no event loop at all; only the
    legacy step 3 needs the async vault and is guarded against a running loop."""
    pw = os.environ.get("WEBAGENT_DB_PASSWORD", "")
    if pw:
        return pw

    key = getattr(cfg, "password_secret_key", None)
    provider = getattr(cfg, "provider", "") or ""
    try:
        from app.db import cred_store
        for k in (key, f"db_password_{provider}" if provider else None):
            if not k:
                continue
            val = cred_store.get_secret(k)
            if val:
                return val
    except Exception as e:
        logger.debug("Could not resolve PG password from cred_store: %s", e)

    if key:
        try:
            import asyncio
            from app.secrets import get_secrets
            coro = get_secrets().get(key)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                resolved = asyncio.run(coro)
                if resolved:
                    return resolved
        except Exception as e:
            logger.debug("Could not resolve PG password from secrets: %s", e)
    return pw


def _hydrate_supabase_env_sync() -> None:
    """Populate SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY from the saved config and
    the database-independent credential store, if not already in the env. Lets a
    Supabase backend cold-start (after a self-restart) without re-activation."""
    try:
        from app.db.connection_config import load_config
        cfg = load_config()
    except Exception:
        return
    if getattr(cfg, "provider", "") != "supabase":
        return
    if not os.environ.get("SUPABASE_URL") and getattr(cfg, "supabase_url", None):
        os.environ["SUPABASE_URL"] = cfg.supabase_url
    if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        try:
            from app.db import cred_store
            key_name = getattr(cfg, "supabase_service_key_secret", None) or "supabase_service_key"
            val = cred_store.get_secret(key_name)
            if val:
                os.environ["SUPABASE_SERVICE_ROLE_KEY"] = val
        except Exception as e:
            logger.debug("Could not hydrate Supabase key from cred_store: %s", e)


def active_postgres_conninfo() -> Optional[str]:
    """If the active backend is a Postgres-family provider, return a libpq
    conninfo string for it (host/port/db/user/password/ssl). Else None.

    Used by tools that need a direct Postgres connection (e.g. the admin DB
    viewer) without going through the storage interface."""
    try:
        from app.db.connection_config import load_config
        cfg = load_config()
    except Exception:
        return None
    if getattr(cfg, "provider", "sqlite") not in _PG_PROVIDERS:
        return None
    try:
        from app.db.postgres_backend import make_conninfo
        return make_conninfo(cfg, _resolve_pg_password(cfg))
    except Exception as e:
        logger.debug("Could not build active PG conninfo: %s", e)
        return None


def _maybe_build_postgres():
    """Return a live PostgresBackend if the saved connection config selects a
    Postgres-family provider; otherwise None (fall through to mode-based logic)."""
    try:
        from app.db.connection_config import load_config
        cfg = load_config()
    except Exception as e:
        logger.debug("Could not load connection config: %s", e)
        return None
    if getattr(cfg, "provider", "sqlite") not in _PG_PROVIDERS:
        return None
    from app.db.postgres_backend import build_postgres_backend
    password = _resolve_pg_password(cfg)
    if not password:
        # No durable password on this device (the exact cause of the silent
        # local fallback) — record it so the page can say so plainly.
        _mark_degraded("postgres", "no_password",
                       "no saved password for the Postgres connection on this device")
        return None
    # Retry a TRANSIENT connection failure before giving up. Opening the pool at
    # boot briefly bursts several connections at the remote pooler; a momentary
    # reset ("server closed the connection unexpectedly" / WinError 10054), common
    # right after a fast restart while the pooler still counts the old connections,
    # would otherwise strand the whole server on an EMPTY local SQLite copy for its
    # entire life — the worst failure mode (silent wrong data), not a slow one. A
    # short backing-off retry lets the pooler drain and accept us. A genuinely
    # unreachable DB still falls through to the local copy after the attempts, with
    # the degraded banner, exactly as before.
    import time as _time
    last_err: Exception | None = None
    for _attempt in range(4):
        try:
            backend = build_postgres_backend(cfg, password=password)
            logger.info("Initialized PostgresBackend (provider=%s%s)", cfg.provider,
                        f", after {_attempt} retr{'y' if _attempt == 1 else 'ies'}" if _attempt else "")
            return backend
        except Exception as e:  # noqa: BLE001
            last_err = e
            if _attempt < 3:
                _delay = 1.5 * (2 ** _attempt)  # 1.5s, 3s, 6s
                logger.warning("PostgresBackend init attempt %d failed (%s); retrying in %.1fs",
                               _attempt + 1, e, _delay)
                _time.sleep(_delay)
    logger.error("PostgresBackend init failed after retries (%s); falling back to local", last_err)
    _mark_degraded("postgres", "connect_failed", str(last_err))
    return None


def get_control_db() -> StorageBackend:
    """
    Get the central/control storage backend instance — the one database the admin
    signed into. This is the single shared database in single-tenant mode, and the
    account/agent-catalog/billing plane in multi-tenant mode.

    Lazily creates the backend based on the current mode. Cached in _db_instance.

    Returns:
        StorageBackend implementation (SupabaseBackend or LocalBackend), possibly
        wrapped by EncryptedStorageBackend when encryption is enabled.
    """
    global _db_instance, _db_mode

    if _db_instance is None:
        # What did the admin sign into? The saved connection config is the source
        # of truth for the intended provider; the cloud/local mode is only the
        # fallback for older installs that never set an explicit provider.
        try:
            from app.db.connection_config import load_config
            _provider = (getattr(load_config(), "provider", "") or "").strip()
        except Exception:
            _provider = ""

        # 1) Postgres-family (postgres / neon / gcp_cloud_sql) — selected via the
        #    saved connection config; takes precedence over mode.
        if _provider in _PG_PROVIDERS:
            pg_backend = _maybe_build_postgres()  # marks health degraded on failure
            if pg_backend is not None:
                _mark_healthy("postgres")
                _db_instance = _maybe_wrap_encryption(_maybe_wrap_hybrid(pg_backend))
                return _db_instance
            # Signed into a shared Postgres DB but couldn't reach it → local copy.
            from app.db.local import LocalBackend
            logger.error("Falling back to LocalBackend (SQLite) — the shared Postgres "
                         "database is unreachable; see the Database page")
            _db_instance = _maybe_wrap_encryption(LocalBackend())
            return _db_instance

        # 2) Supabase (REST URL + service key). Cold start: the activate click
        #    stashes URL/key into the env, but a self-restart loses that — re-hydrate
        #    from the saved config + the DB-independent credential store first.
        if _provider == "supabase":
            _hydrate_supabase_env_sync()
            if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
                _mark_degraded("supabase", "no_credentials",
                               "Supabase URL or service key is not set on this device")
                from app.db.local import LocalBackend
                logger.error("Falling back to LocalBackend (SQLite) — Supabase credentials "
                             "missing on this device; see the Database page")
                _db_instance = _maybe_wrap_encryption(LocalBackend())
                return _db_instance
            try:
                from app.db.supabase import SupabaseBackend
                base = SupabaseBackend()
                _mark_healthy("supabase")
                logger.info("Initialized SupabaseBackend (Cloud)")
            except Exception as e:
                _mark_degraded("supabase", "connect_failed", str(e))
                from app.db.local import LocalBackend
                base = LocalBackend()
                logger.error("Falling back to LocalBackend (SQLite) — Supabase unreachable: %s", e)
            _db_instance = _maybe_wrap_encryption(base)
            return _db_instance

        # 3) Explicit single-device local SQLite — an intentional choice, not a
        #    fallback, so it stays healthy (no warning banner).
        if _provider == "sqlite":
            from app.db.local import LocalBackend
            _mark_healthy("local")
            logger.info("Initialized LocalBackend (SQLite)")
            _db_instance = _maybe_wrap_encryption(LocalBackend())
            return _db_instance

        # 4) No explicit provider saved — honour the legacy cloud/local mode.
        mode = get_mode()
        if mode == "local":
            from app.db.local import LocalBackend
            _mark_healthy("local")
            logger.info("Initialized LocalBackend (SQLite)")
            _db_instance = _maybe_wrap_encryption(LocalBackend())
            return _db_instance

        # Legacy cloud mode → Supabase via env.
        _hydrate_supabase_env_sync()
        if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
            logger.info(
                "Database mode is 'cloud' but Supabase credentials are not configured "
                "— using local backend until credentials are set in the Database tab"
            )
            # No remote was ever configured here → first-run local, NOT a fallback
            # from a known shared DB, so don't raise a false "couldn't reach" banner.
            from app.db.local import LocalBackend
            _mark_healthy("local")
            _db_instance = _maybe_wrap_encryption(LocalBackend())
            return _db_instance
        try:
            from app.db.supabase import SupabaseBackend
            base = SupabaseBackend()
            _mark_healthy("supabase")
            logger.info("Initialized SupabaseBackend (Cloud)")
        except Exception as e:
            _mark_degraded("supabase", "connect_failed", str(e))
            from app.db.local import LocalBackend
            base = LocalBackend()
            _db_mode = "local"
            logger.error("Fell back to LocalBackend (SQLite): %s", e)
        _db_instance = _maybe_wrap_encryption(base)

    return _db_instance


def get_db() -> StorageBackend:
    """
    The storage backend for the current request/turn.

    Single-tenant (default): returns the central control backend unchanged — every
    caller shares the one admin-configured database, exactly as before.

    Multi-tenant ON (App Settings → Multi-tenant data): returns a TenantRouterBackend
    that keeps the account/admin plane on the central database but routes each
    caller's interaction data (sessions, chats, memories, agents, secrets) to THAT
    user's own database. The ~90 existing call sites are unchanged: the router
    duck-types StorageBackend (see app/db/router.py).
    """
    control = get_control_db()
    try:
        from app.admin.settings import get_multi_tenant_enabled
        if not get_multi_tenant_enabled():
            return control
    except Exception:  # noqa: BLE001 — never let a settings read break DB access
        return control
    from app.db.router import TenantRouterBackend
    return TenantRouterBackend(control)


def get_data_db(user_id: Optional[str]) -> StorageBackend:
    """Explicit data-plane backend for a specific user — the user's own database
    when multi-tenant is on and they have one, else the central backend. Used by
    background jobs / raw-SQL paths that must target a KNOWN user rather than the
    ambient request caller. Safe in single-tenant mode (returns central)."""
    try:
        from app.admin.settings import get_multi_tenant_enabled
        if not get_multi_tenant_enabled():
            return get_control_db()
    except Exception:  # noqa: BLE001
        return get_control_db()
    from app.db.tenant import resolve_data_backend
    return resolve_data_backend(user_id)


async def get_db_stats() -> dict:
    """
    Get statistics about the current database.
    
    Returns:
        dict with mode and table row counts
    """
    db = get_db()
    raw = db.get_raw_client()
    
    tables = [
        "sessions", "interactions", "session_summaries", "agent_prompts",
        "memories", "memory_chunks",
        "tools", "agent_credentials", "skills",
    ]
    
    stats = {}
    for table in tables:
        try:
            result = raw.table(table).select("id", count="exact").limit(1).execute()
            # Supabase returns count in response, SQLite doesn't
            # Fallback: just count with a separate query
            if hasattr(result, 'count') and result.count is not None:
                stats[table] = result.count
            else:
                # Count all rows for local mode
                all_rows = raw.table(table).select("id").execute()
                stats[table] = len(all_rows.data) if all_rows.data else 0
        except Exception:
            stats[table] = -1  # Table doesn't exist or error
    
    # Try to get the actual file size for local mode
    db_path = ""
    if get_mode() == "local":
        try:
            from app.db.local import DEFAULT_DB_PATH
            db_path = DEFAULT_DB_PATH
            if os.path.exists(db_path):
                stats["db_size_bytes"] = os.path.getsize(db_path)
        except Exception:
            pass
    
    return {
        "mode": get_mode(),
        "backend": type(db).__name__,
        "tables": stats,
        "db_path": db_path,
    }
