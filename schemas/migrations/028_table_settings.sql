-- Table settings that used to come from the server-wide
-- merge_tree_settings.xml, set on the tables themselves so Loom behaves the
-- same on any ClickHouse it is pointed at, without server config.
--
-- non_replicated_deduplication_window: every Loom INSERT carries an
-- insert_deduplication_token, so a retried batch lands once. A plain
-- MergeTree keeps no tokens unless this window is set; replicated and
-- Shared engines keep their own and ignore it.
--
-- enable_block_number_column / enable_block_offset_column: lightweight UPDATE
-- (memory.reject and the usage fold) needs both. Without them Loom falls back
-- to an ALTER UPDATE mutation per call.
--
-- A table a later migration creates carries the window in its own SETTINGS
-- (tests/unit/test_table_settings.py).

ALTER TABLE loom.memory MODIFY SETTING enable_block_number_column = 1, enable_block_offset_column = 1;
ALTER TABLE loom.memory_embeddings MODIFY SETTING enable_block_number_column = 1, enable_block_offset_column = 1;

ALTER TABLE loom.agent_cost_rollup_daily MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.agent_events MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.agent_runs MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.agent_turn_usage MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.audit_checkpoints MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.audit_log MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.blacklist MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.blacklist_version MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.categories MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.chunk_supersessions MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.chunk_term_freq MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.chunks MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_answers MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_backport_gaps MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_call_sites_unresolved MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_caller_guards MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_chunks MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_chunks_prefix MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_context_packs MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_coverage_files MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_coverage_watches MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_doc_stats MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_edges MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_files MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_gh_items MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_index_state MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_pr_history MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_pr_reviews MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_repos MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_review_briefs MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_scip_batches MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_scip_parts MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_search_memo MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_search_outcome MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_search_trace MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_summaries MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_symbol_ids MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_symbol_names MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_symbol_stats MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_symbols MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_term_df MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_term_freq MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_test_facets MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_test_map MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_unit_risk MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.code_unit_signal MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.conflict_allowlist MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.consolidation_runs MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.context_blobs MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.conversation_embeddings MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.conversation_threads MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.conversations MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.deployment_config MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.derived_facts MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.documents MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.embedding_cache MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_canonical_map MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_communities MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_degree MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_edge_memories MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_edges MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_summaries MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_triple_memories MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_triples MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.entity_vectors MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.erasure_requests MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.erasure_reversals MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.events MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.idempotency MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.learnings_queue MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.llm_response_cache MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_ask_precedents MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_belief_sources MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_categories MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_chunk_sources MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_chunks MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_code_link_queue MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_code_links MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_doc_stats MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_drops MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_embeddings MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_evidence MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_exposure_breadth_hourly MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_exposures MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_extraction_queue MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_projection_failures MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_record_conflicts MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_record_fields MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_record_versions MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_records MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_reinforcement MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_schema_fields MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_schemas MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_term_freq MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_usage_events MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_usage_totals MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.memory_versions MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_calibration_result MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_grants MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_ontology MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_policies MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_snapshots MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_types MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_visits MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespace_write_marks MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.namespaces MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.oauth_state MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.operator_edges MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.policy_decisions MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.procedure_runs MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.provider_configuration MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.qa_fail_hours MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.qa_item_embeddings MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.qa_link_candidates MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.qa_rollup_days MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.qa_sources MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.qa_test_daily MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.query_nodes MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.revoked_tokens MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.search_trace_log MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.service_accounts MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.session_brief_revisions MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.session_briefs MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.session_outcomes_by_user MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.session_summaries MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.shared MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.subject_last_seen MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.task_claims MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.task_events MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.task_inboxes MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.task_poster_policies MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.task_webhook_cursor MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.tasks MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.user_state MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.worker_status MODIFY SETTING non_replicated_deduplication_window = 1000;
ALTER TABLE loom.working_memory MODIFY SETTING non_replicated_deduplication_window = 1000;
