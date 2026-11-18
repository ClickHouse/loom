-- Namespace policy for separate named-user caps and task-inbox budget warnings.
ALTER TABLE loom.namespaces ADD COLUMN IF NOT EXISTS daily_user_cost_cap_usd Float64 DEFAULT 0;
ALTER TABLE loom.namespaces ADD COLUMN IF NOT EXISTS budget_alert_inbox String DEFAULT '';

-- Budget notifications carry the end user explicitly for per-user erasure.
ALTER TABLE loom.task_events ADD COLUMN IF NOT EXISTS user_id String DEFAULT '-';
ALTER TABLE loom.task_events ADD COLUMN IF NOT EXISTS session_id String DEFAULT '-';
ALTER TABLE loom.task_events ADD COLUMN IF NOT EXISTS source_class LowCardinality(String) DEFAULT '';
