-- Create session_interrupts table
CREATE TABLE IF NOT EXISTS session_interrupts (
    session_id UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    interrupt_requested BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- RLS
ALTER TABLE session_interrupts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can manage their own session interrupts"
    ON session_interrupts
    USING (
        session_id IN (
            SELECT id FROM sessions WHERE user_id = auth.uid()
        )
    );