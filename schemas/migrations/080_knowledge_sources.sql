-- The document sources a repository's maintainers registered (knowledge.source.add):
-- one row per (org, trunk code namespace, name), its kind, the ids it reads
-- (`config_json`, never a host or a URL) and its credential sealed under
-- `knowledge_sources.credential`. Add, rotate and remove each write a full version
-- row; a removed source is `active = false`, its credential cleared, and
-- `pending_delete` when its maintainer asked for its documents to be retired.
-- `source_id` names one registration: add writes it once and every later write of
-- the row carries it, so a source removed and added again under the name is told
-- apart. A build that does not know the table never reads it.
CREATE TABLE IF NOT EXISTS loom.knowledge_sources
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `name` LowCardinality(String),
    `source_id` UUID,
    `kind` LowCardinality(String),
    `config_json` String DEFAULT '{}',
    `credential_enc` String DEFAULT '',
    `created_by` String DEFAULT '',
    `active` Bool DEFAULT true,
    `pending_delete` Bool DEFAULT false,
    `created_at` DateTime DEFAULT now(),
    `updated_at` DateTime DEFAULT now(),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, name)
SETTINGS non_replicated_deduplication_window = 1000;
