-- A namespace's guide (server/identity/app/namespace_guide.py,
-- `namespace.guide_set`): the text that tells an agent or a person what the
-- namespace holds and how to use it — its key layout, what each tag means,
-- which ops to call.
--
-- One row per revision, numbered from 1; the highest revision is the guide. A
-- writer names the revision it read, so of two edits made from one revision the
-- second is refused, and its writer merges it into the first and sends it
-- again. content = '' is a cleared guide. updated_by is the actor that wrote
-- the revision. A build that predates this table never reads it.
CREATE TABLE IF NOT EXISTS loom.namespace_guides
(
    `org` LowCardinality(String),
    `namespace` String,
    `revision` UInt32,
    `content` String,
    `summary` String,
    `note` String,
    `updated_by` String,
    `updated_at` DateTime64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY (org, namespace, revision)
SETTINGS non_replicated_deduplication_window = 1000;
