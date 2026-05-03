-- Migration 001: agent_tools table
-- Stores all tool versions: official versions and user forks

CREATE TABLE IF NOT EXISTS agent_tools (
    -- Unique row ID (UUID)
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Tool name identifier, e.g., 'yahoo_email', 'google_calendar'
    name TEXT NOT NULL,
    
    -- Version string: integer for official, hyphenated for forks
    -- Official: '1', '2', '3'
    -- Fork: '3-123' (based on official version 3, by user 123)
    version TEXT NOT NULL,
    
    -- Full Python code of the tool (function definition)
    code TEXT NOT NULL,
    
    -- Description shown to the model in tool list
    description TEXT NOT NULL,
    
    -- JSON Schema for tool parameters
    parameters JSONB NOT NULL DEFAULT '{}'::JSONB,
    
    -- Brief summary of changes in this version
    change_summary TEXT DEFAULT '',
    
    -- Parent version UUID (if this is a fork)
    parent_version UUID REFERENCES agent_tools(id) ON DELETE SET NULL,
    
    -- Status lifecycle
    status TEXT NOT NULL DEFAULT 'personal_draft' 
        CHECK (status IN ('personal_draft', 'pending_review', 'verified', 'deprecated')),
    
    -- Creator/modifier
    created_by TEXT NOT NULL,
    
    -- Flag: true for the latest version per (name, status, created_by) group
    is_latest BOOLEAN NOT NULL DEFAULT FALSE,
    
    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Constraints
    UNIQUE(name, version, created_by),  -- No duplicate version per creator
    CONSTRAINT unique_latest_per_status_per_creator UNIQUE(name, created_by, status)  -- Exactly one latest per status per creator
);

-- Indexes for common queries
CREATE INDEX idx_agent_tools_name_version ON agent_tools(name, version);
CREATE INDEX idx_agent_tools_created_by ON agent_tools(created_by);
CREATE INDEX idx_agent_tools_status ON agent_tools(status);
CREATE INDEX idx_agent_tools_is_latest ON agent_tools(is_latest) WHERE is_latest = TRUE;

-- Trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_agent_tools_updated_at
BEFORE UPDATE ON agent_tools
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Insert built‑in tools as verified versions (seed data)
INSERT INTO agent_tools (
    name, version, code, description, parameters, status, created_by, is_latest
) VALUES
-- Built‑in create_tool tool — the tool that builds more tools
-- Note: loader.py overrides this handler with app/tools/registry.py at runtime
-- to inject user_id automatically. This seed is only a fallback.
(
    'create_tool',
    '1',
    $CODE$
async def create_tool(name, description, parameters, code, change_summary=None):
    """
    Create or update a tool in the agent tools library.
    
    Args:
        name: Tool identifier (e.g. 'check_email')
        description: What the tool does (shown to model)
        parameters: JSON Schema describing tool inputs
        code: Full Python async function code. Must contain an async function
              with the same name as the tool.
        change_summary: Optional description of changes
    """
    from app.tools.registry import create_tool as _impl
    return await _impl(
        name=name, description=description, parameters=parameters,
        code=code, change_summary=change_summary, user_id="system",
    )
$CODE$,
    'Create or update a tool in the agent tools library. Auto-increments version on updates.',
    '{
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Tool identifier (e.g. check_email)"},
            "description": {"type": "string", "description": "What the tool does (shown to model)"},
            "parameters": {"type": "object", "description": "JSON Schema describing tool inputs"},
            "code": {"type": "string", "description": "Full Python async function code. Must contain an async function with the same name as the tool."},
            "change_summary": {"type": "string", "description": "Optional description of changes made"}
        },
        "required": ["name", "description", "parameters", "code"]
    }'::JSONB,
    'verified',
    'system',
    TRUE
),
-- Built‑in browser_open tool for credential/auth flows
(
    'browser_open',
    '1',
    $CODE$
async def browser_open(url, session_id=None):
    """
    Open a browser to a given URL, typically for authentication flows.
    
    Args:
        url: The URL to open
        session_id: Optional session identifier for cookie storage
    """
    import webbrowser
    webbrowser.open(url)
    return f"Browser opened to {url}"
$CODE$,
    'Open a browser to a given URL, typically for authentication flows',
    '{
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to open"},
            "session_id": {"type": "string", "description": "Optional session identifier"}
        },
        "required": ["url"]
    }'::JSONB,
    'verified',
    'system',
    TRUE
)
ON CONFLICT (name, version, created_by) DO NOTHING;