-- The erasures requested of this deployment, recorded in Postgres beside ClickHouse's own
-- ledger (loom.erasure_requests), so that after a restore of either store the erasures
-- it lost are run again from the other (`loom reconcile`,
-- docs/operations/backup-and-recovery.md). A row holds identifiers only: the scope the
-- erasure matched, as erasure_requests holds it. Kept LOOM_ERASURE_LEDGER_DAYS.
CREATE TABLE IF NOT EXISTS loom.erasure_ledger (
    org text COLLATE "C" NOT NULL,
    id uuid NOT NULL,
    scope_type text NOT NULL,
    scope_value text NOT NULL,
    purge boolean NOT NULL DEFAULT false,
    requester text NOT NULL DEFAULT '',
    urgency text NOT NULL DEFAULT 'normal',
    requested_at timestamptz NOT NULL,
    -- The saga's Postgres steps that committed, each recorded in its own transaction: a
    -- restored copy says which ran before its point, which a replay does not run again.
    pg_steps text[] NOT NULL DEFAULT '{}',
    -- When the saga completed; an abandoned one (out of attempts) has `abandoned_at`
    -- instead, and one with neither is still running or awaits its recovery.
    completed_at timestamptz,
    abandoned_at timestamptz,
    PRIMARY KEY (org, id)
);

-- The sweep's and the backfill's order across tenants.
CREATE INDEX IF NOT EXISTS erasure_ledger_requested ON loom.erasure_ledger (requested_at);
