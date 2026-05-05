-- Migration 008: linking_codes table
-- Ephemeral codes for cross-device account linking.
-- TTL: 5 minutes, then auto-expired.

CREATE TABLE IF NOT EXISTS linking_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    source_user_id TEXT NOT NULL,
    target_channel TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    used BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_linking_codes_code ON linking_codes(code);
CREATE INDEX idx_linking_codes_expires ON linking_codes(expires_at) WHERE used = FALSE;

COMMENT ON TABLE linking_codes IS 'Ephemeral codes for cross-channel account linking (5 min TTL)';
