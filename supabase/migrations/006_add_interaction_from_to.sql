-- Add from_id and to_id columns to interactions table
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS from_id TEXT;
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS to_id TEXT;