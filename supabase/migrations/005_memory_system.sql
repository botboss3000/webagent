-- Migration 005: Memory System (knowledge brain)
-- Replaces the old flat memories table with a structured knowledge base.
-- Requires: pgvector extension

-- ============================================================
-- 1. memories — core content table (compiled truth + timeline)
-- ============================================================
DROP TABLE IF EXISTS memories CASCADE;

CREATE TABLE IF NOT EXISTS memories (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    slug            TEXT NOT NULL CHECK (slug ~* '^[a-z0-9/._-]+$'),
    page_type       TEXT NOT NULL CHECK (page_type IN (
                        'person', 'company', 'deal', 'meeting', 'project',
                        'idea', 'concept', 'writing', 'program', 'personal',
                        'media', 'inbox', 'archive'
                    )),
    title           TEXT NOT NULL,
    compiled_truth  TEXT NOT NULL DEFAULT '',
    timeline        TEXT NOT NULL DEFAULT '',
    frontmatter     JSONB NOT NULL DEFAULT '{}',
    content_hash    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(page_type);
CREATE INDEX IF NOT EXISTS idx_memories_frontmatter ON memories USING GIN(frontmatter);
CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);

-- Full-text search vector
ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' ||
            coalesce(compiled_truth, '') || ' ' ||
            coalesce(timeline, '')
        )
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_memories_fts ON memories USING GIN(search_vector);

-- Fuzzy slug matching
CREATE INDEX IF NOT EXISTS idx_memories_trgm ON memories USING GIN(title gin_trgm_ops);

CREATE OR REPLACE FUNCTION update_memories_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_memories_updated ON memories;
CREATE TRIGGER trg_memories_updated
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION update_memories_updated_at();


-- ============================================================
-- 2. memory_chunks — chunked text with vector embeddings
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_chunks (
    id            SERIAL PRIMARY KEY,
    memory_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    chunk_source  TEXT NOT NULL DEFAULT 'compiled_truth'
                  CHECK (chunk_source IN ('compiled_truth', 'timeline')),
    embedding     VECTOR(1536),
    token_count   INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(memory_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_memory ON memory_chunks(memory_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON memory_chunks(chunk_source);

-- HNSW index for vector search. HNSW builds fine on an empty table (no training
-- pass, unlike ivfflat), so it can be created up front. Name + opclass match what
-- the app's DDL renderer emits (idx_memory_chunks_embedding_hnsw, vector_cosine_ops
-- for the `<=>` cosine operator) so IF NOT EXISTS dedupes if both paths run.
CREATE INDEX IF NOT EXISTS idx_memory_chunks_embedding_hnsw
    ON memory_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);


-- ============================================================
-- 3. memory_links — typed knowledge graph edges
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_links (
    id            SERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    from_slug     TEXT NOT NULL,
    to_slug       TEXT NOT NULL,
    link_type     TEXT NOT NULL CHECK (link_type IN (
                      'works_at', 'founded', 'invested_in', 'advises',
                      'attended', 'knows', 'partnered_with', 'acquired',
                      'competes_with', 'references', 'related_to'
                  )),
    context       TEXT,
    weight        INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, from_slug, to_slug, link_type)
);

CREATE INDEX IF NOT EXISTS idx_links_from ON memory_links(from_slug);
CREATE INDEX IF NOT EXISTS idx_links_to ON memory_links(to_slug);
CREATE INDEX IF NOT EXISTS idx_links_type ON memory_links(link_type);
CREATE INDEX IF NOT EXISTS idx_links_user ON memory_links(user_id);


-- ============================================================
-- 4. memory_timeline — append-only event log
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_timeline (
    id            SERIAL PRIMARY KEY,
    memory_id     INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    event_date    DATE NOT NULL,
    source        TEXT NOT NULL,       -- 'chat', 'email', 'meeting', 'tweet', 'web', 'manual'
    summary       TEXT NOT NULL,
    detail        TEXT,
    source_ref    TEXT,               -- optional URL, file path, or interaction_id
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_timeline_memory ON memory_timeline(memory_id);
CREATE INDEX IF NOT EXISTS idx_timeline_date ON memory_timeline(event_date DESC);
