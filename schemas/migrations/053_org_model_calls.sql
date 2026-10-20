-- Whether an org's data may go to a model (server/llm/customer.py,
-- `org.set_model_calls`).
--
-- enabled = 0 switches the org off: no chat, embedding, rerank or classify
-- call is made for it, on a request or by a background job, and its queued
-- work waits. No row means enabled. reason is the admin's note;
-- updated_by the subject that set it. locked = 1 is a platform admin's lock:
-- while it holds, the org's own admins cannot change the setting; locked_by
-- is the platform admin who set it.
CREATE TABLE IF NOT EXISTS loom.org_model_calls
(
    `org` String,
    `enabled` UInt8,
    `reason` String,
    `updated_by` String,
    `updated_at` DateTime64(3, 'UTC'),
    `locked` UInt8,
    `locked_by` String,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY org
SETTINGS non_replicated_deduplication_window = 1000;
