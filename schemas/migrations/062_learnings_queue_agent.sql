-- Rules an assistant states about itself are filed in loom.learnings_queue as
-- `assistant_turn` candidates (server/memory/app/assistant_learnings.py). `agent` is
-- the agent the rule is for, and promotion writes the memory under it.
-- `supporter_users` are the end users whose conversations back the candidate: a user
-- erasure deletes every candidate that names the user (erasure.store.steps_ledgers).
ALTER TABLE loom.learnings_queue
    ADD COLUMN IF NOT EXISTS `agent` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `supporter_users` Array(String) DEFAULT [];
