-- The Postgres authority for the knowledge dataset catalog: for each dataset of a source
-- at a document version, the snapshot that serves it and its schema and approval.
-- loom.datasets in ClickHouse holds a best-effort projection of the rows committed here.
-- docs/architecture/storage-authority.md describes the table.

-- One row per (source, document version, dataset). `row` is the full catalog row (every
-- column of loom.datasets); `snapshot_id` names the loom.dataset_rows it serves, and
-- `version` orders the row's revisions as ReplacingMergeTree(version) does.
CREATE TABLE IF NOT EXISTS loom.knowledge_datasets (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    source_uri text COLLATE "C" NOT NULL,
    doc_version bigint NOT NULL,
    dataset_id text COLLATE "C" NOT NULL,
    snapshot_id bigint NOT NULL,
    version bigint NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    row jsonb NOT NULL,
    PRIMARY KEY (org, namespace, source_uri, doc_version, dataset_id)
);
-- A user erasure's datasets across the org's namespaces.
CREATE INDEX IF NOT EXISTS knowledge_datasets_by_user ON loom.knowledge_datasets (org, user_id);
