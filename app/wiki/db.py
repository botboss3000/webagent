"""
Dedicated, **always-local** SQLite store for the company-wide Wiki.

Deliberately separate from the main application database (``get_db()`` — SQLite
``local.db`` or a remote Postgres). The Wiki is a single shared knowledge base
that lives in its **own file**, ``data/wiki.db``, with its own WAL and its own
write lock — so it can be backed up, copied, or wiped independently of chat /
agent / memory data, and it stays a portable SQLite file even when the main
backend is a remote Postgres.

Schema (mirrors the memories + memory_chunks + FTS pattern):
  • ``wiki_articles``      — one row per article (title, body, tags, category,
                             created_by / updated_by, timestamps). slug is unique.
  • ``wiki_chunks``        — the body chunked + embedded (float32 BLOB) for
                             semantic search.
  • ``wiki_articles_fts``  — FTS5 keyword index, kept in sync by triggers.

Search is hybrid: FTS5 keyword + embedding cosine similarity, merged with
Reciprocal-Rank Fusion. Embeddings use the shared ``embed_text`` helper; when the
embedding API is unavailable, writes still succeed (no vector) and search
degrades to keyword-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.agent.embed import embed_text, EMBED_DIM

logger = logging.getLogger(__name__)

# Runtime DB file. The user-requested location is the project-root data/ dir.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WIKI_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "wiki.db")

_FTS5_QUERY_OPS = frozenset({"AND", "OR", "NOT", "NEAR"})

_np = None


def _ensure_np():
    global _np
    if _np is None:
        import numpy as np  # lazy: numpy import is heavy
        _np = np
    return _np


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


def _fts5_safe_match_query(raw: str, max_tokens: int = 12, max_token_len: int = 64) -> Optional[str]:
    """Turn free text into a conservative prefix OR-query safe for FTS5 MATCH."""
    if not raw or not raw.strip():
        return None
    tokens = re.findall(r"\w+", raw, flags=re.UNICODE)
    parts: List[str] = []
    for t in tokens:
        if len(t) < 2 or t.upper() in _FTS5_QUERY_OPS:
            continue
        if len(t) > max_token_len:
            t = t[:max_token_len]
        parts.append(f"{t}*")
        if len(parts) >= max_tokens:
            break
    if not parts:
        return None
    return " OR ".join(parts)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_articles (
    id          TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings
    category    TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',      -- user_id (attribution only)
    updated_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slug)
);

CREATE INDEX IF NOT EXISTS idx_wiki_articles_updated ON wiki_articles(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_wiki_articles_category ON wiki_articles(category);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_articles_fts USING fts5(
    slug UNINDEXED,
    title,
    body,
    tags,
    content='wiki_articles',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_wiki_articles_fts_insert
    AFTER INSERT ON wiki_articles BEGIN
    INSERT INTO wiki_articles_fts(rowid, slug, title, body, tags)
    VALUES (new.rowid, new.slug, new.title, new.body, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS trg_wiki_articles_fts_delete
    AFTER DELETE ON wiki_articles BEGIN
    INSERT INTO wiki_articles_fts(wiki_articles_fts, rowid, slug, title, body, tags)
    VALUES ('delete', old.rowid, old.slug, old.title, old.body, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS trg_wiki_articles_fts_update
    AFTER UPDATE ON wiki_articles BEGIN
    INSERT INTO wiki_articles_fts(wiki_articles_fts, rowid, slug, title, body, tags)
    VALUES ('delete', old.rowid, old.slug, old.title, old.body, old.tags);
    INSERT INTO wiki_articles_fts(rowid, slug, title, body, tags)
    VALUES (new.rowid, new.slug, new.title, new.body, new.tags);
END;

CREATE TABLE IF NOT EXISTS wiki_chunks (
    id            TEXT PRIMARY KEY,
    article_id    TEXT NOT NULL REFERENCES wiki_articles(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    embedding     BLOB,       -- numpy float32 array for local vector search
    token_count   INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(article_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_wiki_chunks_article ON wiki_chunks(article_id);
"""


