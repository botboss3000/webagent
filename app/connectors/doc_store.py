"""
Document store connector.

Holds a logical "folder" of files (markdown, txt, PDF text). Files are chunked
and embedded into the `doc_chunks` table (FTS5 + vector). The agent gets a
single `search_<name>` tool that runs hybrid keyword + vector search scoped
to this data source.

Phase-1 storage backends:
  * local filesystem path (dev mode)
  * Supabase Storage bucket (cloud mode) — TODO: wire through file_store.py

Ingestion is triggered by the admin API (`POST /api/v1/data-sources/{id}/ingest`)
rather than implicit-on-query, so the model doesn't get blocked waiting on
embeddings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.connectors.base import Connector, GeneratedTool

FEATURE = {
    "id": "doc_store",
    "display_name": "Document store",
    "category": "connector",
    "status": "beta",
    "summary": "Chunk + embed a document folder for hybrid keyword/vector search.",
}

logger = logging.getLogger(__name__)


def _chunk_text(text: str, max_chars: int = 500) -> List[str]:
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    out = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_chars, len(text))
        if end < len(text):
            best = max(
                text.rfind(". ", pos, end),
                text.rfind(".\n", pos, end),
                text.rfind("\n", pos, end),
            )
            if best > pos + max_chars // 2:
                end = best + 1
        chunk = text[pos:end].strip()
        if chunk:
            out.append(chunk)
        pos = end
    return out


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("doc_store: failed to read %s: %s", path, e)
        return ""


class DocStoreConnector(Connector):
    type_name = "doc_store"
    display_name = "Document folder (markdown, txt, PDF text)"
    requires_credential = False

    async def test_connection(self, data_source, auth_resolver):
        cfg = data_source.get("config") or {}
        backend = cfg.get("backend") or "local"
        if backend == "local":
            base = cfg.get("path") or ""
            p = Path(base)
            if not p.exists():
                return {"ok": False, "message": f"path does not exist: {base}", "details": {}}
            if not p.is_dir():
                return {"ok": False, "message": f"path is not a directory: {base}", "details": {}}
            return {"ok": True, "message": "directory accessible", "details": {"path": str(p)}}
        if backend == "supabase_storage":
            # Bucket existence check is delegated to file_store.py; succeed loudly here.
            return {"ok": True, "message": "supabase storage backend (file check on ingest)", "details": {}}
        return {"ok": False, "message": f"unknown backend: {backend}", "details": {}}

    async def introspect(self, data_source, auth_resolver):
        from app.db import get_db

        db = get_db()
        files = await self._list_files(data_source)
        try:
            chunk_count = await db.doc_chunk_count(data_source["id"])
        except Exception:
            chunk_count = 0
        return {
            "files": files[:200],
            "file_count": len(files),
            "chunk_count": chunk_count,
        }

    def generated_tools(self, data_source, attachment, auth_resolver):
        tool_name = self.default_tool_name(data_source, attachment)
        if not tool_name.startswith("search_"):
            tool_name = f"search_{tool_name}"

        async def _handler(query: str, limit: int = 5):
            from app.db import get_db

            db = get_db()
            try:
                results = await db.doc_chunk_search(data_source["id"], query, limit=int(limit))
            except Exception as e:
                return json.dumps({"status": "error", "message": str(e)})
            return json.dumps({
                "status": "ok",
                "query": query,
                "result_count": len(results),
                "results": results,
            }, default=str)

        return [
            GeneratedTool(
                name=tool_name,
                description=(
                    f"Hybrid keyword + semantic search across the `{data_source.get('name')}` "
                    f"document store. Returns the most relevant text chunks."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language search query"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    },
                    "required": ["query"],
                },
                handler=_handler,
                destructive=False,
                metadata={"connector": self.type_name, "data_source_id": data_source.get("id")},
            )
        ]

    def prompt_snippet(self, data_source, attachment):
        name = data_source.get("name", "docs")
        cache = data_source.get("schema_cache") or {}
        fc = cache.get("file_count")
        cc = cache.get("chunk_count")
        bits = []
        if fc is not None:
            bits.append(f"{fc} files")
        if cc is not None:
            bits.append(f"{cc} chunks")
        suffix = f" ({', '.join(bits)})" if bits else ""
        return (
            f"Document store `{name}`{suffix}: use the search tool to look up topics, "
            f"FAQs, or any text in this folder."
        )

    # ---- ingestion (called by API, not by the agent loop) ----

    async def ingest(self, data_source: dict) -> Dict[str, Any]:
        """Re-scan source files, hash, chunk, embed, and write into doc_chunks.

        Returns a summary {files_seen, files_changed, chunks_written, errors}.
        """
        from app.agent.embed import embed_text
        from app.db import get_db

        db = get_db()
        ds_id = data_source["id"]
        cfg = data_source.get("config") or {}
        files = await self._list_files(data_source)
        files_seen = len(files)
        files_changed = 0
        chunks_written = 0
        errors: List[str] = []

        for entry in files:
            try:
                path = Path(entry["path"])
                if not path.exists() or not path.is_file():
                    continue
                file_hash = _file_hash(path)
                # Skip if hash already on every chunk for this source_ref
                existing_hash = await self._existing_hash(ds_id, entry["source_ref"])
                if existing_hash == file_hash:
                    continue
                # Replace chunks for this file
                await db.doc_chunk_delete_by_source_ref(ds_id, entry["source_ref"])
                text = _read_text_file(path)
                if not text.strip():
                    continue
                for i, piece in enumerate(_chunk_text(text)):
                    emb: Optional[List[float]] = None
                    try:
                        emb = await embed_text(piece)
                    except Exception as e:
                        errors.append(f"embed:{entry['source_ref']}#{i}: {e}")
                    await db.doc_chunk_upsert(
                        data_source_id=ds_id,
                        source_ref=entry["source_ref"],
                        chunk_index=i,
                        chunk_text=piece,
                        content_hash=file_hash,
                        embedding=emb,
                        metadata={"file": entry["source_ref"]},
                    )
                    chunks_written += 1
                files_changed += 1
            except Exception as e:
                errors.append(f"{entry.get('source_ref','?')}: {e}")
        return {
            "files_seen": files_seen,
            "files_changed": files_changed,
            "chunks_written": chunks_written,
            "errors": errors,
        }

    # ---- helpers ----

    @staticmethod
    async def _existing_hash(data_source_id: str, source_ref: str) -> Optional[str]:
        """Return the content_hash of the first chunk for this file, if any."""
        try:
            from app.db import get_db

            db = get_db()
            # Reuse the SQLite proxy fallback for portability.
            client = db.get_raw_client()
            res = (
                client.table("doc_chunks")
                .select("content_hash")
                .eq("data_source_id", data_source_id)
                .eq("source_ref", source_ref)
                .limit(1)
                .execute()
            )
            if res.data:
                return res.data[0].get("content_hash")
        except Exception:
            return None
        return None

    @staticmethod
    async def _list_files(data_source: dict) -> List[dict]:
        cfg = data_source.get("config") or {}
        backend = cfg.get("backend") or "local"
        if backend == "local":
            base = cfg.get("path") or ""
            base_p = Path(base)
            if not base_p.exists():
                return []
            allowed_exts = set(
                ext.lower().lstrip(".")
                for ext in (cfg.get("extensions") or ["md", "txt", "markdown"])
            )
            out: List[dict] = []
            for path in base_p.rglob("*"):
                if not path.is_file():
                    continue
                ext = path.suffix.lower().lstrip(".")
                if ext not in allowed_exts:
                    continue
                rel = str(path.relative_to(base_p))
                out.append({
                    "source_ref": rel,
                    "path": str(path),
                    "size": path.stat().st_size,
                })
            return out
        if backend == "supabase_storage":
            try:
                from app.db import get_db

                client = get_db().get_raw_client()
                bucket = cfg.get("bucket") or ""
                prefix = cfg.get("prefix") or ""
                if not bucket:
                    return []
                listing = client.storage.from_(bucket).list(prefix or None)
                out = []
                for item in listing or []:
                    name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
                    if not name:
                        continue
                    out.append({"source_ref": name, "path": f"supabase://{bucket}/{name}", "size": None})
                return out
            except Exception as e:
                logger.warning("doc_store supabase list failed: %s", e)
                return []
        return []
