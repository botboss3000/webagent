-- Migration 009: add channel column to interactions
-- Tracks the source channel for every interaction row.
-- Values: 'webagent_ui' | 'telegram' | 'web_portal' | 'api'

ALTER TABLE interactions ADD COLUMN channel TEXT;
