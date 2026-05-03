# SQL migrations (Python agent)

The **single canonical migration** for the whole monorepo (Web Portal + this agent) lives here:

**[`Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`](../../Web%20Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql)**

Apply it on a new Supabase project via the Dashboard SQL editor, or from the `Web Portal` folder with the Supabase CLI (`supabase link` then `supabase db push`).

Do not maintain a duplicate SQL copy in this folder.
