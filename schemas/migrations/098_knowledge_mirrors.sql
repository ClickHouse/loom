-- loom.knowledge_mirrors: the mirrors of each namespace, a namespace named `acl-` and 24
-- hex digits where its mirrored sources' documents live, each shown only to a reader the
-- store's permission list admits (docs/architecture/document-sources.md). A namespace
-- erasure purges every one and writes each `deleted`, so the name registered again gets
-- a new mirror. A forgotten mirror's row stays, written `deleted`, so a later erasure or
-- a replay still reaches it. Names only, no memory content.
CREATE TABLE IF NOT EXISTS loom.knowledge_mirrors
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `mirror` String,
    `deleted` Bool DEFAULT false,
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, mirror)
SETTINGS non_replicated_deduplication_window = 1000;

-- loom.knowledge_sources: who a source's documents are shown to (`access`: `namespace`,
-- every reader of the namespace, or `mirrored`, a reader the store admits). A row
-- written before the column reads `namespace`, today's behaviour.
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS access LowCardinality(String) DEFAULT 'namespace';
