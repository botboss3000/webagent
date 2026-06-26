"""
DB connection configuration: provider, host, port, db name, secret reference.

Persisted to `db_connection.json` (non-secret fields only). The password is
fetched on demand from the active SecretsBackend using `password_secret_key`.

Providers (case matches UI dropdown):
  - sqlite             → uses LocalBackend, local.db file
  - supabase           → SupabaseBackend (Supabase REST + service-role key)
  - postgres           → raw Postgres via asyncpg (PostgresBackend)
  - mysql              → MySQL (future PostgresBackend extension)
  - aws_rds            → managed Postgres on Amazon RDS / Aurora (postgres dialect)
  - gcp_cloud_sql      → managed Postgres in GCP (uses postgres dialect)
  - azure_postgres     → managed Postgres on Azure (Flexible Server, postgres dialect)
  - neon               → Neon serverless Postgres

The managed-Postgres providers (aws_rds, gcp_cloud_sql, azure_postgres, neon)
are all ordinary Postgres endpoints — they share the raw asyncpg backend and
differ only in their UI label, default-field hints, and connection notes.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from app.util.config_io import safe_write_json

logger = logging.getLogger(__name__)

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_AGENT_DIR, "db_connection.json")

PROVIDERS = (
    "sqlite",
    "supabase",
    "postgres",
    "mysql",
    "aws_rds",
    "gcp_cloud_sql",
    "azure_postgres",
    "neon",
)

# Map provider → SQLAlchemy-style URL scheme + canonical dialect
PROVIDER_META = {
    "sqlite":         {"dialect": "sqlite",   "needs_host": False, "default_port": None},
    "supabase":       {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "postgres":       {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "mysql":          {"dialect": "mysql",    "needs_host": True,  "default_port": 3306},
    "aws_rds":        {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "gcp_cloud_sql":  {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "azure_postgres": {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
    "neon":           {"dialect": "postgres", "needs_host": True,  "default_port": 5432},
}


@dataclass
class DBConnectionConfig:
    provider: str = "sqlite"
    host: Optional[str] = None
    port: Optional[int] = None
    database: str = ""
    username: Optional[str] = None
    password_secret_key: Optional[str] = None  # key in SecretsBackend (e.g. "db_password_postgres")
    ssl_mode: str = "require"      # postgres only: disable | require | verify-ca | verify-full
    schema: str = "public"         # postgres schema name
    # Supabase-specific: REST URL + service role key (the supabase REST API path)
    supabase_url: Optional[str] = None
    supabase_service_key_secret: Optional[str] = None  # secret key for service-role JWT
    # Free-form per-provider tweaks (sslrootcert path, GCP instance connection name, etc.)
    options: dict = field(default_factory=dict)

    @property
    def dialect(self) -> str:
        return PROVIDER_META.get(self.provider, {}).get("dialect", "sqlite")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DBConnectionConfig":
        # Tolerate unknown keys (future fields)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def build_url(self, password: Optional[str] = None) -> str:
        """
        Build a SQLAlchemy-compatible async URL.

        password is supplied by caller (resolved from SecretsBackend) so that
        this method has no IO side effects.
        """
        prov = self.provider
        if prov == "sqlite":
            # use shipped local.db location by default
            from app.db.local import DEFAULT_DB_PATH
            db_path = self.database or DEFAULT_DB_PATH
            return f"sqlite+aiosqlite:///{db_path}"

        meta = PROVIDER_META.get(prov)
        if not meta:
            raise ValueError(f"Unknown provider: {prov}")

        port = self.port or meta["default_port"]
        host = self.host or ""
        db = self.database or "postgres"
        user = self.username or ""
        pw = password or ""

        if meta["dialect"] == "postgres":
            # asyncpg
            cred = f"{user}:{pw}@" if user else ""
            return f"postgresql+asyncpg://{cred}{host}:{port}/{db}"
        if meta["dialect"] == "mysql":
            cred = f"{user}:{pw}@" if user else ""
            return f"mysql+aiomysql://{cred}{host}:{port}/{db}"
        raise ValueError(f"Unsupported dialect for provider {prov}")

    def validate(self) -> list:
        """Return list of human-readable validation errors (empty if OK)."""
        errs = []
        meta = PROVIDER_META.get(self.provider)
        if not meta:
            errs.append(f"Unknown provider: {self.provider}")
            return errs
        # Supabase uses its REST URL + service key; raw host/user/db not required.
        if self.provider == "supabase":
            if not self.supabase_url:
                errs.append("supabase_url is required (the project REST URL)")
            return errs
        if meta["needs_host"]:
            if not self.host:
                errs.append("host is required")
            if not self.database:
                errs.append("database name is required")
            if not self.username:
                errs.append("username is required")
        return errs


# ── Persistence ──────────────────────────────────────────────────────────────

_cached: Optional[DBConnectionConfig] = None


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


def load_config() -> DBConnectionConfig:
    """Load active DB connection config from disk (or env in env-locked mode)."""
    global _cached
    if _cached is not None:
        return _cached

    if _env_locked():
        # Build from env vars: WEBAGENT_DB_PROVIDER, WEBAGENT_DB_HOST, etc.
        cfg = DBConnectionConfig(
            provider=os.environ.get("WEBAGENT_DB_PROVIDER", "sqlite"),
            host=os.environ.get("WEBAGENT_DB_HOST") or None,
            port=int(os.environ["WEBAGENT_DB_PORT"]) if os.environ.get("WEBAGENT_DB_PORT") else None,
            database=os.environ.get("WEBAGENT_DB_NAME", ""),
            username=os.environ.get("WEBAGENT_DB_USER") or None,
            password_secret_key=os.environ.get("WEBAGENT_DB_PASSWORD_KEY") or None,
            ssl_mode=os.environ.get("WEBAGENT_DB_SSLMODE", "require"),
            supabase_url=os.environ.get("SUPABASE_URL") or None,
            supabase_service_key_secret=os.environ.get("WEBAGENT_SUPABASE_KEY_SECRET") or None,
        )
        _cached = cfg
        return cfg

    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r") as f:
                data = json.load(f)
            cfg = DBConnectionConfig.from_dict(data)
        except Exception as e:
            logger.warning("Failed to read db_connection.json: %s", e)
            cfg = DBConnectionConfig()
    else:
        cfg = DBConnectionConfig()
    _cached = cfg
    return cfg


def save_config(cfg: DBConnectionConfig) -> None:
    """Persist active DB connection config to disk (unless env-locked)."""
    global _cached
    _cached = cfg
    if _env_locked():
        logger.warning("Config is env-locked; not writing db_connection.json")
        return
    try:
        safe_write_json(_CONFIG_FILE, cfg.to_dict())
    except Exception as e:
        logger.error("Failed to save db_connection.json: %s", e)
        raise
