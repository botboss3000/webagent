-- ── Migration 022: Add tutorial_prefs to user_profiles ─────────────────────
-- Stores the per-user tutorial walkthrough state as a JSON blob:
--   {"enabled": bool, "currentStep": int}
-- so the on/off toggle and step progress follow the user across devices
-- (previously these lived only in localStorage).
--
-- Safe to run multiple times. Existing rows get tutorial_prefs = NULL,
-- which the client treats as "use defaults" (enabled, step 1).
--
-- The local SQLite backend applies this automatically at startup.
-- ---------------------------------------------------------------------------

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS tutorial_prefs TEXT;
