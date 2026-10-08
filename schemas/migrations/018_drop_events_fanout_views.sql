-- loom.events carries budget violations only. The materialized views that fanned
-- other telemetry kinds from it into their own tables are gone: every writer
-- inserts into its table directly.
DROP VIEW IF EXISTS loom.events_audit_log_mv;

DROP VIEW IF EXISTS loom.events_code_search_outcome_mv;

DROP VIEW IF EXISTS loom.events_memory_exposures_mv;

DROP VIEW IF EXISTS loom.events_query_nodes_mv;

DROP VIEW IF EXISTS loom.events_search_trace_log_mv;
