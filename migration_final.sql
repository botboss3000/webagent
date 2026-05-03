-- =============================================================================
-- DEPRECATED for the webAgent monorepo (Python agent + Web Portal workspace).
-- Canonical schema lives in: Web Portal/supabase/migrations/
-- Do NOT run this file on the same Supabase project as the Web Portal — it
-- creates legacy agent_messages / agent_documents tables that the apps no longer use.
-- Kept only for historical reference or isolated experiments.
-- =============================================================================

-- webAgent Agent Database Migration (LEGACY)
-- Run this SQL in your Supabase SQL Editor (https://dwdezaasuejdrmzdsfqf.supabase.co)
-- This creates the required tables for the LangGraph agent.

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