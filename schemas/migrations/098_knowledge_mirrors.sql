-- loom.knowledge_mirrors: each namespace's mirror, the namespace named `acl-` and 24 hex
-- digits where its mirrored sources' documents live, each shown only to a reader the
-- store's permission list admits (docs/architecture/document-sources.md). Made under the
-- namespace's source lease with its first mirrored source. A namespace erasure erases
-- the mirror's rows, then writes this row `deleted`, so the name registered again gets a
-- new mirror. A registration read by key; no memory content.
CREATE TABLE IF NOT EXISTS loom.knowledge_mirrors
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `mirror` String,
    `deleted` Bool DEFAULT false,
    `created_at` DateTime DEFAULT now(),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace);

-- loom.knowledge_sources: who a source's documents are shown to (`access`: `namespace`,
-- every reader of the namespace, or `mirrored`, a reader the store admits) and whose
-- credential it reads with (`credential_mode`: `shared`, an admin's, for now the only
-- mode). A row written before these columns reads the defaults, today's behaviour.
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS access LowCardinality(String) DEFAULT 'namespace';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS credential_mode LowCardinality(String) DEFAULT 'shared';
