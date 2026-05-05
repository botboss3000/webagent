-- Migration 007: channel_identities table
-- Maps external communication identities to internal user IDs.
-- Supports three tiers: anonymous, channel_verified, full.

CREATE TABLE IF NOT EXISTS channel_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL,                -- 'telegram', 'whatsapp', 'sms'
    external_id TEXT NOT NULL,            -- chat_id, phone number, etc.
    user_id TEXT NOT NULL,                -- internal: 'telegram:12345' or 'user:uuid'
    user_tier TEXT NOT NULL DEFAULT 'anonymous'
        CHECK (user_tier IN ('anonymous', 'channel_verified', 'full')),
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    email_verified BOOLEAN DEFAULT FALSE,
    linked_user_id UUID,                  -- UUID in users table (for cross-device)
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(channel, external_id)
);

CREATE INDEX idx_channel_identities_user_id ON channel_identities(user_id);
CREATE INDEX idx_channel_identities_channel_ext ON channel_identities(channel, external_id);
CREATE INDEX idx_channel_identities_tier ON channel_identities(user_tier);

CREATE TRIGGER update_channel_identities_updated_at
BEFORE UPDATE ON channel_identities
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE channel_identities IS 'Maps external communication identities to internal user IDs with tiered access';
