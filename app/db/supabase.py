"""
Supabase storage backend for webAgent.

Implements StorageBackend using Supabase as the remote database.
Refactored from the original static-method SupabaseClient into an instance-based class.
"""

import os
from dotenv import load_dotenv

load_dotenv()

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from supabase import create_client, Client
from app.models.schemas import InteractionRecord
from app.db.interface import StorageBackend

logger = logging.getLogger(__name__)


# Mirror of LocalBackend._slots_from_template_data — module-level so the
# seeder can call it without a class reference. Kept in sync with the local
# version (legacy key map identical).
_VALID_MERGE_MODES = ("replace", "append")


def _supabase_slots_from_template_data(tpl: dict) -> List[dict]:
    raw_slots = tpl.get("slots")
    if isinstance(raw_slots, list) and raw_slots:
        out: List[dict] = []
        for i, s in enumerate(raw_slots):
            if not isinstance(s, dict):
                continue
            name = (s.get("slot_name") or "").strip()
            if not name:
                continue
            out.append({
                "slot_name": name,
                "order_index": int(s.get("order_index", (i + 1) * 10)),
                "lock": bool(s.get("lock", False)),
                "merge_mode": s.get("merge_mode") if s.get("merge_mode") in _VALID_MERGE_MODES else "replace",
                "content": s.get("content", "") or "",
            })
        return out
    legacy_map = [
        ("system",          "system_prompt",     10, True),
        ("agent",           "agent_prompt",      20, False),
        ("user",            "user_prompt",       30, False),
        ("skills",          "skills_prompt",     40, False),
        ("tasks",           "tasks_prompt",      50, False),
        ("misc",            "misc_prompt",       60, False),
        ("automation",      "automation_prompt", 70, False),
        ("bootstrap_tools", "bootstrap_tools",   90, True),
    ]
    out = []
    for slot_name, src_key, order, lock in legacy_map:
        content = tpl.get(src_key, "") or ""
        out.append({
            "slot_name": slot_name,
            "order_index": order,
            "lock": lock,
            "merge_mode": "replace",
            "content": content,
        })
    return out


