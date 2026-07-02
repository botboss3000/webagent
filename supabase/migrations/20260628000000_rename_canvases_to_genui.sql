-- Rename the canvases table to genui (Canvas → Gen UI).
-- Supabase/Postgres backend. Apply via the Supabase SQL editor or CLI.

ALTER TABLE IF EXISTS canvases RENAME TO genui;
ALTER INDEX IF EXISTS idx_canvases_user RENAME TO idx_genui_user;
