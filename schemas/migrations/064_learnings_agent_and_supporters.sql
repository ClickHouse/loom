-- Rules an assistant states about itself are filed in loom.learnings_queue as
-- `assistant_turn` candidates (server/memory/app/assistant_learnings.py). `agent` is
-- the agent the rule is for: a promotion tags the knowledge document `agent:<agent>`,
-- or, with destination="memory", writes the memory row under it.
-- Existing candidates read agent = ''. A build without this change ignores the column
-- and the table, and its user erasure does not delete loom.learnings_supporters, so
-- memory_extract_agent_learnings (off by default) is turned on only once no replica
-- runs such a build.
ALTER TABLE loom.learnings_queue
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '';

-- One row per (candidate, end user, session) that stated an `assistant_turn`
-- candidate's rule, with the agent it was said to (the candidate's `agent`, so a row
-- read on its own names its bot), the principal that wrote the row (`actor`, so a
-- reviewer can tell many user_ids sent by one token from many writers) and that user's
-- own bounded quotes: `asked` (the user turn before the rule) and `said` (the cited
-- span, else the assistant turn, else the rule itself). A user's words live only on
-- that user's rows: a user erasure deletes them (erasure.store.steps_ledgers), and a
-- candidate left with no supporters is deleted, or keeps only its decision.
-- learnings.list counts the rows at read time with uniqExact.
CREATE TABLE IF NOT EXISTS loom.learnings_supporters
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `agent` LowCardinality(String),
    `candidate_id` UUID,
    `user_id` String,
    `session_id` String,
    `actor` LowCardinality(String) DEFAULT '',
    `confidence` Float32,
    `asked` String,
    `said` String,
    `created_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (org, namespace, candidate_id, user_id, session_id)
SETTINGS non_replicated_deduplication_window = 1000;