class WikiStore:
    """Manages the dedicated local ``data/wiki.db`` SQLite file (own WAL + lock)."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or WIKI_DB_PATH
        self._write_lock = asyncio.Lock()
        self._init_db()

    # ── Connection ───────────────────────────────────────────────────────────
    # journal_mode=DELETE (rollback journal), NOT WAL: this DB file is tracked in
    # the repo, so it must stay a single self-contained file. WAL keeps recent
    # writes in a -wal sidecar until checkpoint, which would mean the committed
    # .db could miss data and leave transient -wal/-shm files lying around. With
    # DELETE mode the only sidecar is a short-lived -journal that exists solely
    # during a write transaction; between writes there is just wiki.db.
    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        except Exception:
            pass
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ── Row helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
        return d

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 500) -> List[str]:
        """Split text into ~max_chars chunks, breaking at sentence boundaries."""
        if len(text) <= max_chars:
            return [text]
        chunks: List[str] = []
        pos = 0
        while pos < len(text):
            end = min(pos + max_chars, len(text))
            if end < len(text):
                best_break = max(
                    text.rfind(". ", pos, end),
                    text.rfind(".\n", pos, end),
                    text.rfind("\n", pos, end),
                    text.rfind(" ", pos + max_chars // 2, end),
                )
                if best_break > pos + max_chars // 2:
                    end = best_break + 1
            chunk = text[pos:end].strip()
            if chunk:
                chunks.append(chunk)
            pos = end
        return chunks

    # ── Reads ────────────────────────────────────────────────────────────────
    async def list(self) -> List[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, slug, title, tags, category, created_by, updated_by, "
                "created_at, updated_at, substr(body, 1, 240) AS snippet "
                "FROM wiki_articles ORDER BY updated_at DESC",
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    async def get(self, slug: str) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM wiki_articles WHERE slug = ?", (slug,),
            ).fetchone()
            return self._row(row) if row else None
        finally:
            conn.close()

    # ── Writes ───────────────────────────────────────────────────────────────
    async def upsert(
        self,
        slug: str,
        title: str,
        body: str = "",
        tags: Optional[list] = None,
        category: str = "",
        user_id: str = "",
    ) -> dict:
        """Create or update an article (keyed by slug), then re-chunk + re-embed
        its body. The FTS row is kept in sync by triggers."""
        tags_json = json.dumps(tags or [])
        async with self._write_lock:
            conn = self._connect()
            try:
                now = _now_iso()
                existing = conn.execute(
                    "SELECT id FROM wiki_articles WHERE slug = ?", (slug,),
                ).fetchone()
                if existing:
                    article_id = existing["id"]
                    conn.execute(
                        "UPDATE wiki_articles SET title = ?, body = ?, tags = ?, "
                        "category = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                        (title, body, tags_json, category, user_id, now, article_id),
                    )
                else:
                    article_id = _uuid()
                    conn.execute(
                        "INSERT INTO wiki_articles (id, slug, title, body, tags, "
                        "category, created_by, updated_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (article_id, slug, title, body, tags_json, category,
                         user_id, user_id, now, now),
                    )
                conn.commit()
                await self._embed_and_store_chunks(conn, article_id, body)
                conn.commit()
                return self._row(conn.execute(
                    "SELECT * FROM wiki_articles WHERE id = ?", (article_id,),
                ).fetchone())
            finally:
                conn.close()

    async def delete(self, slug: str) -> bool:
        async with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM wiki_articles WHERE slug = ?", (slug,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def _embed_and_store_chunks(self, conn, article_id: str, text: str) -> None:
        conn.execute("DELETE FROM wiki_chunks WHERE article_id = ?", (article_id,))
        if not text or not text.strip():
            return
        for i, chunk in enumerate(self._chunk_text(text, max_chars=500)):
            if not chunk.strip():
                continue
            embedding_blob = None
            try:
                emb_list = await embed_text(chunk)
                embedding_blob = struct.pack(f"{len(emb_list)}f", *emb_list)
            except Exception as e:
                logger.warning("Wiki chunk embed failed (idx=%d art=%s): %s", i, article_id, e)
            conn.execute(
                "INSERT OR REPLACE INTO wiki_chunks "
                "(id, article_id, chunk_index, chunk_text, embedding, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_uuid(), article_id, i, chunk, embedding_blob, len(chunk.split())),
            )

    # ── Search (hybrid FTS + vector, RRF-merged) ─────────────────────────────
    async def search(self, query: str, limit: int = 10) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []
        fts_task = asyncio.create_task(self._fts5_search(query, limit * 3))
        vec_task = asyncio.create_task(self._vector_search(query, limit * 3))
        fts_results, vec_results = await asyncio.gather(
            fts_task, vec_task, return_exceptions=True
        )
        if isinstance(fts_results, BaseException):
            logger.warning("Wiki FTS5 search failed: %s", fts_results)
            fts_results = []
        if isinstance(vec_results, BaseException):
            logger.warning("Wiki vector search failed: %s", vec_results)
            vec_results = []

        if not vec_results:
            return fts_results[:limit] if fts_results else []
        if not fts_results:
            return vec_results[:limit]

        k = 60
        rrf: Dict[str, float] = {}
        for rank, a in enumerate(fts_results, start=1):
            rrf[a.get("slug", "")] = rrf.get(a.get("slug", ""), 0.0) + 1.0 / (k + rank)
        for rank, a in enumerate(vec_results, start=1):
            rrf[a.get("slug", "")] = rrf.get(a.get("slug", ""), 0.0) + 1.0 / (k + rank)

        all_arts: Dict[str, dict] = {}
        for a in fts_results + vec_results:
            s = a.get("slug", "")
            if s and s not in all_arts:
                all_arts[s] = a

        merged = []
        for slug, score in sorted(rrf.items(), key=lambda x: -x[1]):
            if slug in all_arts:
                entry = dict(all_arts[slug])
                entry["rank"] = round(score, 4)
                merged.append(entry)
        return merged[:limit]

    async def _fts5_search(self, query: str, limit: int = 10) -> List[dict]:
        match_expr = _fts5_safe_match_query(query)
        if not match_expr:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT w.id, w.slug, w.title, w.tags, w.category, w.updated_at, "
                "substr(w.body, 1, 240) AS snippet, rank "
                "FROM wiki_articles_fts fts "
                "JOIN wiki_articles w ON w.rowid = fts.rowid "
                "WHERE wiki_articles_fts MATCH ? ORDER BY rank LIMIT ?",
                (match_expr, limit),
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    async def _vector_search(self, query_text: str, limit: int = 10) -> List[dict]:
        np = _ensure_np()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT wc.article_id, wc.embedding, w.slug, w.title, w.tags, "
                "w.category, w.updated_at, substr(w.body, 1, 240) AS snippet "
                "FROM wiki_chunks wc JOIN wiki_articles w ON w.id = wc.article_id "
                "WHERE wc.embedding IS NOT NULL",
            ).fetchall()
            if not rows:
                return []
        finally:
            conn.close()

        try:
            query_vec = np.array(await embed_text(query_text), dtype=np.float32)
        except Exception as e:
            logger.warning("Wiki query embed failed, skipping vector search: %s", e)
            return []

        article_ids, vecs = [], []
        for r in rows:
            if r["embedding"]:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if vec.shape[0] == EMBED_DIM:
                    article_ids.append(r["article_id"])
                    vecs.append(vec)
        if not vecs:
            return []

        matrix = np.stack(vecs)
        norms = np.linalg.norm(matrix, axis=1)
        q_norm = np.linalg.norm(query_vec)
        scores = np.dot(matrix, query_vec) / (norms * q_norm + 1e-12)

        art_best: Dict[str, float] = {}
        for i, aid in enumerate(article_ids):
            s = float(scores[i])
            if aid not in art_best or s > art_best[aid]:
                art_best[aid] = s

        art_rows: Dict[str, dict] = {}
        for r in rows:
            aid = r["article_id"]
            if aid not in art_rows:
                art_rows[aid] = self._row(r)

        result = []
        for aid, score in sorted(art_best.items(), key=lambda x: -x[1]):
            if aid in art_rows:
                entry = dict(art_rows[aid])
                entry["rank"] = round(float(score), 4)
                entry.pop("embedding", None)
                entry.pop("article_id", None)
                result.append(entry)
                if len(result) >= limit:
                    break
        return result

    def path(self) -> str:
        return self._path


# ── Module singleton ──────────────────────────────────────────────────────────
_store: Optional[WikiStore] = None


def get_wiki_store() -> WikiStore:
    global _store
    if _store is None:
        _store = WikiStore()
    return _store
