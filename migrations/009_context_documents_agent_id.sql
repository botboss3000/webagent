-- =============================================================================
-- Migration 009: Context rows keyed by agent_id (Supabase / Postgres)
--
-- Run manually on your Supabase project after backing up `public.context`.
-- Adjust UUID/TEXT types to match your existing `agents.id` and `context` schema.
-- =============================================================================

-- 1. Add nullable agent_id referencing the assigned agent
ALTER TABLE public.context
    ADD COLUMN IF NOT EXISTS agent_id uuid REFERENCES public.agents(id);

-- 2. Backfill from agents.user_id (legacy column on context)
UPDATE public.context AS c
SET agent_id = a.id
FROM public.agents AS a
WHERE c.user_id = a.user_id;

-- 3. Remove rows that could not be tied to an agent
DELETE FROM public.context WHERE agent_id IS NULL;

-- 4. Drop legacy user scope column (optional — comment out if you want to keep it temporarily)
ALTER TABLE public.context DROP COLUMN IF EXISTS user_id;

-- 5. Require agent_id on new rows (optional — uncomment after verifying data)
-- ALTER TABLE public.context ALTER COLUMN agent_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_context_agent ON public.context(agent_id);
