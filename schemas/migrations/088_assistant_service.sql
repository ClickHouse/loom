-- loom.assistant_service: the one row of the assistant's LibreChat service user, which
-- owns the agent definition and whose agents API key runs it. It holds the user's email,
-- the agent's id, the version and model of the definition last written, and one sealed
-- value (server/core/secrets_at_rest.py) with the user's random password and its API key
-- together, so `loom secrets reseal` rewrites both at once. A boot fills it step by step
-- under the cluster lease assistant.service, each step at the version it read plus one.
-- The key reads nothing in Loom: each answer's tools read under that answer's own token.
-- No content.
CREATE TABLE IF NOT EXISTS loom.assistant_service
(
    `id` UInt8 DEFAULT 0,
    `email` String,
    `agent_id` String DEFAULT '',
    `definition_version` UInt32 DEFAULT 0,
    `definition_model` String DEFAULT '',
    `sealed` String,
    `version` UInt64,
    `updated_at` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(version)
ORDER BY id
SETTINGS non_replicated_deduplication_window = 1000;
