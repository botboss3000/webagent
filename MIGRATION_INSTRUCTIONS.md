# webAgent - Database Migration Instructions

## Canonical schema (Python agent + Web Portal workspace)

Apply the single file **`Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`** to your Supabase project (SQL Editor, or CLI: `cd Web Portal` then `supabase db push` after `supabase link`).

The Python agent expects **`public.sessions`**, **`public.messages`**, **`public.context`**, **`public.memories`**, **`public.users`**, etc.—not the legacy `agent_*` tables.

**Do not** run `migration_final.sql` on the same database as the Web Portal. That file is **deprecated** and kept only as a historical reference (see header inside the file).

---

## Legacy reference only (do not use for monorepo deploy)

The following steps applied the old standalone agent schema. They are **not** valid when using the Web Portal migrations.

### Step 1 (LEGACY): Run the SQL Migration in Supabase

1. Go to your Supabase project
2. Click on **SQL Editor** in the left sidebar
3. Click **New Query**
4. Copy the entire contents below (or from `migration_final.sql`) — **legacy only**:

```sql
-- webAgent Agent Database Migration
-- Run this SQL in your Supabase SQL Editor (https://dwdezaasuejdrmzdsfqf.supabase.co)
-- This creates the required tables for the webAgent agent.

-- Table: agent_messages
create table if not exists public.agent_messages (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  session_id text not null,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  created_at timestamptz default now()
);

-- Index for fast lookups
create index if not exists agent_messages_user_session_created_idx 
  on public.agent_messages (user_id, session_id, created_at);

-- Table: agent_documents
create table if not exists public.agent_documents (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  doc_type text not null, -- 'agent', 'user_profile', 'skills', 'tools', 'jobs', 'memory', 'project'
  title text not null,
  content text not null,  -- markdown body
  tags text[] default '{}',
  linked_doc_ids uuid[] default '{}',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Index for document lookups
create index if not exists agent_documents_user_type_idx 
  on public.agent_documents (user_id, doc_type);

-- Function to automatically update updated_at timestamp
create or replace function public.update_updated_at_column()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

-- Trigger for agent_documents
drop trigger if exists update_agent_documents_updated_at on public.agent_documents;
create trigger update_agent_documents_updated_at
    before update on public.agent_documents
    for each row
    execute function public.update_updated_at_column();

-- Grant necessary permissions to service_role (required for Supabase service role key)
grant select, insert, update, delete on public.agent_messages to service_role;
grant select, insert, update, delete on public.agent_documents to service_role;

-- Optional: Enable Row Level Security (RLS) if you want per-user data isolation
-- alter table public.agent_messages enable row level security;
-- alter table public.agent_documents enable row level security;

-- Output confirmation
select '✅ webAgent tables created successfully' as result;
```

5. Click **Run** (or press Ctrl+Enter)

### Step 2 (LEGACY): Verify Tables Were Created

1. Go to **Table Editor** in the left sidebar
2. Legacy tables: `agent_messages` and `agent_documents`

### Step 3 (LEGACY): Test Your webAgent Application

After running the migration, you can test the full application:

```bash
# Start the server
cd ~/projects/webagent
.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# In another terminal, seed test documents
curl -X POST "http://localhost:8000/api/v1/seed-docs?user_id=test_user"

# Test the chat endpoint
curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test_user",
    "session_id": "session_123",
    "message": "Hello webAgent!"
  }'
```

## Step 4: Use the Web Interface

1. Open `test_interface.html` in your browser
2. Click **"Seed Test Documents"**
3. Start chatting!

## Troubleshooting

If you get permission errors after creating tables:
1. Go to **Authentication > Policies** in Supabase
2. Ensure `service_role` has access to the tables
3. Or run these SQL commands in the SQL Editor:

```sql
-- webAgent Agent Database Migration
-- Run this SQL in your Supabase SQL Editor (https://dwdezaasuejdrmzdsfqf.supabase.co)
-- This creates the required tables for the webAgent agent.

-- Table: agent_messages
create table if not exists public.agent_messages (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  session_id text not null,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  created_at timestamptz default now()
);

-- Index for fast lookups
create index if not exists agent_messages_user_session_created_idx 
  on public.agent_messages (user_id, session_id, created_at);

-- Table: agent_documents
create table if not exists public.agent_documents (
  id uuid primary key default gen_random_uuid(),
  user_id text not null,
  doc_type text not null, -- 'agent', 'user_profile', 'skills', 'tools', 'jobs', 'memory', 'project'
  title text not null,
  content text not null,  -- markdown body
  tags text[] default '{}',
  linked_doc_ids uuid[] default '{}',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Index for document lookups
create index if not exists agent_documents_user_type_idx 
  on public.agent_documents (user_id, doc_type);

-- Function to automatically update updated_at timestamp
create or replace function public.update_updated_at_column()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

-- Trigger for agent_documents
drop trigger if exists update_agent_documents_updated_at on public.agent_documents;
create trigger update_agent_documents_updated_at
    before update on public.agent_documents
    for each row
    execute function public.update_updated_at_column();

-- Grant necessary permissions to service_role (required for Supabase service role key)
grant select, insert, update, delete on public.agent_messages to service_role;
grant select, insert, update, delete on public.agent_documents to service_role;

-- Optional: Enable Row Level Security (RLS) if you want per-user data isolation
-- alter table public.agent_messages enable row level security;
-- alter table public.agent_documents enable row level security;

-- Output confirmation
select '✅ webAgent tables created successfully' as result;
```

## Next: Connect Your Web App

Your web app can now connect to `http://localhost:8000/api/v1/chat` (or your deployed URL).

The environment variable from your web app: `PYTHON_AGENT_URL=http://localhost:8000` is already correct!