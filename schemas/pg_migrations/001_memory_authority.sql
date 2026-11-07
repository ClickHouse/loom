-- The Postgres memory authority: current content, the revision journal, the
-- deliveries still owed to ClickHouse, scope generations and idempotency.
-- docs/architecture/storage-authority.md describes each table.

CREATE SCHEMA IF NOT EXISTS loom;

-- Key columns compare by byte (COLLATE "C"): the order Loom takes row locks in,
-- and ClickHouse's.

-- The deployment's recorded authority. One row; a replica configured for
-- another authority, or pointed at another ClickHouse, refuses to start.
CREATE TABLE IF NOT EXISTS loom.authority (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    authority text NOT NULL,
    epoch bigint NOT NULL,
    deployment_id text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

-- The last revision sequence number of each scope. A namespace's row orders
-- its revisions and is what an admission captures and rechecks; scope '*'
-- is the org's policy generation (blacklist, access), org '' the global one.
CREATE TABLE IF NOT EXISTS loom.scope_generations (
    org text COLLATE "C" NOT NULL,
    scope text COLLATE "C" NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (org, scope)
);

-- The live head of every key: the complete loom.memory row a read serves.
CREATE TABLE IF NOT EXISTS loom.memory_current (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    agent text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    memory_key text COLLATE "C" NOT NULL,
    memory_id uuid NOT NULL,
    version bigint NOT NULL,
    seq bigint NOT NULL,
    content_sha256 text NOT NULL,
    row jsonb NOT NULL,
    consolidated boolean GENERATED ALWAYS AS (row->'tags' ? 'consolidated') STORED,
    PRIMARY KEY (org, namespace, agent, user_id, session_id, memory_key)
) WITH (fillfactor = 90);
CREATE INDEX IF NOT EXISTS memory_current_by_id ON loom.memory_current (org, memory_id);
-- A user erasure's rows across the org's namespaces.
CREATE INDEX IF NOT EXISTS memory_current_by_user ON loom.memory_current (org, user_id);
-- A namespace's consolidated heads, which a user's erasure or a source's delete then
-- filters by their `src:` tags. Indexed by a stored flag, so a rewrite of `row` that
-- keeps it stays a heap-only update.
CREATE INDEX IF NOT EXISTS memory_current_consolidated
    ON loom.memory_current (org, namespace) WHERE consolidated;
-- A key read under any identity in a namespace.
CREATE INDEX IF NOT EXISTS memory_current_by_key
    ON loom.memory_current (org, namespace, memory_key, agent, user_id);

-- Every committed row, append-only (a redaction scrubs one in place), numbered per
-- namespace, with the hash-only audit row of its commit (built once, so a retried
-- delivery hashes the same).
CREATE TABLE IF NOT EXISTS loom.memory_revisions (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    seq bigint NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    memory_id uuid NOT NULL,
    version bigint NOT NULL,
    content_sha256 text NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    request_id text NOT NULL DEFAULT '',
    committed_at timestamptz NOT NULL DEFAULT now(),
    row jsonb NOT NULL,
    audit jsonb NOT NULL,
    PRIMARY KEY (org, namespace, seq)
);
CREATE INDEX IF NOT EXISTS memory_revisions_by_user ON loom.memory_revisions (org, user_id);
-- A user's profile versions, which a redaction of a memory they were folded from scrubs.
CREATE INDEX IF NOT EXISTS memory_revisions_profile
    ON loom.memory_revisions (org, namespace, user_id)
    WHERE row->>'agent' = '-' AND row->>'memory_type' = 'profile';
-- A memory's versions by id, in one namespace or across them (a restored namespace keeps
-- the ids).
CREATE INDEX IF NOT EXISTS memory_revisions_by_id
    ON loom.memory_revisions (org, memory_id, namespace, version);

-- What a revision still owes ClickHouse, apart from its payload so progress
-- updates never rewrite it. `targets` is a bit set (1 the memory row, 2 the search
-- document, 4 the audit row, 8 a deleted decision's record to retire, 16 the vectors of
-- the version a revision that is not embedded replaced); the row is deleted when it
-- reaches 0 under the commit's token, and one a later holder finished is kept settled
-- for the sweep.
-- `lease_token` fences a publisher: only the current holder's update lands.
CREATE TABLE IF NOT EXISTS loom.memory_deliveries (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    seq bigint NOT NULL,
    targets smallint NOT NULL,
    lease_token bigint NOT NULL DEFAULT 1,
    lease_until timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    -- A publish started and recorded no outcome: the next claim counts an attempt.
    held boolean NOT NULL DEFAULT false,
    -- The last moment a former holder may still send (a lease a redaction ended, or
    -- the delivery was parked); redaction, erasure and the sweep wait it out.
    fenced_until timestamptz,
    -- No target left: kept for the insert bound after a takeover (`pg_memory.complete`).
    settled boolean GENERATED ALWAYS AS (targets = 0) STORED,
    -- A parked delivery has no lease of its own: `fenced_until` is what a wait covers.
    CHECK (lease_until <> 'infinity' OR fenced_until IS NOT NULL),
    PRIMARY KEY (org, namespace, seq),
    FOREIGN KEY (org, namespace, seq) REFERENCES loom.memory_revisions ON DELETE CASCADE
) WITH (fillfactor = 90);
-- The delivery worker's queue across tenants, oldest lease first; a settled delivery waiting
-- for the sweep is not in it.
CREATE INDEX IF NOT EXISTS memory_deliveries_due ON loom.memory_deliveries (lease_until)
    WHERE NOT settled;
