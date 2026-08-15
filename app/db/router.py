"""
TenantRouterBackend — routes storage calls between the central control plane and
the current user's own data database (multi-tenant / bring-your-own-database).

Only used when App Settings → User BYOD is ON. `app.db.get_db()` returns
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
import inspect
import json
import uuid

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

# Temporary compatibility dispatch used during the v2 cutover.  New code should
# request an explicit plane handle; this list exists so old StorageBackend call
# sites keep working while app.db becomes authoritative table by table.
APP_METHODS = CONTROL_METHODS | frozenset({
    # The three scoped secret vaults are attached to the app backend.  Keeping
    # these calls on that already-encrypted handle prevents the plane router
    # from exposing raw ``enc:v1:`` ciphertext through an unwrapped user store.
    "auth_element_get",
    "auth_element_set",
    "auth_element_list",
    "auth_element_delete",
    "find_user_by_oauth_account",
    "get_default_template",
    "seed_agent_templates",
    "list_agent_templates",
    "update_agent_template_fields",
    "background_leader_acquire",
    "background_leader_release",
    "device_heartbeat",
    "list_devices",
    "delete_device",
    "set_device_override",
    "enqueue_device_job",
    "claim_device_jobs",
    "get_device_job",
    "finish_device_job",
    "reclaim_expired_device_jobs",
})

# Operations whose authority is one per-agent database. Methods that aggregate
# the fleet or create a new authority are implemented explicitly below; the
# remainder are dispatched by their ``agent_id`` argument.
AGENT_METHODS = frozenset({
    "copy_defaults_to_agent", "list_slots", "resolve_prompts", "assemble_prompt",
    "upsert_slot", "delete_slot", "reset_overrides", "upsert_override",
    "delete_override", "replace_slots", "get_agent_by_id", "get_agent_skills",
    "set_agent_skills", "get_agent_tool_modes", "set_agent_tool_mode",
    "seed_agent_tool_modes", "get_agent_ability_modes", "get_agent_discovery_default",
    "set_agent_discovery_default", "set_agent_ability_mode", "get_agent_ability_access",
    "set_agent_ability_access", "get_agent_skill_modes", "set_agent_skill_mode",
    "fetch_agent_with_context", "fetch_agent_by_id_with_context",
    "increment_agent_turn_count", "get_max_turn_count", "save_agent_as_template",
    "upsert_agent_to_template", "update_agent_fields", "get_agent_connections",
    "upsert_agent_connection", "get_agent_soft_abilities", "upsert_agent_soft_ability",
    "delete_agent_soft_ability", "get_agent_abilities", "upsert_agent_ability",
    "delete_agent_ability", "get_agent_byo_creds", "get_agent_roles",
    "set_agent_authorized", "add_agent_member", "add_agent_admin",
    "agent_data_source_list", "agent_data_source_attach", "agent_data_source_update",
    "agent_data_source_detach", "list_clone_agents", "delete_all_ability_connections",
    "trash_clone_agent", "restore_clone_agent", "trash_custom_agent",
    "restore_custom_agent", "delete_clone_agent", "delete_custom_agent",
})


class PlaneRouterBackend:
    """Hard-cutover facade over app, user, and per-agent authorities.

    There is deliberately no legacy backend member. An unclassified method goes
    to the current user's authority; app and agent methods are explicitly
    dispatched to their owning files.
    """

    def __init__(self, app) -> None:
        self._app = app

    def _user(self, explicit_user_id=None):
        from app.db import get_user_db
        user_id = explicit_user_id
        if not user_id:
            try:
                from app.auth.identity import get_verified_caller_uid
                user_id = get_verified_caller_uid()
            except Exception:
                user_id = None
        if not user_id:
            from app.db.local import get_db_user_context
            user_id = get_db_user_context()
        return get_user_db(str(user_id or "admin"))

    @staticmethod
    def _bound_argument(name, args, kwargs, argument):
        if argument in kwargs:
            return kwargs[argument]
        from app.db.local import LocalBackend
        function = getattr(LocalBackend, name)
        try:
            bound = inspect.signature(function).bind_partial(None, *args, **kwargs)
            return bound.arguments.get(argument)
        except TypeError:
            return None

    def _agent(self, agent_id, *, parent_id=None):
        from app.db import get_agent_db
        return get_agent_db(str(agent_id), parent_id=parent_id)

    def _catalog_rows(self):
        conn = self._app._get_conn()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM agent_catalog ORDER BY created_at, agent_id"
            ).fetchall()]
        finally:
            conn.close()

    async def _catalog_agent(self, agent_id):
        return await self._agent(agent_id).get_agent_by_id(agent_id)

    async def get_agent_for_user(self, user_id: str):
        rows = await self.list_agents_for_user(user_id, view="active", include_templates=False)
        return rows[0] if rows else None

    async def fetch_agent_with_context(self, user_id: str, context_types=None):
        agent = await self.get_agent_for_user(user_id)
        if not agent:
            return None
        return await self._agent(agent["id"]).fetch_agent_by_id_with_context(
            agent["id"], context_types=context_types, user_id=user_id
        )

    async def get_or_resolve_session_agent(
        self, session_id: str, user_id: str, template_id=None,
    ):
        user = self._user(user_id)
        agent_id = await user.get_session_agent_id(session_id)
        if agent_id:
            found = await self._agent(agent_id).fetch_agent_by_id_with_context(
                agent_id, user_id=user_id
            )
            if found:
                return found
        resolved_template = template_id or "default"
        if resolved_template == "default":
            from app.api.agents import provision_default_agent
            resolved = await provision_default_agent(self, user_id)
            if resolved is None:
                return None
        else:
            resolved = await self.resolve_agent(user_id, resolved_template)
        if resolved.get("status") == "template":
            resolved = await self.create_custom_agent(
                user_id=user_id,
                name=resolved.get("name") or "WebAgent",
                template_id=resolved_template,
            )
        agent_id = resolved.get("id")
        if not agent_id:
            return resolved
        await user.bind_session_to_agent(session_id, agent_id)
        if not await user.is_session_participant(session_id, agent_id, "agent"):
            await user.add_session_participant(session_id, agent_id, "agent")
        if not await user.is_session_participant(session_id, user_id, "user"):
            await user.add_session_participant(session_id, user_id, "user")
        return await self._agent(agent_id).fetch_agent_by_id_with_context(
            agent_id, user_id=user_id
        )

    async def find_default_agent(self, user_id: str):
        for row in await self.list_agents_for_user(user_id, view="active", include_templates=False):
            if row.get("template_id") == "default":
                return row
        return None

    async def resolve_agent(self, user_id: str, template_id: str):
        for row in await self.list_agents_for_user(user_id, view="active", include_templates=False):
            if row.get("template_id") == template_id:
                return row
        template = next(
            (row for row in await self._app.list_agent_templates(include_admin=True)
             if row.get("id") == template_id),
            None,
        )
        if template is None:
            raise ValueError(f"No agent template found for id '{template_id}'")
        result = dict(template)
        result.update(status="template", template_id=template_id)
        return result

    async def list_agents_for_user(
        self, user_id: str, include_admin: bool = False, view: str = "active",
        include_templates: bool = True,
    ):
        result = []
        if include_templates and view == "active":
            for template in await self._app.list_agent_templates(include_admin=include_admin):
                item = dict(template)
                item.update(source="template", is_user_default=0)
                result.append(item)
        for catalog in self._catalog_rows():
            def members(field):
                try:
                    value = json.loads(catalog.get(field) or "[]")
                    return value if isinstance(value, list) else []
                except Exception:
                    return []
            if user_id not in (members("admin_users") + members("member_users") + members("authorized_users")):
                continue
            status = catalog.get("status") or "active"
            if view == "clones" and status != "clone":
                continue
            if view == "bin" and status != "trashed":
                continue
            if view == "active" and status in {"clone", "clone_trashed", "trashed"}:
                continue
            try:
                authority = await self._catalog_agent(catalog["agent_id"])
            except Exception as exc:
                # A stale catalog projection must not make the entire roster (and
                # therefore login) return HTTP 500. Reconciliation can repair or
                # remove the projection; meanwhile omit only the unavailable row.
                logger.warning(
                    "Skipping unavailable agent authority %s: %s",
                    catalog["agent_id"], exc,
                )
                continue
            if authority:
                authority["source"] = "custom"
                result.append(authority)
        profile = await self._app.get_user_profile(user_id)
        default_id = (profile or {}).get("default_agent_id")
        for item in result:
            item["is_user_default"] = int(bool(default_id and item.get("id") == default_id))
        return result

    async def _create_agent(self, method: str, args, kwargs, *, parent_id=None):
        kwargs = dict(kwargs)
        # Most agents get a UUID, but app-level singleton agents deliberately
        # supply a stable ID. Discarding it creates a random private authority
        # while callers continue looking up the fixed ID.
        agent_id = str(kwargs.get("agent_id") or uuid.uuid4())
        kwargs["agent_id"] = agent_id
        result = await getattr(self._agent(agent_id, parent_id=parent_id), method)(*args, **kwargs)
        await self._refresh_catalog(agent_id, parent_id=parent_id)
        return result

    async def create_agent_for_user(self, *args, **kwargs):
        result = await self._create_agent("create_agent_for_user", args, kwargs)
        user_id = self._bound_argument("create_agent_for_user", args, kwargs, "user_id")
        if user_id:
            await self._app.set_user_default_agent(user_id, result["id"])
        return result

    async def create_custom_agent(self, *args, **kwargs):
        return await self._create_agent("create_custom_agent", args, kwargs)

    async def create_clone_agent(self, *args, **kwargs):
        parent_id = self._bound_argument("create_clone_agent", args, kwargs, "master_agent_id")
        return await self._create_agent("create_clone_agent", args, kwargs, parent_id=parent_id)

    async def list_clone_agents(self, owner_user_id: str):
        return await self.list_agents_for_user(
            owner_user_id, include_templates=False, view="clones"
        )

    async def get_all_connections_by_type(self, connection_type: str):
        result = []
        for row in self._catalog_rows():
            conn = self._agent(row["agent_id"])._get_conn()
            try:
                result.extend(dict(item) for item in conn.execute(
                    "SELECT * FROM agent_connections WHERE connection_type=? AND enabled=1",
                    (connection_type,),
                ).fetchall())
            finally:
                conn.close()
        return result

    async def reorder_agents(self, user_id: str, ordered_ids):
        changed = 0
        for index, agent_id in enumerate(ordered_ids):
            updated = await self._agent(agent_id).update_agent_fields(
                agent_id, user_id, {"sort_order": index}
            )
            if updated:
                changed += 1
                await self._refresh_catalog(agent_id)
        return changed

    async def backfill_agent_admin_users(self):
        return 0

    async def _delete_agent_authority(self, agent_id: str, user_id=None, session_ids=None):
        backend = self._agent(agent_id)
        authority = await backend.get_agent_by_id(agent_id)
        if not authority:
            return False
        if user_id:
            try:
                owners = json.loads(authority.get("admin_users") or "[]")
            except Exception:
                owners = []
            if user_id not in owners:
                return False

        from pathlib import Path
        from app.db.user_store import close_user_store
        user_root = Path(__file__).resolve().parents[2] / "data" / "user_data"
        for path in user_root.glob("*/*.db"):
            uid = path.parent.name
            close_user_store(uid)
            import sqlite3
            conn = sqlite3.connect(path)
            try:
                ids = [row[0] for row in conn.execute(
                    "SELECT id FROM sessions WHERE agent_id=?", (agent_id,)
                ).fetchall()]
                ids.extend(value for value in (session_ids or []) if value not in ids)
                for sid in ids:
                    for table in (
                        "interactions", "session_summaries", "session_summary_segments",
                        "session_runs", "session_interrupts", "session_notifications",
                        "attachments", "skill_executions",
                    ):
                        try:
                            conn.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
                        except Exception:
                            pass
                    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                for table in ("agent_automations", "agent_event_subscriptions", "automation_runs"):
                    try:
                        conn.execute(f"DELETE FROM {table} WHERE agent_id=?", (agent_id,))
                    except Exception:
                        pass
                conn.commit()
            finally:
                conn.close()

        try:
            meta = json.loads(authority.get("metadata") or "{}")
        except Exception:
            meta = {}
        parent_id = meta.get("clone_of") if isinstance(meta, dict) else None
        from app.db.agent_store import close_agent_store
        close_agent_store(agent_id)
        from app.db import _agent_db_instances
        _agent_db_instances.pop(str(Path(backend._db_path).resolve()), None)
        if parent_id:
            from app.agent_workspace import purge_subagent_home
            purge_subagent_home(parent_id, agent_id)
        else:
            from app.agent_workspace import purge_agent_home
            purge_agent_home(agent_id)
        conn = self._app._get_conn()
        try:
            conn.execute("DELETE FROM agent_catalog WHERE agent_id=?", (agent_id,))
            conn.commit()
        finally:
            conn.close()
        return True

    async def delete_custom_agent(self, agent_id: str, user_id: str):
        return await self._delete_agent_authority(agent_id, user_id=user_id)

    async def delete_clone_agent(self, agent_id: str, *, session_ids=None):
        return await self._delete_agent_authority(agent_id, session_ids=session_ids)

    async def _refresh_catalog(self, agent_id, *, parent_id=None):
        authority = await self._agent(agent_id, parent_id=parent_id).get_agent_by_id(agent_id)
        conn = self._app._get_conn()
        try:
            if not authority:
                conn.execute("DELETE FROM agent_catalog WHERE agent_id=?", (agent_id,))
            else:
                path = getattr(self._agent(agent_id, parent_id=parent_id), "_db_path", "")
                try:
                    admin_users = json.loads(authority.get("admin_users") or "[]")
                except (TypeError, json.JSONDecodeError):
                    admin_users = []
                owner_user_id = admin_users[0] if admin_users else None
                conn.execute(
                    """INSERT INTO agent_catalog
                       (agent_id,name,icon,status,template_id,owner_user_id,admin_users,member_users,
                        authorized_users,storage_ref,authority_revision,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         name=excluded.name,icon=excluded.icon,status=excluded.status,
                         template_id=excluded.template_id,
                         owner_user_id=COALESCE(agent_catalog.owner_user_id, excluded.owner_user_id),
                         admin_users=excluded.admin_users,
                         member_users=excluded.member_users,authorized_users=excluded.authorized_users,
                         storage_ref=excluded.storage_ref,
                         authority_revision=agent_catalog.authority_revision+1,
                         updated_at=excluded.updated_at""",
                    (agent_id, authority.get("name") or "", authority.get("icon"),
                     authority.get("status") or "active", authority.get("template_id"),
                     owner_user_id,
                     authority.get("admin_users") or "[]", authority.get("member_users") or "[]",
                     authority.get("authorized_users") or "[]", path,
                     authority.get("created_at"), authority.get("updated_at")),
                )
            conn.commit()
        finally:
            conn.close()

    def __getattr__(self, name):
        if name in APP_METHODS:
            return getattr(self._app, name)
        if name in AGENT_METHODS:
            async def call(*args, **kwargs):
                agent_id = self._bound_argument(name, args, kwargs, "agent_id")
                if not agent_id:
                    agent_id = self._bound_argument(name, args, kwargs, "master_agent_id")
                if not agent_id and args and isinstance(args[0], dict):
                    agent_id = args[0].get("agent_id")
                if not agent_id:
                    raise RuntimeError(f"Agent-plane operation {name} requires an agent id")
                result = await getattr(self._agent(agent_id), name)(*args, **kwargs)
                if name.startswith(("set_", "upsert_", "update_", "delete_", "trash_", "restore_", "add_", "increment_", "reset_", "replace_", "copy_")):
                    await self._refresh_catalog(agent_id)
                return result
            return call
        return getattr(self._user(), name)


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

    async def erase_user_owned_data(self, user_id: str):
        """Erase target-provider data plus any central trial/control-plane rows."""
        from app.db.tenant import resolve_data_backend
        backend = resolve_data_backend(user_id)
        eraser = getattr(backend, "erase_user_owned_data", None)
        if eraser is None:
            raise NotImplementedError(
                "The target user's data provider does not support scoped erasure"
            )
        counts = dict(await eraser(user_id))
        if backend is not self._control:
            central_eraser = getattr(self._control, "erase_user_owned_data", None)
            if central_eraser is None:
                raise NotImplementedError(
                    "The control provider does not support scoped erasure"
                )
            central_counts = await central_eraser(user_id)
            counts.update({
                f"control:{name}": count
                for name, count in central_counts.items()
            })
        return counts

    async def export_user_data(self, user_id: str):
        """Export target-provider data plus any central trial/control-plane rows."""
        from app.db.tenant import resolve_data_backend
        backend = resolve_data_backend(user_id)
        exporter = getattr(backend, "export_user_data", None)
        if exporter is None:
            raise NotImplementedError(
                "The target user's data provider does not support scoped export"
            )
        payload = await exporter(user_id)
        if backend is not self._control:
            central_exporter = getattr(self._control, "export_user_data", None)
            if central_exporter is None:
                raise NotImplementedError(
                    "The control provider does not support scoped export"
                )
            payload["control_plane"] = await central_exporter(user_id)
        return payload

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
