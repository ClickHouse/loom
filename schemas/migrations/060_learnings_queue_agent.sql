-- Rules an assistant states about itself are filed in loom.learnings_queue as
-- `assistant_turn` candidates (server/memory/app/assistant_learnings.py). `agent` is
-- the agent the rule is for, and promotion writes the memory under it.
ALTER TABLE loom.learnings_queue
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '';

-- One row per (candidate, end user, session) that stated an `assistant_turn`
-- candidate's rule, with that user's own bounded quotes: `asked` (the user turn before
-- the rule) and `said` (the rule as the assistant said it). A user's words live only on
-- that user's rows: a user erasure deletes them (erasure.store.steps_ledgers), and a
-- candidate left with no supporters is deleted, or keeps only its decision.
-- learnings.list counts the rows at read time with uniqExact.
CREATE TABLE IF NOT EXISTS loom.learnings_supporters
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `candidate_id` UUID,
    `user_id` String,
    `session_id` String,
    `confidence` Float32,
    `asked` String,
    `said` String,
    `created_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (org, namespace, candidate_id, user_id, session_id)
SETTINGS non_replicated_deduplication_window = 1000;
