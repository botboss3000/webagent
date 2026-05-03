-- Migration 003: Add unique constraint to session_summaries.session_id
-- This is required for the UPSERT operation to correctly handle conflicts

ALTER TABLE session_summaries
ADD CONSTRAINT session_summaries_session_id_key UNIQUE (session_id);
