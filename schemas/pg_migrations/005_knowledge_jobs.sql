-- The Postgres authority for knowledge ingestion jobs: each job's row, its queue fields,
-- the token that fences its saves and whether a cancel was asked for.
-- loom.knowledge_ingestions in ClickHouse holds a projection of every version saved
-- here. docs/architecture/storage-authority.md describes the table.

-- One row per job. `row` is the full job (every column of loom.knowledge_ingestions);
-- the queue reads the columns kept beside it. `lease_token` fences a run: each take
-- raises it, and a save lands only under the token its run took.
CREATE TABLE IF NOT EXISTS loom.knowledge_jobs (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    ingestion_id text COLLATE "C" NOT NULL,
    source_uri text COLLATE "C" NOT NULL,
    state text NOT NULL,
    next_attempt_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    version bigint NOT NULL,
    lease_token bigint NOT NULL DEFAULT 0,
    cancel_requested_at timestamptz,
    row jsonb NOT NULL,
    PRIMARY KEY (org, namespace, ingestion_id)
) WITH (fillfactor = 80);
-- The queue: each source's open jobs, oldest first.
CREATE INDEX IF NOT EXISTS knowledge_jobs_open
    ON loom.knowledge_jobs (org, namespace, source_uri, created_at, ingestion_id)
    WHERE state IN ('pending', 'processing');
-- When the queue last served each namespace.
CREATE INDEX IF NOT EXISTS knowledge_jobs_served
    ON loom.knowledge_jobs (org, namespace, updated_at) WHERE state <> 'pending';
-- A user erasure's jobs across the org's namespaces.
CREATE INDEX IF NOT EXISTS knowledge_jobs_by_user ON loom.knowledge_jobs (org, user_id);
-- The expiry of jobs no run has saved for 30 days, as ClickHouse's TTL drops them.
CREATE INDEX IF NOT EXISTS knowledge_jobs_by_update ON loom.knowledge_jobs (updated_at);
-- Rewritten at every stage of a run: vacuumed by a fixed count of dead rows.
ALTER TABLE loom.knowledge_jobs
    SET (autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 1000);
