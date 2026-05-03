-- Migration 002: Add agent tools schema
-- Enhances: memories, messages tables
-- Adds: pgvector, session_summaries, skills tables
-- For: web_search, db_query, memory, session_search/recall tools

-- ============================================================
-- Part 0: Enable required extensions first
-- ============================================================
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- Part 1: Enhance memories table with Hermes-like features
-- ============================================================
ALTER TABLE memories
  ADD COLUMN IF NOT EXISTS memory_type text DEFAULT 'general',
  ADD COLUMN IF NOT EXISTS priority integer DEFAULT 5,
  ADD COLUMN IF NOT EXISTS access_count integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_accessed timestamptz DEFAULT now(),
  ADD COLUMN IF NOT EXISTS is_active boolean DEFAULT true;

-- Index for active memory queries by priority
CREATE INDEX IF NOT EXISTS memories_active_priority_idx
  ON memories (user_id, priority DESC, last_accessed DESC)
  WHERE is_active = true;

-- Trigram index for text search on memories
CREATE INDEX IF NOT EXISTS memories_content_trgm_idx
  ON memories USING gin (content gin_trgm_ops);

-- ============================================================
-- Part 2: Add embedding columns for semantic search
-- ============================================================
ALTER TABLE messages
  ADD COLUMN IF NOT EXISTS embedding vector(384);

-- Index for vector similarity search on messages
CREATE INDEX IF NOT EXISTS messages_embedding_idx
  ON messages USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);

-- Trigram index for text search on messages
CREATE INDEX IF NOT EXISTS messages_content_trgm_idx
  ON messages USING gin (content gin_trgm_ops);

-- ============================================================
-- Part 3: Session summaries table for cross-session recall
-- ============================================================
CREATE TABLE IF NOT EXISTS session_summaries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id),
  session_id uuid NOT NULL REFERENCES sessions(id),
  title text,
  summary text NOT NULL,
  tags text[] DEFAULT '{}',
  embedding vector(384),
  message_count integer DEFAULT 0,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS session_summaries_user_idx
  ON session_summaries (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS session_summaries_embedding_idx
  ON session_summaries USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);

ALTER TABLE session_summaries ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- Part 4: Skills table (for future procedural memory / skill system)
-- ============================================================
CREATE TABLE IF NOT EXISTS skills (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id),
  name text NOT NULL,
  category text,
  description text,
  trigger_conditions text[] DEFAULT '{}',
  content text NOT NULL,
  usage_count integer DEFAULT 0,
  last_used timestamptz,
  success_rate float DEFAULT 1.0,
  is_active boolean DEFAULT true,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS skills_user_active_idx
  ON skills (user_id, usage_count DESC)
  WHERE is_active = true;
