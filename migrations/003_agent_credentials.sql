-- Migration 003: agent_credentials table
-- Encrypted credential storage for user-specific API keys, OAuth tokens, etc.

CREATE TABLE IF NOT EXISTS agent_credentials (
    -- Unique row ID
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Owner
    user_id TEXT NOT NULL,
    
    -- Which tool this credential belongs to
    tool_name TEXT NOT NULL,
    
    -- Credential type
    credential_type TEXT NOT NULL DEFAULT 'api_key'
        CHECK (credential_type IN ('api_key', 'oauth_token', 'session_cookie', 'password', 'app_password')),
    
    -- Encrypted credential data (using pgcrypto)
    encrypted_data TEXT NOT NULL,
    
    -- Metadata about the credential
    display_name TEXT,
    expires_at TIMESTAMP WITH TIME ZONE,
    scopes TEXT[],  -- For OAuth: array of scope strings
    
    -- Tracking usage
    last_used_at TIMESTAMP WITH TIME ZONE,
    use_count INTEGER DEFAULT 0,
    
    -- Security flags
    is_active BOOLEAN DEFAULT TRUE,
    requires_renewal BOOLEAN DEFAULT FALSE,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Constraints
    UNIQUE(user_id, tool_name, credential_type)  -- One credential type per tool per user
);

-- Indexes
CREATE INDEX idx_agent_credentials_user_tool ON agent_credentials(user_id, tool_name);
CREATE INDEX idx_agent_credentials_expires ON agent_credentials(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_agent_credentials_is_active ON agent_credentials(is_active) WHERE is_active = TRUE;

-- Trigger for updated_at
CREATE TRIGGER update_agent_credentials_updated_at
BEFORE UPDATE ON agent_credentials
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Enable pgcrypto if not already enabled
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Example of how to encrypt/decrypt (for reference, not part of table creation):
-- INSERT INTO agent_credentials (user_id, tool_name, encrypted_data)
-- VALUES ('user123', 'yahoo_email', pgp_sym_encrypt('my_app_password', current_setting('app.encryption_key')));
--
-- SELECT pgp_sym_decrypt(encrypted_data::bytea, current_setting('app.encryption_key'))
-- FROM agent_credentials WHERE id = '...';

COMMENT ON TABLE agent_credentials IS 'Encrypted credential storage for user-specific tool authentication';