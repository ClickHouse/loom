-- An optional budget on an agent run: what it may spend and how long it may run.
--
-- run.begin stores them; run.get compares them with what the run has spent
-- (loom.agent_turn_usage, by session) and how long it has been open, and names
-- the budgets it has gone past. Loom does not run the agent, so the check is a
-- signal for whoever does, never a stop. 0 means no budget.
ALTER TABLE loom.agent_runs
    ADD COLUMN IF NOT EXISTS budget_usd Float64 DEFAULT 0 AFTER graded_at,
    ADD COLUMN IF NOT EXISTS budget_seconds UInt32 DEFAULT 0 AFTER budget_usd;
