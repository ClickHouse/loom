-- Rules an assistant states about itself are filed in loom.learnings_queue as
-- `assistant_turn` candidates (server/memory/app/assistant_learnings.py). `agent` is
-- the agent the rule is for, and promotion writes the memory under it.
ALTER TABLE loom.learnings_queue
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '';

-- One row per (candidate, end user, session) that stated an `assistant_turn`
-- candidate's rule, written once and never by a review, so support never rewrites a
-- candidate's status row; learnings.list counts it at read time. The earliest row of a
-- candidate names the user who filed it, whose conversation its rationale quotes. A
-- user erasure deletes the user's rows and every candidate the user filed
-- (erasure.store.steps_ledgers).
CREATE TABLE IF NOT EXISTS loom.learnings_supporters
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `candidate_id` UUID,
    `user_id` String,
    `session_id` String,
    `confidence` Float32,
    `created_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (org, namespace, candidate_id, user_id, session_id)
SETTINGS non_replicated_deduplication_window = 1000;
