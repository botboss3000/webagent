-- Migration 002: agent_tool_executions table
-- Logs every tool call for performance tracking, success rates, debugging

CREATE TABLE IF NOT EXISTS agent_tool_executions (
    -- Unique row ID
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Which tool version was executed (references agent_tools.id)
    tool_id UUID NOT NULL REFERENCES agent_tools(id) ON DELETE CASCADE,
    
    -- Denormalized for fast queries (avoid joins)
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    
    -- Who executed it
    user_id TEXT NOT NULL,
    
    -- Which conversation/session
    session_id TEXT NOT NULL,

    -- Execution outcome
    success BOOLEAN NOT NULL,

    -- Duration in milliseconds
    duration_ms INTEGER NOT NULL,

    -- Error message if success = FALSE
    error_message TEXT,

    -- Input parameters (stored for debugging, truncated if large)
    input_params JSONB DEFAULT '{}'::JSONB,

    -- Output result (truncated to avoid huge storage)
    output_result TEXT,

    -- Timestamp
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_tool_executions_tool_name ON agent_tool_executions (tool_name);
CREATE INDEX idx_tool_executions_user_id ON agent_tool_executions (user_id);
CREATE INDEX idx_tool_executions_session_id ON agent_tool_executions (session_id);
CREATE INDEX idx_tool_executions_created_at ON agent_tool_executions (created_at);
CREATE INDEX idx_tool_executions_success ON agent_tool_executions (success);

-- Create a view for tool performance stats
CREATE OR REPLACE VIEW agent_tool_performance AS
SELECT
    tool_id,
    tool_name,
    tool_version,
    COUNT(*) AS total_executions,
    SUM(CASE WHEN success THEN 1 ELSE 0 END) AS successful_executions,
    ROUND(100.0 * SUM(CASE WHEN success THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_percent,
    AVG(duration_ms) AS avg_duration_ms,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_ms) AS median_duration_ms,
    MIN(duration_ms) AS min_duration_ms,
    MAX(duration_ms) AS max_duration_ms,
    MIN(created_at) AS first_executed,
    MAX(created_at) AS last_executed
FROM agent_tool_executions
GROUP BY tool_id, tool_name, tool_version;

-- Create a view for user-specific tool performance
CREATE OR REPLACE VIEW user_tool_performance AS
SELECT
    user_id,
    tool_name,
    COUNT(*) AS total_executions,
    SUM(CASE WHEN success THEN 1 ELSE 0 END) AS successful_executions,
    ROUND(100.0 * SUM(CASE WHEN success THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_percent,
    AVG(duration_ms) AS avg_duration_ms
FROM agent_tool_executions
GROUP BY user_id, tool_name;

-- Retention policy: older than 90 days can be archived or deleted
-- (Implement via cron job or Supabase Edge Function later)

COMMENT ON TABLE agent_tool_executions IS 'Logs every tool call for performance tracking and debugging';