-- The settled deliveries the delivery pass sweeps. On a stored flag, so clearing a target
-- that leaves others owed stays a heap-only update.
CREATE INDEX IF NOT EXISTS memory_deliveries_settled ON loom.memory_deliveries (lease_until)
    WHERE settled;
-- A queue (a row per write, deleted once delivered) and a counter per scope updated
-- on every commit: vacuumed by a fixed count of dead rows, not a fraction of a small table.
ALTER TABLE loom.memory_deliveries
    SET (autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 1000);
ALTER TABLE loom.scope_generations
    SET (autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 1000);

-- An op's answer for an idempotency key, and the revisions its commit wrote.
-- state: running (a request holds `lease_until`), committed (its revisions
-- are in, its answer is not), done (`response_body` is the answer). A retry
-- that takes over an expired claim advances `lease_token`, so the request it
-- replaced can no longer commit under the key, and restarts `created_at`.
CREATE TABLE IF NOT EXISTS loom.idempotency (
    org text COLLATE "C" NOT NULL,
    key_hash text COLLATE "C" NOT NULL,
    subject text NOT NULL,
    operation text NOT NULL,
    request_digest text NOT NULL,
    state text NOT NULL,
    lease_token bigint NOT NULL DEFAULT 1,
    lease_until timestamptz,
    revisions jsonb NOT NULL DEFAULT '[]',
    status_code integer,
    response_body jsonb,
    response_headers jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (org, key_hash)
);
-- The retention sweep's order across tenants.
CREATE INDEX IF NOT EXISTS idempotency_expiry ON loom.idempotency (expires_at);
-- An erasure's fence reads the org's claims in flight, not its day of answers.
CREATE INDEX IF NOT EXISTS idempotency_running ON loom.idempotency (org) WHERE state = 'running';

-- When an erasure last removed an org's replay answers: an answer claimed before it
-- and stored is residue, one claimed since is a later request's. No content.
CREATE TABLE IF NOT EXISTS loom.idempotency_fences (
    org text COLLATE "C" PRIMARY KEY,
    fenced_at timestamptz NOT NULL
);

-- Deletion evidence: the ids an erasure removed (no content). For
-- LOOM_PG_ERASURE_EVIDENCE_HOURS after `erased_at` a commit of the id into the namespace
-- is refused and a restore into it leaves the id out; readers judge the age, the delivery
-- pass sweeps expired rows.
CREATE TABLE IF NOT EXISTS loom.erased_memories (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    memory_id uuid NOT NULL,
    erased_at timestamptz NOT NULL DEFAULT now(),
    -- When no publisher of the removed rows can still land one: what a run of the
    -- erasure that finds them gone already waits until.
    quiet_at timestamptz NOT NULL DEFAULT now(),
    -- By namespace: a namespace restored from another's snapshot keeps its memory ids.
    PRIMARY KEY (org, namespace, memory_id)
);
CREATE INDEX IF NOT EXISTS erased_memories_age ON loom.erased_memories (erased_at);
-- An erasure reads the latest `quiet_at` of its org (and its namespaces).
CREATE INDEX IF NOT EXISTS erased_memories_quiet ON loom.erased_memories (org, quiet_at);

-- The audit rows an erasure removed with their revisions before ClickHouse had them (the
-- audit trail outlives the memory: keys and ids, never values), and those of a delivery
-- claimed while the holder it replaced may still land them. The delivery pass publishes
-- each once `due_at` passes, when no earlier publisher can still land it; one refused
-- LOOM_PG_DELIVERY_MAX_ATTEMPTS times is parked (`due_at` infinity).
CREATE TABLE IF NOT EXISTS loom.audit_outbox (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    row_hash text NOT NULL,
    audit jsonb NOT NULL,
    due_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    PRIMARY KEY (org, namespace, row_hash)
);
CREATE INDEX IF NOT EXISTS audit_outbox_due ON loom.audit_outbox (due_at);
