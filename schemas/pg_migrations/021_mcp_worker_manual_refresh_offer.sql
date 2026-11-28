ALTER TABLE loom.mcp_worker_refresh_admission
    ADD COLUMN IF NOT EXISTS manual_incarnation uuid,
    ADD COLUMN IF NOT EXISTS manual_sequence bigint NOT NULL DEFAULT 0 CHECK (manual_sequence >= 0),
    ADD COLUMN IF NOT EXISTS manual_fingerprint text CHECK (manual_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD COLUMN IF NOT EXISTS manual_issued_at timestamptz,
    ADD COLUMN IF NOT EXISTS manual_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS manual_accepted_items smallint CHECK (manual_accepted_items BETWEEN 1 AND 64),
    ADD COLUMN IF NOT EXISTS manual_clock_at timestamptz;
