-- Rules an assistant states about itself are filed in loom.learnings_queue as
-- `assistant_turn` candidates (server/memory/app/assistant_learnings.py). `agent` is
-- the agent the rule is for, and promotion writes the memory under it.
-- `filed_by_user` is the end user whose conversation the candidate's rationale
-- quotes: a user erasure deletes the candidates that user filed.
ALTER TABLE loom.learnings_queue
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `filed_by_user` String DEFAULT '';

-- One row per (candidate, end user, session) that stated an `assistant_turn`
-- candidate's rule. Written once per session and never by a review, so support never
-- rewrites a candidate's status row; learnings.list counts it at read time. A user
-- erasure deletes the user's rows (erasure.store.steps_ledgers).
CREATE TABLE IF NOT EXISTS loom.learnings_supporters
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `candidate_id` UUID,
    `user_id` String,
    `session_id` String,
    `confidence` Float32,
    `created_at` DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (org, namespace, candidate_id, user_id, session_id)
SETTINGS non_replicated_deduplication_window = 1000;
