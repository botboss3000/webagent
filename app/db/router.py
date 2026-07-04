"""
TenantRouterBackend — routes storage calls between the central control plane and
the current user's own data database (multi-tenant / bring-your-own-database).

Only used when App Settings → Multi-tenant data is ON. `app.db.get_db()` returns
one of these instead of the plain backend; single-tenant installs never see it.

How it routes
-------------
Each user's personal database is a COMPLETE, self-contained WebAgent database:
first-connect bootstrap seeds it with the schema AND the default agent templates
(app/db/postgres_backend.py `_init_db(seed=True)`), so agent resolution, sessions,
interactions, memories and the user's own secrets all live together in one DB with
their foreign keys intact (interactions.session_id → sessions(id), and
get_or_resolve_session_agent materialises the agent row beside the session). That
co-location is why the split is by *database*, not by table: we send almost every
call to the caller's own database and keep only the true ACCOUNT plane central.

The central control database keeps just the account/identity plane — the
`user_profiles` table (admin flag, per-user default agent, appearance) — so a user
can't self-grant admin inside their own database, and `is_user_admin` stays
authoritative. Billing/usage rows are also kept central, but those are written via
raw SQL and pinned to `get_control_db()` at their call sites, not routed here.

Mirrors the duck-typing pattern of EncryptedStorageBackend (app/db/interface.py):
it deliberately does NOT inherit the StorageBackend ABC (which would force us to
re-declare every abstract method); callers typed as StorageBackend are unaffected
because every attribute not found on the router is delegated via `__getattr__`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Methods that MUST hit the central control database. These are exactly the
# operations on the `user_profiles` table — the account/identity/admin plane.
# Everything else (sessions, interactions, agents, memories, skills, secrets,
# attachments, diagnostics, …) routes to the CURRENT caller's own database.
CONTROL_METHODS = frozenset({
    "get_user_profile",
    "upsert_user_profile",
    "is_user_admin",
    "set_user_admin",
    "get_user_appearance",
    "set_user_appearance",
    "merge_user_appearance",
    "get_user_default_agent_id",
    "set_user_default_agent",
    # Login/credential plane (user_accounts table) — the account store that was
    # formerly app/auth/users.json. Central so login/admin identity is one shared
    # authority across every instance, never a per-tenant copy a user could forge.
    "get_user_account_by_id",
    "get_user_account_by_username",
    "get_user_account_by_remember_token",
    "get_user_account_by_social",
    "list_user_accounts",
    "count_user_accounts",
    "create_user_account",
    "update_user_account",
    "delete_user_account",
})


class TenantRouterBackend:
    """Duck-typed StorageBackend that dispatches per method to the control backend
    or the current caller's data backend."""

    def __init__(self, control) -> None:
        # Instance attributes are resolved by normal lookup, so they never hit
        # __getattr__ below.
        self._control = control

    def _data(self):
        """The data backend for the CURRENT request/turn's verified caller. The
        caller id comes from the same contextvar the auth middleware sets
        (app/auth/identity.py); background/WS paths set it too (see Phase 5)."""
        from app.auth.identity import get_verified_caller_uid
        from app.db.tenant import resolve_data_backend
        return resolve_data_backend(get_verified_caller_uid())

    def __getattr__(self, name):
        # Only called for attributes NOT found normally (so _control / _data
        # above are never routed here).
        #
        # Everything except the account-plane CONTROL_METHODS routes to the
        # caller's data backend — INCLUDING raw-connection accessors
        # (`_get_conn`, `get_raw_client`). Those carry no plane intent and are
        # used mostly for a user's OWN data (e.g. the device worker's session
        # INSERT), so they must follow the data plane. The few genuinely central
        # raw writes (billing/usage_events, the admin dashboard) resolve
        # get_control_db() explicitly at their call sites instead of relying on
        # this router — see app/agent/loop.py `_record_billing_usage`.
        if name in CONTROL_METHODS:
            return getattr(self._control, name)
        return getattr(self._data(), name)
