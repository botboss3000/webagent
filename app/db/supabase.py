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
from typing import Any, Dict, List, Optional
from supabase import create_client, Client
from app.models.schemas import InteractionRecord
from app.db.interface import StorageBackend

logger = logging.getLogger(__name__)


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
                .select("id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input, output, from_id, to_id, created_at")
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

    async def _seed_agent_templates_from_json_files(self) -> None:
        """
        Scan app/context/agents/*.json and seed each into
        agent_templates table with full schema (id, system_prompt,
        max_turn_count, model, provider, temperature, max_tokens,
        metadata). Upserts via insert+update to preserve existing
        rows while adding new ones.
        """
        from app.context.md_seeder import scan_agent_json_files
        templates = scan_agent_json_files()
        if not templates:
            return
        try:
            for tpl in templates:
                # Check if template exists
                existing = self._client.table("agent_templates").select(
                    "id"
                ).eq("id", tpl["id"]).limit(1).execute()
                new_data = {
                    "system_prompt": tpl["system_prompt"],
                    "max_turn_count": tpl["max_turn_count"],
                    "model": tpl["model"],
                    "provider": tpl["provider"],
                    "temperature": tpl["temperature"],
                    "max_tokens": tpl["max_tokens"],
                    "metadata": tpl["metadata"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if existing.data and len(existing.data) > 0:
                    self._client.table("agent_templates").update(
                        new_data
                    ).eq("id", tpl["id"]).execute()
                else:
                    new_data["id"] = tpl["id"]
                    new_data["created_at"] = datetime.now(timezone.utc).isoformat()
                    self._client.table("agent_templates").insert(
                        new_data
                    ).execute()
            logger.info(
                "Seeded %d agent template(s) from app/context/agents/*.json",
                len(templates),
            )
        except Exception as e:
            logger.debug(
                "Agent template seeding from JSON files failed: %s", e
            )

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
        try:
            res = (
                self._client.table("agents")
                .select("*")
                .eq("is_user_default", True)
                .contains("admin_users", [user_id])
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
                .eq("is_user_default", True)
                .contains("admin_users", [user_id])
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
            # Always seed from JSON files to ensure latest values
            await self._seed_agent_templates_from_json_files()

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

    async def seed_agent_templates(self) -> int:
        """Re-seed agent_templates from app/context/agents/*.json. Returns count."""
        await self._seed_agent_templates_from_json_files()
        try:
            res = (
                self._client.table("agent_templates")
                .select("id")
                .execute()
            )
            return len(res.data) if res.data else 0
        except Exception as e:
            logger.error("Error counting agent templates: %s", e)
            return 0

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
                "metadata": json.dumps(metadata or {}),
            })
            .execute()
        )
        logger.debug("Inserted attachment %s: %s", att_id, original_name)
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
    ) -> str:
        return await SupabaseClient._get_backend().insert_interaction(
            user_id, session_id, role, content, parent_id, tool_name, tool_call_id, channel, source, metadata, input_data, output_data, sender_id, receiver_id,
        )

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