class SupabaseBackend(StorageBackend):
    """Supabase implementation of StorageBackend."""

    def __init__(self):
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        self._client: Client = create_client(url, key)

    def get_raw_client(self) -> Client:
        """Return the underlying Supabase client for direct table queries."""
        return self._client

    # ---- Sessions ----

    async def assert_session_owned(self, user_id: str, session_id: str) -> None:
        res = (
            self._client.table("sessions")
            .select("id")
            .eq("id", session_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise PermissionError(
                f"Session {session_id} not found or not owned by user {user_id}"
            )

    async def upsert_session_summary(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        message_count: int,
        title: str = None,
    ) -> None:
        try:
            data = {
                "user_id": user_id,
                "session_id": session_id,
                "summary": summary,
                "message_count": message_count,
                "updated_at": "now()",
            }
            if title:
                data["title"] = title

            response = self._client.table("session_summaries").upsert(
                data, on_conflict="session_id"
            ).execute()
            if response.data:
                logger.debug("Upserted session summary for session %s", session_id)
            else:
                raise ValueError("No data returned after upsert")
        except Exception as e:
            logger.error("Error upserting session summary: %s", e)
            raise

    # ---- Interactions ----

    async def fetch_interactions(self, user_id: str, session_id: str) -> List[InteractionRecord]:
        try:
            await self.assert_session_owned(user_id, session_id)
            response = (
                self._client.table("interactions")
                .select("id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input, output, from_id, to_id, session_seq, turn_id, turn_seq, created_at")
                .eq("session_id", session_id)
                .order("created_at", desc=False)
                .execute()
            )
            interactions = [InteractionRecord(**row) for row in response.data]
            logger.debug(
                "Fetched %s interactions for user %s, session %s",
                len(interactions), user_id, session_id,
            )
            return interactions
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error fetching interactions: %s", e)
            raise

    async def insert_interaction(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        parent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        channel: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[str] = None,
        input_data: Optional[str] = None,
        output_data: Optional[str] = None,
        sender_id: Optional[str] = None,
        receiver_id: Optional[str] = None,
        session_seq: Optional[int] = None,
        turn_id: Optional[str] = None,
        turn_seq: Optional[int] = None,
        status: str = "complete",
    ) -> str:
        try:
            await self.assert_session_owned(user_id, session_id)
            data = {
                "session_id": session_id,
                "role": role,
                "content": content,
                "parent_id": parent_id,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "channel": channel,
                "source": source or 'user',
                "metadata": metadata,
                "input": input_data,
                "output": output_data,
                "from_id": sender_id,
                "to_id": receiver_id,
                "session_seq": session_seq,
                "turn_id": turn_id,
                "turn_seq": turn_seq,
                "status": status,
            }
            response = self._client.table("interactions").insert(data).execute()
            if response.data and len(response.data) > 0:
                interaction_id = response.data[0]["id"]
                logger.debug("Inserted interaction %s", interaction_id)
                return interaction_id
            raise ValueError("No data returned after insert")
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error inserting interaction: %s", e)
            raise

    async def insert_interactions_batch(self, rows: List[Dict[str, Any]]) -> List[str]:
        """Bulk-insert interactions via a single PostgREST request.

        Each row dict may contain any column. PostgREST accepts an array body
        and inserts in one transaction. Caller is responsible for session
        ownership.
        Returns inserted ids in the order Supabase returns them.
        """
        if not rows:
            return []
        try:
            import uuid as _uuid_mod
            payload: List[dict] = []
            for r in rows:
                d = {
                    "id": r.get("id") or str(_uuid_mod.uuid4()),
                    "session_id": r["session_id"],
                    "role": r.get("role", "tool"),
                    "content": r.get("content", ""),
                    "parent_id": r.get("parent_id"),
                    "tool_name": r.get("tool_name"),
                    "tool_call_id": r.get("tool_call_id"),
                    "channel": r.get("channel"),
                    "source": r.get("source") or "user",
                    "metadata": r.get("metadata"),
                    "input": r.get("input"),
                    "output": r.get("output"),
                    "from_id": r.get("from_id"),
                    "to_id": r.get("to_id"),
                    "session_seq": r.get("session_seq"),
                    "turn_id": r.get("turn_id"),
                    "turn_seq": r.get("turn_seq"),
                }
                if r.get("created_at"):
                    d["created_at"] = r["created_at"]
                payload.append(d)
            response = self._client.table("interactions").insert(payload).execute()
            ids = [row["id"] for row in (response.data or [])]
            logger.debug("Bulk inserted %d interactions (supabase)", len(ids))
            return ids
        except Exception as e:
            logger.error("Error in insert_interactions_batch (supabase): %s", e)
            raise

    async def next_session_seq(self, session_id: str, count: int = 1) -> int:
        """Return the next session_seq value to assign for this session.

        Caller reserves `count` consecutive values starting from the returned
        integer. NOTE: not atomic across concurrent runs on the same session;
        the in-memory buffer counter in chat.py is the primary source of truth.
        This helper is only used to bootstrap or recover after restart.
        """
        try:
            response = (
                self._client.table("interactions")
                .select("session_seq")
                .eq("session_id", session_id)
                .order("session_seq", desc=True)
                .limit(1)
                .execute()
            )
            rows = response.data or []
            current = 0
            if rows and rows[0].get("session_seq") is not None:
                current = int(rows[0]["session_seq"])
            return current + 1
        except Exception as e:
            logger.warning("next_session_seq lookup failed: %s — starting from 1", e)
            return 1

    # ---- Context Defaults ----

    async def fetch_context_defaults(
        self, context_types: List[str]
    ) -> List[dict]:
        try:
            response = (
                self._client.table("context_templates")
                .select("id, context_type, title, content, tags, created_at, updated_at")
                .in_("context_type", context_types)
                .execute()
            )
            logger.debug(
                "Fetched %s context default rows", len(response.data or []),
            )
            return response.data or []
        except Exception as e:
            logger.error("Error fetching context defaults: %s", e)
            raise

    async def _ensure_p5js_template(self) -> None:
        """Lazy-seed the p5js visualizer skill as a context_templates row."""
        try:
            existing = (
                self._client.table("context_templates")
                .select("id")
                .eq("context_type", "p5js")
                .limit(1)
                .execute()
            )
            if existing.data:
                return  # Already seeded

            import os
            skill_path = os.path.join(os.path.dirname(__file__), "..", "visualizer", "SKILL.md")
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()

            self._client.table("context_templates").insert({
                "context_type": "p5js",
                "title": "p5.js Creative Coding",
                "content": content,
                "tags": ["p5js", "creative-coding", "visualizer"],
            }).execute()
            logger.info("Seeded p5js visualizer skill template")
        except (FileNotFoundError, OSError):
            logger.warning("Visualizer SKILL.md not found — skipping Supabase seed")
        except Exception as e:
            logger.warning("Failed to seed p5js template: %s", e)

    async def copy_defaults_to_agent(self, agent_id: str, template_id: Optional[str] = None) -> int:
        """
        Copy template rows into context for this agent.
        Only copies rows that don't already exist for this agent (by context_type).
        """
        try:
            # Ensure p5js visualizer template row exists (one-time lazy seed)
            await self._ensure_p5js_template()

            agent_check = (
                self._client.table("agents")
                .select("id")
                .eq("id", agent_id)
                .limit(1)
                .execute()
            )
            if not agent_check.data:
                return 0

            defaults = (
                self._client.table("context_templates")
                .select("context_type, title, content, tags")
                .execute()
            )
            if not defaults.data:
                await self._seed_context_templates_from_md_files()
                defaults = (
                    self._client.table("context_templates")
                    .select("context_type, title, content, tags")
                    .execute()
                )
                if not defaults.data:
                    return 0

            existing = (
                self._client.table("context")
                .select("context_type")
                .eq("agent_id", agent_id)
                .execute()
            )
            existing_types = set(r["context_type"] for r in (existing.data or []))

            copied = 0
            for d in defaults.data:
                if d["context_type"] in existing_types:
                    continue
                data = {
                    "agent_id": agent_id,
                    "context_type": d["context_type"],
                    "title": d["title"],
                    "content": d["content"],
                    "tags": d.get("tags", []),
                }
                self._client.table("context").insert(data).execute()
                copied += 1

            if copied > 0:
                logger.info(
                    "Copied %s default context rows to agent %s", copied, agent_id,
                )
            return copied
        except Exception as e:
            logger.error("Error copying defaults to agent: %s", e)
            raise

    async def _seed_agent_templates_from_json_files(self, force: bool = False) -> dict:
        """
        Manifest-gated, non-destructive Supabase seeder.

        Same contract as LocalBackend._seed_agent_templates_from_json_files:
          - Computes a manifest hash over the JSON files.
          - Short-circuits when app_meta['last_agent_manifest_hash'] matches.
          - Per-slot upserts on agent_prompt_templates respecting source guard
            ('admin' rows skipped unless force=True).
          - Writes new hash to app_meta when work is done.

        See LocalBackend for the full contract; this is the same logic against
        the Supabase REST client. Returns the same summary dict.
        """
        import uuid as _uuid_mod
        from app.context.md_seeder import (
            scan_agent_json_files,
            compute_agent_manifest_hash,
        )

        manifest_hash = compute_agent_manifest_hash()
        now = datetime.now(timezone.utc).isoformat()

        # Short-circuit on hash match.
        if not force:
            try:
                meta = (
                    self._client.table("app_meta")
                    .select("value")
                    .eq("key", "last_agent_manifest_hash")
                    .limit(1)
                    .execute()
                )
                if meta.data and meta.data[0].get("value") == manifest_hash:
                    return {
                        "changed": 0, "skipped_admin": 0, "templates": 0,
                        "cached": True, "manifest_hash": manifest_hash,
                    }
            except Exception as e:
                # If app_meta lookup blew up (table missing on old project),
                # fall through to the full pass — first run will create it.
                logger.debug("Supabase app_meta lookup failed (%s) — full seed", e)

        templates = scan_agent_json_files()
        if not templates:
            return {
                "changed": 0, "skipped_admin": 0, "templates": 0,
                "cached": False, "manifest_hash": manifest_hash,
            }

        changed = 0
        skipped_admin = 0

        try:
            for tpl in templates:
                tpl_id = tpl["id"]
                tpl_version = int(tpl.get("version") or 1)

                # 1. agent_templates row — config only.
                cfg_existing = (
                    self._client.table("agent_templates")
                    .select("id")
                    .eq("id", tpl_id)
                    .limit(1)
                    .execute()
                )
                cfg_data = {
                    "max_turn_count": tpl["max_turn_count"],
                    "model": tpl["model"],
                    "provider": tpl["provider"],
                    "temperature": tpl["temperature"],
                    "max_tokens": tpl["max_tokens"],
                    "metadata": tpl["metadata"],
                    "trigger_type": tpl.get("trigger_type", "user_input"),
                    "trigger_key": tpl.get("trigger_key"),
                    "loop_logic": tpl.get("loop_logic", "[]"),
                    "updated_at": now,
                }
                if cfg_existing.data:
                    self._client.table("agent_templates").update(cfg_data).eq("id", tpl_id).execute()
                else:
                    cfg_data["id"] = tpl_id
                    cfg_data["created_at"] = now
                    self._client.table("agent_templates").insert(cfg_data).execute()

                # 2. agent_prompt_templates rows — per slot, version-gated.
                # We rely on LocalBackend._slots_from_template_data shape, but
                # SupabaseBackend has no equivalent helper. Inline a minimal
                # conversion mirroring the local one (slots array OR legacy keys).
                slots = _supabase_slots_from_template_data(tpl)

                for s in slots:
                    slot_name = s["slot_name"]
                    existing = (
                        self._client.table("agent_prompt_templates")
                        .select("id, version, source")
                        .eq("template_id", tpl_id)
                        .eq("slot_name", slot_name)
                        .limit(1)
                        .execute()
                    )
                    payload = {
                        "template_id": tpl_id,
                        "slot_name": slot_name,
                        "order_index": int(s.get("order_index", 0) or 0),
                        "lock": 1 if s.get("lock") else 0,
                        "merge_mode": s.get("merge_mode", "replace"),
                        "content": s.get("content", "") or "",
                        "version": tpl_version,
                        "source": "json",
                        "updated_at": now,
                        "updated_by": "system" if not force else "system-force",
                    }
                    if not existing.data:
                        payload["id"] = str(_uuid_mod.uuid4())
                        self._client.table("agent_prompt_templates").insert(payload).execute()
                        changed += 1
                        continue
                    row = existing.data[0]
                    if row.get("source") == "admin" and not force:
                        skipped_admin += 1
                        continue
                    if force or tpl_version > int(row.get("version") or 0):
                        self._client.table("agent_prompt_templates").update(payload).eq("id", row["id"]).execute()
                        changed += 1

            # Stamp manifest hash so next call short-circuits.
            try:
                existing_meta = (
                    self._client.table("app_meta")
                    .select("key")
                    .eq("key", "last_agent_manifest_hash")
                    .limit(1)
                    .execute()
                )
                if existing_meta.data:
                    self._client.table("app_meta").update(
                        {"value": manifest_hash, "updated_at": now}
                    ).eq("key", "last_agent_manifest_hash").execute()
                else:
                    self._client.table("app_meta").insert(
                        {"key": "last_agent_manifest_hash", "value": manifest_hash, "updated_at": now}
                    ).execute()
            except Exception as e:
                logger.warning("Supabase app_meta hash stamp failed: %s", e)

            logger.info(
                "Supabase seeded %d agent template(s): %d slot rows changed, %d admin rows skipped%s",
                len(templates), changed, skipped_admin,
                " (force=True)" if force else "",
            )
            return {
                "changed": changed,
                "skipped_admin": skipped_admin,
                "templates": len(templates),
                "cached": False,
                "manifest_hash": manifest_hash,
            }
        except Exception as e:
            logger.debug("Supabase agent template seeding failed: %s", e)
            return {
                "changed": changed, "skipped_admin": skipped_admin,
                "templates": len(templates),
                "cached": False, "manifest_hash": manifest_hash,
                "error": str(e)[:200],
            }

    async def _seed_context_templates_from_md_files(self) -> None:
        """
        Scan app/context/context_templates/*.md and seed them into
        context_templates table. Skips duplicates silently
        (unique index on context_type + title).
        """
        from app.context.md_seeder import scan_context_files
        rows = scan_context_files()
        if not rows:
            return
        for row in rows:
            try:
                self._client.table("context_templates").insert({
                    "context_type": row["context_type"],
                    "title": row["title"],
                    "content": row["content"],
                    "tags": row["tags"],  # Supabase accepts native list
                }).execute()
            except Exception as e:
                # Likely unique constraint on (context_type, title)
                logger.debug(
                    "Skipping duplicate template %s/%s: %s",
                    row["context_type"], row["title"], e,
                )
        logger.info("Seeded %d context templates from .md files", len(rows))

    # ---- Context Documents ----

    async def fetch_context_documents(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        try:
            q = (
                self._client.table("context")
                .select(
                    "id, agent_id, context_type, title, content, tags, created_at, updated_at",
                )
                .eq("agent_id", agent_id)
            )
            if context_types:
                q = q.in_("context_type", context_types)
            response = q.order("context_type").execute()
            logger.debug(
                "Fetched %s context rows for agent %s",
                len(response.data or []), agent_id,
            )
            return response.data or []
        except Exception as e:
            logger.error("Error fetching context documents: %s", e)
            raise

    async def get_context_document(
        self, agent_id: str, context_id: str
    ) -> Optional[dict]:
        try:
            response = (
                self._client.table("context")
                .select(
                    "id, agent_id, context_type, title, content, tags, created_at, updated_at",
                )
                .eq("id", context_id)
                .eq("agent_id", agent_id)
                .limit(1)
                .execute()
            )
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            logger.error("Error fetching context document: %s", e)
            raise

    async def update_context_document_content(
        self, agent_id: str, context_id: str, content: str
    ) -> None:
        try:
            response = (
                self._client.table("context")
                .update({"content": content, "updated_at": "now()"})
                .eq("id", context_id)
                .eq("agent_id", agent_id)
                .execute()
            )
            rows = response.data or []
            if not rows:
                raise PermissionError(
                    "Context document not found or not owned by this agent",
                )
            logger.debug("Updated context row %s for agent %s", context_id, agent_id)
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error updating context row: %s", e)
            raise

    async def insert_document(
        self,
        agent_id: str,
        context_type: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
    ) -> str:
        try:
            data = {
                "agent_id": agent_id,
                "context_type": context_type,
                "title": title,
                "content": content,
                "tags": tags or [],
            }
            response = self._client.table("context").insert(data).execute()
            if response.data and len(response.data) > 0:
                doc_id = response.data[0]["id"]
                logger.debug(
                    "Inserted context %s type=%s for agent %s",
                    doc_id, context_type, agent_id,
                )
                return doc_id
            raise ValueError("No data returned after insert")
        except Exception as e:
            logger.error("Error inserting document: %s", e)
            raise

    async def delete_context_row(self, agent_id: str, context_id: str) -> None:
        try:
            response = (
                self._client.table("context")
                .delete()
                .eq("id", context_id)
                .eq("agent_id", agent_id)
                .execute()
            )
            rows = response.data or []
            if not rows:
                raise PermissionError(
                    "Context document not found or not owned by this agent",
                )
            logger.debug("Deleted context row %s for agent %s", context_id, agent_id)
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error deleting context row: %s", e)
            raise

    async def delete_all_documents_for_agent(self, agent_id: str) -> int:
        try:
            response = (
                self._client.table("context")
                .delete()
                .eq("agent_id", agent_id)
                .execute()
            )
            deleted = len(response.data) if response.data else 0
            logger.debug(
                "Deleted %s context rows for agent %s", deleted, agent_id,
            )
            return deleted
        except Exception as e:
            logger.error("Error deleting context: %s", e)
            raise

    async def fetch_context_documents_for_agent(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        try:
            q = (
                self._client.table("context")
                .select("id, agent_id, context_type, title, content, tags, created_at, updated_at")
                .eq("agent_id", agent_id)
            )
            if context_types:
                q = q.in_("context_type", context_types)
            response = q.order("context_type").execute()
            return response.data or []
        except Exception as e:
            logger.error("Error fetching context documents for agent: %s", e)
            raise

    async def get_context_document_for_agent(
        self, agent_id: str, context_id: str
    ) -> Optional[dict]:
        try:
            response = (
                self._client.table("context")
                .select("id, agent_id, context_type, title, content, tags, created_at, updated_at")
                .eq("id", context_id)
                .eq("agent_id", agent_id)
                .limit(1)
                .execute()
            )
            if response.data:
                return response.data[0]
            return None
        except Exception as e:
            logger.error("Error fetching context document for agent: %s", e)
            raise

    async def update_context_document_content_for_agent(
        self, agent_id: str, context_id: str, content: str
    ) -> None:
        try:
            response = (
                self._client.table("context")
                .update({"content": content, "updated_at": "now()"})
                .eq("id", context_id)
                .eq("agent_id", agent_id)
                .execute()
            )
            rows = response.data or []
            if not rows:
                raise PermissionError(
                    "Context document not found or not owned by this agent",
                )
            logger.debug("Updated context row %s (agent-scoped)", context_id)
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error updating context row (agent-scoped): %s", e)
            raise

    async def insert_context_document_for_agent(
        self,
        agent_id: str,
        context_type: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
    ) -> str:
        return await self.insert_document(
            agent_id, context_type, title, content, tags=tags,
        )

    # ---- Memories ----

    # ---- Memory System (knowledge brain) ----
    # Stubs for cloud mode — full implementation when switching to Supabase

    async def memory_upsert(
        self,
        user_id: str,
        slug: str,
        page_type: str,
        title: str,
        compiled_truth: str = "",
        timeline: str = "",
        frontmatter: Optional[dict] = None,
    ) -> dict:
        logger.warning("memory_upsert not yet implemented for Supabase backend")
        return {"slug": slug, "status": "stub"}

    async def memory_get(self, user_id: str, slug: str) -> Optional[dict]:
        logger.warning("memory_get not yet implemented for Supabase backend")
        return None

    async def memory_delete(self, user_id: str, slug: str) -> bool:
        logger.warning("memory_delete not yet implemented for Supabase backend")
        return False

    async def memory_list(
        self, user_id: str, page_type: Optional[str] = None
    ) -> List[dict]:
        return []

    async def memory_search(
        self, user_id: str, query: str, limit: int = 10
    ) -> List[dict]:
        logger.warning("memory_search not yet implemented for Supabase backend")
        return []

    async def memory_add_link(
        self,
        user_id: str,
        from_slug: str,
        to_slug: str,
        link_type: str,
        context: Optional[str] = None,
    ) -> dict:
        logger.warning("memory_add_link not yet implemented for Supabase backend")
        return {"status": "stub"}

    async def memory_graph_query(
        self,
        user_id: str,
        node_slug: str,
        link_type: Optional[str] = None,
        direction: str = "both",
        depth: int = 2,
    ) -> List[dict]:
        return []

    async def memory_add_timeline_entry(
        self,
        user_id: str,
        page_slug: str,
        event_date: str,
        source: str,
        summary: str,
        detail: Optional[str] = None,
    ) -> dict:
        logger.warning("memory_add_timeline_entry not yet implemented for Supabase backend")
        return {"status": "stub"}

    # ---- Session Search ----

    async def search_sessions(
        self, user_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        try:
            summary_response = (
                self._client.table("session_summaries")
                .select("*")
                .eq("user_id", user_id)
                .ilike("summary", f"%{query}%")
                .order("updated_at", desc=True)
                .limit(limit)
                .execute()
            )
            if summary_response.data:
                logger.debug("Found %s session summaries for query %s", len(summary_response.data), query)
                return summary_response.data

            msg_response = (
                self._client.table("interactions")
                .select("session_id, content, created_at")
                .ilike("content", f"%{query}%")
                .limit(limit * 5)
                .execute()
            )

            if not msg_response.data:
                return []

            session_ids = list(set(m["session_id"] for m in msg_response.data))

            sessions_response = (
                self._client.table("sessions")
                .select("id, title, created_at, updated_at")
                .in_("id", session_ids)
                .execute()
            )

            results = []
            for s in sessions_response.data or []:
                matched_msgs = [m for m in msg_response.data if m["session_id"] == s["id"]]
                temp_summary = "Messages found: " + "; ".join(
                    m["content"][:100] for m in matched_msgs[:3]
                )
                results.append({
                    "session_id": s["id"],
                    "title": s.get("title", "Untitled"),
                    "summary": temp_summary,
                    "message_count": len(matched_msgs),
                    "updated_at": s.get("updated_at", ""),
                })
            logger.debug("Found %s sessions via message search for query %s", len(results), query)
            return results

        except Exception as e:
            logger.error("Error searching sessions: %s", e)
            raise

    # ---- Skills ----

    async def list_skills(self, user_id: str, limit: int = 50) -> List[dict]:
        logger.warning("list_skills not fully implemented for Supabase backend")
        return []

    async def skill_track_execution(
        self, skill_id, user_id, session_id, success, duration_ms,
        interaction_id=None, error_message=None, input_params=None,
        output_summary=None, steps_to_complete=1,
    ) -> str:
        logger.warning("skill_track_execution not yet implemented for Supabase")
        return ""

    async def skill_get_rating(self, skill_id: str, user_id: Optional[str] = None) -> dict:
        return {"skill_id": skill_id, "score": None, "execution_count": 0}

    async def skill_add_feedback(
        self, skill_id, user_id, feedback_type, execution_id=None, message=None,
    ) -> str:
        logger.warning("skill_add_feedback not yet implemented for Supabase")
        return ""

    async def skill_get_id_by_name(self, user_id: str, name: str) -> Optional[str]:
        return None

    # ---- Agent Assignment ----

    async def get_agent_for_user(self, user_id: str) -> Optional[dict]:
        # Returns any agent the user owns (oldest first). There is no longer
        # a "default agent" concept — callers that need a specific agent
        # should pass agent_id explicitly.
        try:
            res = (
                self._client.table("agents")
                .select("*")
                .contains("admin_users", [user_id])
                .order("created_at", desc=False)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error("Error getting agent for user %s: %s", user_id, e)
            raise

    async def get_agent_by_id(self, agent_id: str) -> Optional[dict]:
        try:
            res = (
                self._client.table("agents")
                .select("*")
                .eq("id", agent_id)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error("Error getting agent by id %s: %s", agent_id, e)
            raise

    async def fetch_agent_with_context(
        self,
        user_id: str,
        context_types: Optional[List[str]] = None,
    ) -> Optional[dict]:
        """
        Fetch agent + context docs in one query via PostgREST nested embedding.
        Returns agent dict with added key ``context_documents`` (list of dicts).
        Returns None if no agent for user.
        """
        try:
            # PostgREST embedding: relies on FK from context.agent_id -> agents.id
            q = (
                self._client.table("agents")
                .select(
                    "*, context(id, context_type, title, content, tags, created_at, updated_at)"
                )
                .contains("admin_users", [user_id])
                .order("created_at", desc=False)
                .limit(1)
            )
            if context_types:
                q = q.in_("context.context_type", context_types)
            res = q.execute()
            if not res.data:
                return None
            agent = res.data[0]
            # Normalize: rename 'context' to 'context_documents'
            agent["context_documents"] = agent.pop("context", None) or []
            # Ensure tags are parsed (Supabase returns them as-is from JSONB)
            for doc in agent["context_documents"]:
                if isinstance(doc.get("tags"), str):
                    try:
                        doc["tags"] = json.loads(doc["tags"])
                    except (json.JSONDecodeError, TypeError):
                        doc["tags"] = []
            return agent
        except Exception as e:
            logger.error("Error fetching agent with context: %s", e)
            raise

    async def fetch_agent_by_id_with_context(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Same as ``fetch_agent_with_context`` but queries by agent ``id`` (PK) instead of ``user_id``.
        ``user_id`` is accepted for API parity with the local backend's per-caller slot resolution.
        """
        try:
            q = (
                self._client.table("agents")
                .select(
                    "*, context(id, context_type, title, content, tags, created_at, updated_at)"
                )
                .eq("id", agent_id)
                .limit(1)
            )
            if context_types:
                q = q.in_("context.context_type", context_types)
            res = q.execute()
            if not res.data:
                return None
            agent = res.data[0]
            agent["context_documents"] = agent.pop("context", None) or []
            for doc in agent["context_documents"]:
                if isinstance(doc.get("tags"), str):
                    try:
                        doc["tags"] = json.loads(doc["tags"])
                    except (json.JSONDecodeError, TypeError):
                        doc["tags"] = []
            return agent
        except Exception as e:
            logger.error("Error fetching agent by id with context: %s", e)
            raise

    async def create_agent_for_user(self, user_id: str) -> dict:
        from datetime import datetime, timezone
        import uuid
        try:
            # Templates are seeded at boot (manifest-gated) + on admin re-seed.
            # No per-call re-seed: avoids round-trip churn and protects admin edits.

            # Fetch default template
            tpl_res = (
                self._client.table("agent_templates")
                .select("*")
                .eq("id", "default")
                .limit(1)
                .execute()
            )
            tpl = tpl_res.data[0] if tpl_res.data else None
            if not tpl:
                logger.warning(
                    "No 'default' agent template found after JSON seeding — "
                    "check app/context/agents/default.json"
                )
                raise ValueError("No default agent template available")

            now = datetime.now(timezone.utc).isoformat()
            agent_data = {
                "id": str(uuid.uuid4()),
                "is_user_default": True,
                "admin_users": [user_id],
                "system_prompt": tpl["system_prompt"],
                "max_turn_count": tpl["max_turn_count"],
                "model": tpl["model"],
                "provider": tpl["provider"],
                "temperature": tpl["temperature"],
                "max_tokens": tpl["max_tokens"],
                "status": "active",
                "metadata": tpl["metadata"],
                "assigned_at": now,
                "created_at": now,
                "updated_at": now,
                "turn_count": 0,
            }

            res = self._client.table("agents").insert(agent_data).execute()
            if res.data and len(res.data) > 0:
                logger.info("Created agent %s for user %s from JSON template", agent_data["id"], user_id)
                return res.data[0]
            raise ValueError("No data returned after agent insert")
        except Exception as e:
            logger.error("Error creating agent for user %s: %s", user_id, e)
            raise

    async def increment_agent_turn_count(self, agent_id: str) -> int:
        try:
            # Note: Supabase REST API doesn't have an atomic increment via standard RPC without a custom function.
            # Here we do a select then update. In a high concurrency environment, a DB function would be better.
            res = self._client.table("agents").select("turn_count").eq("id", agent_id).single().execute()
            current_count = res.data.get("turn_count", 0) if res.data else 0
            new_count = current_count + 1
            up_res = self._client.table("agents").update({"turn_count": new_count}).eq("id", agent_id).execute()
            return up_res.data[0]["turn_count"] if up_res.data else new_count
        except Exception as e:
            logger.error("Error incrementing turn count for agent %s: %s", agent_id, e)
            raise

    async def get_default_template(self) -> dict:
        try:
            res = (
                self._client.table("agent_templates")
                .select("*")
                .eq("id", "default")
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]
            logger.warning(
                "No 'default' agent template in DB — check app/context/agents/default.json"
            )
            # Fallback: minimal dict — JSON is the real source of truth
            return {
                "id": "default",
                "system_prompt": "",
                "max_turn_count": 10,
                "model": None,
                "provider": None,
                "temperature": 0.0,
                "max_tokens": 4096,
                "metadata": "{}",
            }
        except Exception as e:
            logger.error("Error getting default template: %s", e)
            raise

    async def get_max_turn_count(self, agent_id: str = "default_agent") -> int:
        try:
            res = (
                self._client.table("agents")
                .select("max_turn_count")
                .eq("id", agent_id)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0]["max_turn_count"]
            logger.warning("Agent %s not found for max_turn_count lookup", agent_id)
            return 10
        except Exception as e:
            logger.error("Error getting max_turn_count for agent %s: %s", agent_id, e)
            raise

    async def seed_agent_templates(self, force: bool = False) -> dict:
        """Re-seed agent_templates + agent_prompt_templates from JSON.

        force=True overrides the manifest short-circuit AND overwrites rows
        whose source = 'admin'. See StorageBackend.seed_agent_templates docstring.
        """
        return await self._seed_agent_templates_from_json_files(force=force)

    # ---- Agent Resolution & Session Binding ----

    async def resolve_agent(self, user_id: str, template_id: str) -> dict:
        """
        Resolve an agent for a user + template combo.
        Delegates to local backend for cross-source resolution.
        """
        from app.db.local import LocalBackend
        lb = LocalBackend()
        return await lb.resolve_agent(user_id, template_id)

    async def get_session_agent_id(self, session_id: str) -> Optional[str]:
        """Get the agent_id bound to a session from sessions table."""
        try:
            res = (
                self._client.table("sessions")
                .select("agent_id")
                .eq("id", session_id)
                .limit(1)
                .execute()
            )
            if res.data and res.data[0].get("agent_id"):
                return res.data[0]["agent_id"]
            return None
        except Exception as e:
            logger.error("Error getting session agent_id: %s", e)
            return None

    # ---- Session Participants ----

    async def add_session_participant(
        self, session_id: str, participant_id: str, role: str
    ) -> None:
        """Add a participant to a session. role is 'user' or 'agent'."""
        try:
            res = self._client.table("sessions").select("participants").eq("id", session_id).limit(1).execute()
            participants = json.loads(res.data[0]["participants"]) if res.data and res.data[0].get("participants") else []
            if not any(p.get("id") == participant_id for p in participants):
                participants.append({"id": participant_id, "role": role})
                self._client.table("sessions").upsert(
                    {"id": session_id, "participants": json.dumps(participants)},
                    on_conflict="id",
                ).execute()
        except Exception as e:
            logger.error("Error adding session participant: %s", e)
            raise

    async def remove_session_participant(
        self, session_id: str, participant_id: str
    ) -> None:
        """Remove a participant from a session by id."""
        try:
            res = self._client.table("sessions").select("participants").eq("id", session_id).limit(1).execute()
            participants = json.loads(res.data[0]["participants"]) if res.data and res.data[0].get("participants") else []
            participants = [p for p in participants if p.get("id") != participant_id]
            self._client.table("sessions").upsert(
                {"id": session_id, "participants": json.dumps(participants)},
                on_conflict="id",
            ).execute()
        except Exception as e:
            logger.error("Error removing session participant: %s", e)
            raise

    async def is_session_participant(
        self, session_id: str, participant_id: str, role: Optional[str] = None
    ) -> bool:
        """Check if participant_id is in a session. If role specified, also checks role matches."""
        try:
            res = self._client.table("sessions").select("participants").eq("id", session_id).limit(1).execute()
            participants = json.loads(res.data[0]["participants"]) if res.data and res.data[0].get("participants") else []
            for p in participants:
                if p.get("id") == participant_id:
                    if role is None or p.get("role") == role:
                        return True
            return False
        except Exception as e:
            logger.error("Error checking session participant: %s", e)
            return False

    async def get_session_participants(
        self, session_id: str
    ) -> List[dict]:
        """Return the full participants array for a session."""
        try:
            res = self._client.table("sessions").select("participants").eq("id", session_id).limit(1).execute()
            return json.loads(res.data[0]["participants"]) if res.data and res.data[0].get("participants") else []
        except Exception as e:
            logger.error("Error getting session participants: %s", e)
            return []

    async def bind_session_to_agent(self, session_id: str, agent_id: str) -> None:
        """Bind a session to an agent by setting sessions.agent_id."""
        try:
            self._client.table("sessions").upsert(
                {"id": session_id, "agent_id": agent_id},
                on_conflict="id",
            ).execute()
            logger.debug("Bound session %s to agent %s", session_id[:8], agent_id[:8])
        except Exception as e:
            logger.error("Error binding session to agent: %s", e)
            raise

    async def get_or_resolve_session_agent(
        self,
        session_id: str,
        user_id: str,
        template_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Get the agent for a session, creating/binding it if needed.
        Delegates to the local backend for the full resolution logic.
        """
        from app.db.local import LocalBackend
        lb = LocalBackend()
        return await lb.get_or_resolve_session_agent(session_id, user_id, template_id)

    # ── Agent membership (admin_users / member_users) ────────────────────────

    async def get_agent_roles(self, agent_id: str) -> dict:
        """Return admin_users and member_users for an agent."""
        try:
            import json as _json
            res = (
                self._client.table("agents")
                .select("admin_users, member_users")
                .eq("id", agent_id)
                .limit(1)
                .execute()
            )
            row = res.data[0] if res.data else None
            if not row:
                return {"admin_users": [], "member_users": []}
            return {
                "admin_users": _json.loads(row.get("admin_users") or "[]"),
                "member_users": _json.loads(row.get("member_users") or "[]"),
            }
        except Exception as e:
            logger.warning("get_agent_roles failed: %s", e)
            return {"admin_users": [], "member_users": []}

    async def add_agent_member(self, agent_id: str, user_id: str) -> bool:
        """Add user_id to an agent's member_users list if not already present."""
        try:
            import json as _json
            roles = await self.get_agent_roles(agent_id)
            if user_id in roles["member_users"] or user_id in roles["admin_users"]:
                return False
            new_members = _json.dumps(roles["member_users"] + [user_id])
            self._client.table("agents").update({"member_users": new_members}).eq("id", agent_id).execute()
            return True
        except Exception as e:
            logger.warning("add_agent_member failed: %s", e)
            return False

    async def is_agent_member(self, agent_id: str, user_id: str) -> bool:
        """Return True if user_id is a member or admin of the agent."""
        roles = await self.get_agent_roles(agent_id)
        return user_id in roles["member_users"] or user_id in roles["admin_users"]

    # ---- Interrupt Handling ----

    async def set_interrupt(self, session_id: str) -> None:
        try:
            data = {
                "session_id": session_id,
                "interrupt_requested": True,
            }
            self._client.table("session_interrupts").upsert(data, on_conflict="session_id").execute()
        except Exception as e:
            logger.error("Error setting interrupt for %s: %s", session_id, e)
            raise

    async def clear_interrupt(self, session_id: str) -> None:
        try:
            self._client.table("session_interrupts").delete().eq("session_id", session_id).execute()
        except Exception as e:
            logger.error("Error clearing interrupt for %s: %s", session_id, e)
            raise

    # ---- Attachments ----

    async def insert_attachment(
        self,
        user_id: str,
        session_id: str,
        original_name: str,
        mime_type: str,
        size_bytes: int,
        storage_path: str,
        metadata: Optional[dict] = None,
        storage_provider: str = "local",
    ) -> str:
        """Insert an attachment record. Returns the attachment id."""
        import uuid
        att_id = str(uuid.uuid4())
        res = (
            self._client.table("attachments")
            .insert({
                "id": att_id,
                "user_id": user_id,
                "session_id": session_id,
                "original_name": original_name,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "storage_path": storage_path,
                "storage_provider": storage_provider,
                "metadata": json.dumps(metadata or {}),
            })
            .execute()
        )
        logger.debug("Inserted attachment %s: %s (provider=%s)", att_id, original_name, storage_provider)
        return att_id

    async def get_attachment(self, attachment_id: str) -> Optional[dict]:
        """Get a single attachment by id."""
        res = (
            self._client.table("attachments")
            .select("*")
            .eq("id", attachment_id)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
        return None

    async def get_session_attachments(self, session_id: str) -> List[dict]:
        """Get all attachments for a session."""
        res = (
            self._client.table("attachments")
            .select("*")
            .eq("session_id", session_id)
            .order("created_at")
            .execute()
        )
        return res.data or []

    async def delete_attachment(self, attachment_id: str) -> bool:
        """Delete an attachment record by id."""
        res = (
            self._client.table("attachments")
            .delete()
            .eq("id", attachment_id)
            .execute()
        )
        return len(res.data or []) > 0

    async def update_attachment_metadata(self, attachment_id: str, patch: dict) -> bool:
        """Merge `patch` into an attachment's metadata (preserving existing keys).
        Returns True if a row was updated."""
        res = (
            self._client.table("attachments")
            .select("metadata")
            .eq("id", attachment_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return False
        meta = res.data[0].get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(patch or {})
        upd = (
            self._client.table("attachments")
            .update({"metadata": meta})
            .eq("id", attachment_id)
            .execute()
        )
        return len(upd.data or []) > 0

    async def update_interaction_content(self, interaction_id: str, content: str) -> bool:
        """Replace an interaction row's content (persists injected attachment
        descriptions into the user turn so later turns retain them)."""
        res = (
            self._client.table("interactions")
            .update({"content": content})
            .eq("id", interaction_id)
            .execute()
        )
        return len(res.data or []) > 0

    async def check_interrupt(self, session_id: str) -> bool:
        try:
            res = (
                self._client.table("session_interrupts")
                .select("interrupt_requested")
                .eq("session_id", session_id)
                .limit(1)
                .execute()
            )
            if res.data and res.data[0]["interrupt_requested"]:
                return True
            return False
        except Exception as e:
            logger.error("Error checking interrupt for %s: %s", session_id, e)
            return False

    # ---- Auth Elements ----

    async def auth_element_get(
        self, user_id: str, service: str, label: str = "default"
    ) -> Optional[dict]:
        try:
            res = (
                self._client.table("auth_elements")
                .select("*")
                .eq("user_id", user_id)
                .eq("service", service)
                .eq("label", label)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error("auth_element_get error: %s", e)
            return None

    async def auth_element_set(
        self,
        user_id: str,
        service: str,
        config: dict,
        secret_ref: str = "",
        label: str = "default",
    ) -> dict:
        import json, uuid
        data = {
            "user_id": user_id,
            "service": service,
            "label": label,
            "config": json.dumps(config),
            "secret_ref": secret_ref,
        }
        try:
            existing = (
                self._client.table("auth_elements")
                .select("id")
                .eq("user_id", user_id)
                .eq("service", service)
                .eq("label", label)
                .limit(1)
                .execute()
            )
            if existing.data:
                res = (
                    self._client.table("auth_elements")
                    .update(data)
                    .eq("id", existing.data[0]["id"])
                    .execute()
                )
            else:
                data["id"] = str(uuid.uuid4())
                res = (
                    self._client.table("auth_elements")
                    .insert(data)
                    .execute()
                )
            return res.data[0] if res.data else data
        except Exception as e:
            logger.error("auth_element_set error: %s", e)
            return data

    async def auth_element_list(
        self, user_id: str, service: Optional[str] = None
    ) -> List[dict]:
        try:
            q = (
                self._client.table("auth_elements")
                .select("*")
                .eq("user_id", user_id)
            )
            if service:
                q = q.eq("service", service)
            res = q.execute()
            return res.data or []
        except Exception as e:
            logger.error("auth_element_list error: %s", e)
            return []

    async def auth_element_delete(
        self, user_id: str, service: str, label: str = "default"
    ) -> bool:
        try:
            res = (
                self._client.table("auth_elements")
                .delete()
                .eq("user_id", user_id)
                .eq("service", service)
                .eq("label", label)
                .execute()
            )
            return len(res.data) > 0
        except Exception as e:
            logger.error("auth_element_delete error: %s", e)
            return False

    # ────────────────────────────────────────────────────────────────────
    # Pages (page-builder workspace)
    # ────────────────────────────────────────────────────────────────────

    async def pages_list(self, user_id: str) -> List[dict]:
        try:
            res = (
                self._client.table("pages")
                .select("*")
                .eq("user_id", user_id)
                .order("updated_at", desc=True)
                .execute()
            )
            rows = res.data or []
            # Force 'home' first
            home = [r for r in rows if r.get("slug") == "home"]
            others = [r for r in rows if r.get("slug") != "home"]
            return home + others
        except Exception as e:
            logger.error("pages_list error: %s", e)
            return []

    async def pages_get(self, user_id: str, slug: str) -> Optional[dict]:
        try:
            res = (
                self._client.table("pages")
                .select("*")
                .eq("user_id", user_id)
                .eq("slug", slug)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error("pages_get error: %s", e)
            return None

    async def pages_upsert(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        html: Optional[str] = None,
    ) -> dict:
        import uuid
        now = datetime.now(timezone.utc).isoformat()
        try:
            existing = (
                self._client.table("pages")
                .select("id")
                .eq("user_id", user_id)
                .eq("slug", slug)
                .limit(1)
                .execute()
            )
            if existing.data:
                data: dict = {
                    "title": title,
                    "agent_context": agent_context,
                    "updated_at": now,
                }
                if html is not None:
                    data["html"] = html
                res = (
                    self._client.table("pages")
                    .update(data)
                    .eq("id", existing.data[0]["id"])
                    .execute()
                )
            else:
                data = {
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "slug": slug,
                    "title": title,
                    "agent_context": agent_context,
                    "html": html,
                    "created_at": now,
                    "updated_at": now,
                }
                res = self._client.table("pages").insert(data).execute()
            return res.data[0] if res.data else data
        except Exception as e:
            logger.error("pages_upsert error: %s", e)
            raise

    async def pages_delete(self, user_id: str, slug: str) -> bool:
        try:
            res = (
                self._client.table("pages")
                .delete()
                .eq("user_id", user_id)
                .eq("slug", slug)
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            logger.error("pages_delete error: %s", e)
            return False

    # ────────────────────────────────────────────────────────────────────
    # Per-Agent External Data Sources
    # ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _coerce_data_source(row: dict) -> dict:
        d = dict(row)
        for k in ("config", "schema_cache", "safety_policy"):
            val = d.get(k)
            if isinstance(val, str):
                try:
                    d[k] = json.loads(val) if val else {}
                except Exception:
                    d[k] = {}
            elif val is None:
                d[k] = {}
        return d

    async def data_source_create(
        self,
        user_id: str,
        name: str,
        type: str,
        config: Optional[dict] = None,
        auth_element_id: Optional[str] = None,
        safety_policy: Optional[dict] = None,
    ) -> dict:
        import uuid
        row = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "type": type,
            "config": json.dumps(config or {}),
            "auth_element_id": auth_element_id,
            "schema_cache": json.dumps({}),
            "safety_policy": json.dumps(safety_policy or {}),
            "status": "unverified",
        }
        try:
            res = self._client.table("data_sources").insert(row).execute()
            return self._coerce_data_source(res.data[0] if res.data else row)
        except Exception as e:
            logger.error("data_source_create error: %s", e)
            raise

    async def data_source_update(self, ds_id: str, user_id: str, **fields) -> Optional[dict]:
        allowed = {
            "name", "config", "auth_element_id", "schema_cache",
            "safety_policy", "status", "last_test_message",
            "last_tested_at", "last_introspected_at",
        }
        update = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("config", "schema_cache", "safety_policy") and not isinstance(v, str):
                v = json.dumps(v or {})
            update[k] = v
        if not update:
            return await self.data_source_get(ds_id, user_id)
        update["updated_at"] = "now()"
        try:
            res = (
                self._client.table("data_sources")
                .update(update)
                .eq("id", ds_id)
                .eq("user_id", user_id)
                .execute()
            )
            if res.data:
                return self._coerce_data_source(res.data[0])
            return None
        except Exception as e:
            logger.error("data_source_update error: %s", e)
            raise

    async def data_source_get(self, ds_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        try:
            q = self._client.table("data_sources").select("*").eq("id", ds_id)
            if user_id is not None:
                q = q.eq("user_id", user_id)
            res = q.limit(1).execute()
            if res.data:
                return self._coerce_data_source(res.data[0])
            return None
        except Exception as e:
            logger.error("data_source_get error: %s", e)
            return None

    async def data_source_list(self, user_id: str) -> List[dict]:
        try:
            res = (
                self._client.table("data_sources")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .execute()
            )
            return [self._coerce_data_source(r) for r in (res.data or [])]
        except Exception as e:
            logger.error("data_source_list error: %s", e)
            return []

    async def data_source_delete(self, ds_id: str, user_id: str) -> bool:
        try:
            res = (
                self._client.table("data_sources")
                .delete()
                .eq("id", ds_id)
                .eq("user_id", user_id)
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            logger.error("data_source_delete error: %s", e)
            return False

    async def agent_data_source_list(self, agent_id: str, enabled_only: bool = False) -> List[dict]:
        try:
            q = (
                self._client.table("agent_data_sources")
                .select(
                    "id, agent_id, data_source_id, tool_alias, enabled, "
                    "inject_schema_in_prompt, created_at, "
                    "data_sources(user_id, name, type, config, auth_element_id, "
                    "schema_cache, safety_policy, status, last_tested_at, last_introspected_at)"
                )
                .eq("agent_id", agent_id)
            )
            if enabled_only:
                q = q.eq("enabled", True)
            res = q.execute()
            results = []
            for row in (res.data or []):
                ds = row.pop("data_sources", {}) or {}
                merged = {
                    "attachment_id": row.get("id"),
                    "agent_id": row.get("agent_id"),
                    "data_source_id": row.get("data_source_id"),
                    "tool_alias": row.get("tool_alias"),
                    "enabled": row.get("enabled"),
                    "inject_schema_in_prompt": row.get("inject_schema_in_prompt"),
                    "attached_at": row.get("created_at"),
                    "owner_user_id": ds.get("user_id"),
                    "name": ds.get("name"),
                    "type": ds.get("type"),
                    "config": ds.get("config"),
                    "auth_element_id": ds.get("auth_element_id"),
                    "schema_cache": ds.get("schema_cache"),
                    "safety_policy": ds.get("safety_policy"),
                    "status": ds.get("status"),
                    "last_tested_at": ds.get("last_tested_at"),
                    "last_introspected_at": ds.get("last_introspected_at"),
                }
                for k in ("config", "schema_cache", "safety_policy"):
                    val = merged.get(k)
                    if isinstance(val, str):
                        try:
                            merged[k] = json.loads(val) if val else {}
                        except Exception:
                            merged[k] = {}
                    elif val is None:
                        merged[k] = {}
                results.append(merged)
            return results
        except Exception as e:
            logger.error("agent_data_source_list error: %s", e)
            return []

    async def agent_data_source_attach(
        self,
        agent_id: str,
        data_source_id: str,
        tool_alias: Optional[str] = None,
        inject_schema_in_prompt: bool = True,
    ) -> dict:
        import uuid
        row = {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "data_source_id": data_source_id,
            "tool_alias": tool_alias,
            "enabled": True,
            "inject_schema_in_prompt": bool(inject_schema_in_prompt),
        }
        try:
            res = (
                self._client.table("agent_data_sources")
                .upsert(row, on_conflict="agent_id,data_source_id")
                .execute()
            )
            return res.data[0] if res.data else row
        except Exception as e:
            logger.error("agent_data_source_attach error: %s", e)
            raise

    async def agent_data_source_update(
        self, agent_id: str, data_source_id: str, **fields
    ) -> Optional[dict]:
        allowed = {"tool_alias", "enabled", "inject_schema_in_prompt"}
        update = {k: v for k, v in fields.items() if k in allowed}
        if not update:
            return None
        try:
            res = (
                self._client.table("agent_data_sources")
                .update(update)
                .eq("agent_id", agent_id)
                .eq("data_source_id", data_source_id)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error("agent_data_source_update error: %s", e)
            return None

    async def agent_data_source_detach(self, agent_id: str, data_source_id: str) -> bool:
        try:
            res = (
                self._client.table("agent_data_sources")
                .delete()
                .eq("agent_id", agent_id)
                .eq("data_source_id", data_source_id)
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            logger.error("agent_data_source_detach error: %s", e)
            return False

    # doc_chunks — minimal CRUD; hybrid search uses pgvector in cloud mode.
    # Phase-1 doc-store ingest writes to the SQLite local backend by default.
    # When running in cloud mode, the connector should call these directly.

    async def doc_chunk_upsert(
        self,
        data_source_id: str,
        source_ref: str,
        chunk_index: int,
        chunk_text: str,
        content_hash: Optional[str] = None,
        embedding: Optional[List[float]] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        import uuid
        chunk_id = str(uuid.uuid4())
        row = {
            "id": chunk_id,
            "data_source_id": data_source_id,
            "source_ref": source_ref,
            "chunk_index": chunk_index,
            "chunk_text": chunk_text,
            "content_hash": content_hash,
            "embedding": embedding,
            "token_count": len(chunk_text.split()) if chunk_text else 0,
            "metadata": json.dumps(metadata or {}),
        }
        try:
            self._client.table("doc_chunks").delete().eq(
                "data_source_id", data_source_id
            ).eq("source_ref", source_ref).eq("chunk_index", chunk_index).execute()
            self._client.table("doc_chunks").insert(row).execute()
            return chunk_id
        except Exception as e:
            logger.error("doc_chunk_upsert error: %s", e)
            raise

    async def doc_chunk_delete_by_source_ref(self, data_source_id: str, source_ref: str) -> int:
        try:
            res = (
                self._client.table("doc_chunks")
                .delete()
                .eq("data_source_id", data_source_id)
                .eq("source_ref", source_ref)
                .execute()
            )
            return len(res.data or [])
        except Exception as e:
            logger.error("doc_chunk_delete_by_source_ref error: %s", e)
            return 0

    async def doc_chunk_count(self, data_source_id: str) -> int:
        try:
            res = (
                self._client.table("doc_chunks")
                .select("id", count="exact")
                .eq("data_source_id", data_source_id)
                .execute()
            )
            return getattr(res, "count", 0) or 0
        except Exception as e:
            logger.error("doc_chunk_count error: %s", e)
            return 0

    async def doc_chunk_search(
        self, data_source_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        """Postgres hybrid search via RPC `doc_chunks_hybrid_search` if defined,
        else falls back to a simple ILIKE keyword scan. Define the RPC in
        migrations/014_data_sources.sql for production-quality results."""
        try:
            res = self._client.rpc(
                "doc_chunks_hybrid_search",
                {"p_data_source_id": data_source_id, "p_query": query, "p_limit": limit},
            ).execute()
            if res.data:
                return res.data
        except Exception:
            pass  # fall through to ILIKE
        try:
            res = (
                self._client.table("doc_chunks")
                .select("id, source_ref, chunk_index, chunk_text")
                .eq("data_source_id", data_source_id)
                .ilike("chunk_text", f"%{query}%")
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception as e:
            logger.error("doc_chunk_search ILIKE fallback error: %s", e)
            return []


# ── Backward-compatible alias ────────────────────────────────────────────────
# The old SupabaseClient used static methods directly.
# This shim creates an instance and delegates, so existing imports still work.


class SupabaseClient:
    """
    Backward-compatible static-method interface.
    Delegates to a single SupabaseBackend instance.
    """
    _backend: Optional[SupabaseBackend] = None

    @classmethod
    def _get_backend(cls) -> SupabaseBackend:
        if cls._backend is None:
            cls._backend = SupabaseBackend()
        return cls._backend

    @classmethod
    def get_client(cls) -> Client:
        """Return the underlying Supabase client (for direct table queries)."""
        return cls._get_backend().get_raw_client()

    @staticmethod
    async def assert_session_owned(user_id: str, session_id: str) -> None:
        await SupabaseClient._get_backend().assert_session_owned(user_id, session_id)

    @staticmethod
    async def upsert_session_summary(
        user_id: str, session_id: str, summary: str, message_count: int, title: str = None
    ) -> None:
        await SupabaseClient._get_backend().upsert_session_summary(
            user_id, session_id, summary, message_count, title
        )

    @staticmethod
    async def fetch_interactions(user_id: str, session_id: str) -> List[InteractionRecord]:
        return await SupabaseClient._get_backend().fetch_interactions(user_id, session_id)

    @staticmethod
    async def insert_interaction(
        user_id: str, session_id: str, role: str, content: str,
        parent_id: Optional[str] = None, tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None, channel: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[str] = None, input_data: Optional[str] = None,
        output_data: Optional[str] = None,
        sender_id: Optional[str] = None,
        receiver_id: Optional[str] = None,
        session_seq: Optional[int] = None,
        turn_id: Optional[str] = None,
        turn_seq: Optional[int] = None,
        status: str = "complete",
    ) -> str:
        return await SupabaseClient._get_backend().insert_interaction(
            user_id, session_id, role, content, parent_id, tool_name, tool_call_id, channel, source, metadata, input_data, output_data, sender_id, receiver_id,
            session_seq=session_seq, turn_id=turn_id, turn_seq=turn_seq, status=status,
        )

    @staticmethod
    async def insert_interactions_batch(rows: List[Dict[str, Any]]) -> List[str]:
        return await SupabaseClient._get_backend().insert_interactions_batch(rows)

    @staticmethod
    async def next_session_seq(session_id: str, count: int = 1) -> int:
        return await SupabaseClient._get_backend().next_session_seq(session_id, count)

    @staticmethod
    async def fetch_context_defaults(context_types: List[str]) -> List[dict]:
        return await SupabaseClient._get_backend().fetch_context_defaults(
            context_types
        )

    @staticmethod
    async def copy_defaults_to_agent(agent_id: str, template_id: Optional[str] = None) -> int:
        return await SupabaseClient._get_backend().copy_defaults_to_agent(agent_id, template_id=template_id)

    @staticmethod
    async def fetch_context_documents(
        agent_id: str, context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        return await SupabaseClient._get_backend().fetch_context_documents(
            agent_id, context_types
        )

    @staticmethod
    async def get_context_document(agent_id: str, context_id: str) -> Optional[dict]:
        return await SupabaseClient._get_backend().get_context_document(
            agent_id, context_id,
        )

    @staticmethod
    async def update_context_document_content(
        agent_id: str, context_id: str, content: str,
    ) -> None:
        await SupabaseClient._get_backend().update_context_document_content(
            agent_id, context_id, content,
        )

    @staticmethod
    async def insert_document(
        agent_id: str, context_type: str, title: str, content: str,
        tags: Optional[List[str]] = None,
    ) -> str:
        return await SupabaseClient._get_backend().insert_document(
            agent_id, context_type, title, content, tags
        )

    @staticmethod
    async def delete_context_row(agent_id: str, context_id: str) -> None:
        await SupabaseClient._get_backend().delete_context_row(agent_id, context_id)

    @staticmethod
    async def delete_all_documents_for_agent(agent_id: str) -> int:
        return await SupabaseClient._get_backend().delete_all_documents_for_agent(
            agent_id,
        )

    @staticmethod
    async def memory_upsert(
        user_id: str, slug: str, page_type: str, title: str,
        compiled_truth: str = "", timeline: str = "",
        frontmatter: Optional[dict] = None,
    ) -> dict:
        return await SupabaseClient._get_backend().memory_upsert(
            user_id, slug, page_type, title, compiled_truth, timeline, frontmatter
        )

    @staticmethod
    async def memory_get(user_id: str, slug: str) -> Optional[dict]:
        return await SupabaseClient._get_backend().memory_get(user_id, slug)

    @staticmethod
    async def memory_delete(user_id: str, slug: str) -> bool:
        return await SupabaseClient._get_backend().memory_delete(user_id, slug)

    @staticmethod
    async def memory_list(user_id: str, page_type: Optional[str] = None) -> List[dict]:
        return await SupabaseClient._get_backend().memory_list(user_id, page_type)

    @staticmethod
    async def memory_search(user_id: str, query: str, limit: int = 10) -> List[dict]:
        return await SupabaseClient._get_backend().memory_search(user_id, query, limit)

    @staticmethod
    async def memory_add_link(
        user_id: str, from_slug: str, to_slug: str,
        link_type: str, context: Optional[str] = None,
    ) -> dict:
        return await SupabaseClient._get_backend().memory_add_link(
            user_id, from_slug, to_slug, link_type, context
        )

    @staticmethod
    async def memory_graph_query(
        user_id: str, node_slug: str,
        link_type: Optional[str] = None,
        direction: str = "both", depth: int = 2,
    ) -> List[dict]:
        return await SupabaseClient._get_backend().memory_graph_query(
            user_id, node_slug, link_type, direction, depth
        )

    @staticmethod
    async def memory_add_timeline_entry(
        user_id: str, page_slug: str, event_date: str,
        source: str, summary: str, detail: Optional[str] = None,
    ) -> dict:
        return await SupabaseClient._get_backend().memory_add_timeline_entry(
            user_id, page_slug, event_date, source, summary, detail
        )

    @staticmethod
    async def search_sessions(user_id: str, query: str, limit: int = 5) -> List[dict]:
        return await SupabaseClient._get_backend().search_sessions(user_id, query, limit)

    @staticmethod
    async def list_skills(user_id: str, limit: int = 50) -> List[dict]:
        return await SupabaseClient._get_backend().list_skills(user_id, limit)

    @staticmethod
    async def skill_track_execution(
        skill_id, user_id, session_id, success, duration_ms,
        interaction_id=None, error_message=None, input_params=None,
        output_summary=None, steps_to_complete=1,
    ) -> str:
        return await SupabaseClient._get_backend().skill_track_execution(
            skill_id, user_id, session_id, success, duration_ms,
            interaction_id, error_message, input_params,
            output_summary, steps_to_complete,
        )

    @staticmethod
    async def skill_get_rating(skill_id: str, user_id: Optional[str] = None) -> dict:
        return await SupabaseClient._get_backend().skill_get_rating(skill_id, user_id)

    @staticmethod
    async def skill_add_feedback(
        skill_id, user_id, feedback_type, execution_id=None, message=None,
    ) -> str:
        return await SupabaseClient._get_backend().skill_add_feedback(
            skill_id, user_id, feedback_type, execution_id, message,
        )

    @staticmethod
    async def skill_get_id_by_name(user_id: str, name: str) -> Optional[str]:
        return await SupabaseClient._get_backend().skill_get_id_by_name(user_id, name)

    @staticmethod
    async def auth_element_get(
        user_id: str, service: str, label: str = "default"
    ) -> Optional[dict]:
        return await SupabaseClient._get_backend().auth_element_get(user_id, service, label)

    @staticmethod
    async def auth_element_set(
        user_id: str,
        service: str,
        config: dict,
        secret_ref: str = "",
        label: str = "default",
    ) -> dict:
        return await SupabaseClient._get_backend().auth_element_set(
            user_id, service, config, secret_ref, label
        )

    @staticmethod
    async def auth_element_list(
        user_id: str, service: Optional[str] = None
    ) -> List[dict]:
        return await SupabaseClient._get_backend().auth_element_list(user_id, service)

    @staticmethod
    async def auth_element_delete(
        user_id: str, service: str, label: str = "default"
    ) -> bool:
        return await SupabaseClient._get_backend().auth_element_delete(
            user_id, service, label
        )

    @staticmethod
    async def resolve_agent(user_id: str, template_id: str) -> dict:
        return await SupabaseClient._get_backend().resolve_agent(user_id, template_id)

    @staticmethod
    async def get_session_agent_id(session_id: str) -> Optional[str]:
        return await SupabaseClient._get_backend().get_session_agent_id(session_id)

    @staticmethod
    async def add_session_participant(session_id: str, participant_id: str, role: str) -> None:
        return await SupabaseClient._get_backend().add_session_participant(session_id, participant_id, role)

    @staticmethod
    async def remove_session_participant(session_id: str, participant_id: str) -> None:
        return await SupabaseClient._get_backend().remove_session_participant(session_id, participant_id)

    @staticmethod
    async def is_session_participant(session_id: str, participant_id: str, role: Optional[str] = None) -> bool:
        return await SupabaseClient._get_backend().is_session_participant(session_id, participant_id, role)

    @staticmethod
    async def get_session_participants(session_id: str) -> List[dict]:
        return await SupabaseClient._get_backend().get_session_participants(session_id)

    @staticmethod
    async def bind_session_to_agent(session_id: str, agent_id: str) -> None:
        return await SupabaseClient._get_backend().bind_session_to_agent(session_id, agent_id)

    @staticmethod
    async def fetch_agent_by_id_with_context(
        agent_id: str,
        context_types: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict]:
        return await SupabaseClient._get_backend().fetch_agent_by_id_with_context(
            agent_id, context_types, user_id=user_id,
        )

    @staticmethod
    async def get_or_resolve_session_agent(
        session_id: str,
        user_id: str,
        template_id: Optional[str] = None,
    ) -> Optional[dict]:
        return await SupabaseClient._get_backend().get_or_resolve_session_agent(
            session_id, user_id, template_id,
        )
