-- Crawl admission stays closed until every process validates its authority and exclusions.
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS options_json String DEFAULT '{}';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS registrant_subject String DEFAULT '';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS approval_enc String DEFAULT '';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS crawl_generation String DEFAULT '';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS crawl_customer_id String DEFAULT '';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS crawl_credential_hash String DEFAULT '';
-- A deletion records verified canonical identity before discarding its contact ties.
ALTER TABLE loom.scim_users ADD COLUMN IF NOT EXISTS crawl_erasure_enc String DEFAULT '';
ALTER TABLE loom.scim_users ADD COLUMN IF NOT EXISTS crawl_erasure_done Bool DEFAULT false;
ALTER TABLE loom.scim_users ADD COLUMN IF NOT EXISTS crawl_erasure_required Bool DEFAULT false;
-- A pre-erasure full listing never proves a post-erasure crawl union.
ALTER TABLE loom.knowledge_source_listings ADD COLUMN IF NOT EXISTS crawl_exclusion_generation String DEFAULT '';

-- Only a complete listing of this exact registration generation admits its copies.
ALTER TABLE loom.knowledge_source_listings ADD COLUMN IF NOT EXISTS crawl_generation String DEFAULT '';

-- One stable, sealed refusal key per org survives rotation of the deployment root.
CREATE TABLE IF NOT EXISTS loom.knowledge_crawl_exclusion_keys
(
    org String,
    key_id String,
    key_enc String,
    generation String,
    pending_id String DEFAULT '',
    pending_digests Array(String) DEFAULT [],
    pending_exclude Bool DEFAULT false,
    version UInt64 DEFAULT toUnixTimestamp64Nano(now64(9))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY org
SETTINGS non_replicated_deduplication_window = 1000;

-- These explicit refusal tombstones contain no raw provider id or address.
CREATE TABLE IF NOT EXISTS loom.knowledge_crawl_exclusions
(
    org String,
    digest String,
    excluded Bool,
    version UInt64 DEFAULT toUnixTimestamp64Nano(now64(9))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, digest)
SETTINGS non_replicated_deduplication_window = 1000;
