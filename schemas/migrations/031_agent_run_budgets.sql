-- An optional budget on an agent run: what it may spend and how long it may run.
--
-- run.begin stores them. run.get compares them with what the run has spent (its
-- session's turns in loom.agent_turn_usage while it was open) and how long it
-- has been open, and names the budgets it has gone past. Loom does not run the
-- agent, so the check is a signal for whoever does, never a stop. 0 means no
-- budget. budget_usd is a Float64 threshold, only compared with the spend and
-- never summed, so it does not need the Decimal of agent_turn_usage.cost_usd.
ALTER TABLE loom.agent_runs
    ADD COLUMN IF NOT EXISTS budget_usd Float64 DEFAULT 0 AFTER graded_at,
    ADD COLUMN IF NOT EXISTS budget_seconds UInt32 DEFAULT 0 AFTER budget_usd;
