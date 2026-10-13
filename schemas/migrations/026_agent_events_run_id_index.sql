-- A skip index on agent_events.run_id.
--
-- An MCP session's implicit run is closed when no request labelled with it
-- reached the ledger within the idle window, in any namespace of the org, and
-- its call count is read at the close. The sort key is (org, namespace, agent,
-- ts), so that read narrowed only by org and month; the index lets it skip
-- every granule that holds none of the run's rows. Parts written before this
-- migration take it as they merge.
ALTER TABLE loom.agent_events
    ADD INDEX IF NOT EXISTS run_id_bloom_idx run_id TYPE bloom_filter(0.01) GRANULARITY 1;
