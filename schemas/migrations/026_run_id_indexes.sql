-- Skip indexes on run_id in agent_events and agent_runs.
--
-- An MCP session's implicit run is found by (org, run_id), in whichever
-- namespace the session began it, and closed when no request labelled with it
-- reached the ledger within the idle window. Both tables are sorted by
-- namespace before run_id, so those reads narrowed only by org; the indexes
-- let them skip every granule that holds none of the run's rows. A part written
-- before this migration is read in full until a merge rewrites it.
ALTER TABLE loom.agent_events
    ADD INDEX IF NOT EXISTS run_id_bloom_idx run_id TYPE bloom_filter(0.01) GRANULARITY 1;
ALTER TABLE loom.agent_runs
    ADD INDEX IF NOT EXISTS run_id_bloom_idx run_id TYPE bloom_filter(0.01) GRANULARITY 1;
