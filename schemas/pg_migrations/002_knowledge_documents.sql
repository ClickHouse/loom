-- The Postgres authority for knowledge documents: the head of every source, the
-- loom.documents rows each commit still owes ClickHouse, and the deletion evidence an
-- erasure keeps.
-- docs/architecture/storage-authority.md describes each table.

-- The live head of every source: the complete loom.documents row a read serves. A
-- commit replaces it only over the version its writer read, so two writes of one
-- source never number the same revision.
CREATE TABLE IF NOT EXISTS loom.knowledge_heads (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    source_uri text COLLATE "C" NOT NULL,
    version bigint NOT NULL,
    revision integer NOT NULL,
    deleted boolean NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    row jsonb NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org, namespace, source_uri)
) WITH (fillfactor = 90);
-- A user erasure's heads across the org's namespaces.
CREATE INDEX IF NOT EXISTS knowledge_heads_by_user ON loom.knowledge_heads (org, user_id);

-- What a commit still owes ClickHouse. `targets` is a bit set (1 the loom.documents
-- row, always set; 2 the retirement of the source's older chunk versions). A delivery
-- is published whole, never bit by bit: the row is deleted, under its holder's token,
-- once it is published, or kept `settled` when that holder took it over. It holds its
-- own copy of the row, so a later head does not change what an earlier commit
-- publishes.
-- `lease_token` fences a publisher: only the current holder's update lands.
CREATE TABLE IF NOT EXISTS loom.knowledge_deliveries (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    source_uri text COLLATE "C" NOT NULL,
    version bigint NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    row jsonb NOT NULL,
    targets smallint NOT NULL,
    lease_token bigint NOT NULL DEFAULT 1,
    lease_until timestamptz NOT NULL,
    -- A parked delivery has no lease of its own: the last moment the send that parked
    -- it started, which an erasure's wait still covers.
    fenced_until timestamptz,
    -- Published by a holder that took it over: kept, its lease ending then, until the
    -- request it replaced can land nothing more, which an erasure waits out; then swept.
    settled boolean NOT NULL DEFAULT false,
    attempts integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    PRIMARY KEY (org, namespace, source_uri, version),
    CHECK (lease_until <> 'infinity' OR fenced_until IS NOT NULL)
) WITH (fillfactor = 90);
-- The delivery worker's queue across tenants, oldest lease first; a settled delivery waiting
-- for the sweep is not in it.
CREATE INDEX IF NOT EXISTS knowledge_deliveries_due ON loom.knowledge_deliveries (lease_until)
    WHERE NOT settled;
-- The settled deliveries the sweep deletes.
CREATE INDEX IF NOT EXISTS knowledge_deliveries_settled ON loom.knowledge_deliveries (lease_until)
    WHERE settled;
-- A user erasure's deliveries across the org's namespaces.
CREATE INDEX IF NOT EXISTS knowledge_deliveries_by_user
    ON loom.knowledge_deliveries (org, user_id);
-- A queue: vacuumed by a fixed count of dead rows, not a fraction of a small table.
ALTER TABLE loom.knowledge_deliveries
    SET (autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 1000);

-- Deletion evidence: the documents an erasure removed, by the `doc_id` a row built from a
-- read of one keeps (no content): every version's, in Postgres and in ClickHouse. For
-- LOOM_PG_ERASURE_EVIDENCE_HOURS after `erased_at` a commit of the id into the namespace
-- is refused; the delivery pass sweeps expired rows.
CREATE TABLE IF NOT EXISTS loom.erased_documents (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    doc_id text COLLATE "C" NOT NULL,
    erased_at timestamptz NOT NULL DEFAULT now(),
    -- When no publisher of the removed deliveries can still land a row: what a run of the
    -- erasure that finds them gone already waits until.
    quiet_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org, namespace, doc_id)
);
CREATE INDEX IF NOT EXISTS erased_documents_age ON loom.erased_documents (erased_at);
-- An erasure reads the latest `quiet_at` of its org (and its namespaces).
CREATE INDEX IF NOT EXISTS erased_documents_quiet ON loom.erased_documents (org, quiet_at);
