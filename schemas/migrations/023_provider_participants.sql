-- Internal process census. No content or keys; no expiry may imply drain.
CREATE TABLE IF NOT EXISTS loom._provider_participants
(
    `participant_id` UUID,
    `transition_id` UUID DEFAULT '00000000-0000-0000-0000-000000000000',
    `state` LowCardinality(String),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY participant_id;
