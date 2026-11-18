"""`cell_scan.LOG_TABLES` against the schema: every table with an `org` column is a log
table or one of `_DATA_TABLES` below, so a new one is judged the day it lands, and a table a
view fills from a log table is a log table too. CH-free."""

from __future__ import annotations

import re

from server.cli.cell_scan import LOG_TABLES
from tests.log_tables import LOGGED, closure
from tests.unit import schema_ddl

# The tables whose orgs attach places: every row's org was checked by the request or the
# worker that wrote it. A new table with an `org` column joins this set or LOG_TABLES.
_DATA_TABLES = frozenset(
    {
        "loom.acl_versions",
        "loom.agent_run_claims",
        "loom.agent_runs",
        "loom.agent_turn_usage",
        "loom.analysis_canary",
        "loom.audit_log",
        "loom.blacklist",
        "loom.blob_uploads",
        "loom.categories",
        "loom.chunk_supersessions",
        "loom.chunk_term_freq",
        "loom.chunks",
        "loom.code_answers",
        "loom.code_backport_gaps",
        "loom.code_call_sites_unresolved",
        "loom.code_caller_guards",
        "loom.code_chunks",
        "loom.code_chunks_prefix",
        "loom.code_context_packs",
        "loom.code_coverage_files",
        "loom.code_coverage_watches",
        "loom.code_doc_stats",
        "loom.code_edges",
        "loom.code_files",
        "loom.code_gh_items",
        "loom.code_index_state",
        "loom.code_pr_history",
        "loom.code_pr_reviews",
        "loom.code_repos",
        "loom.code_review_briefs",
        "loom.code_scip_batches",
        "loom.code_scip_parts",
        "loom.code_search_memo",
        "loom.code_search_outcome",
        "loom.code_summaries",
        "loom.code_symbol_ids",
        "loom.code_symbol_names",
        "loom.code_symbol_stats",
        "loom.code_symbols",
        "loom.code_term_df",
        "loom.code_term_freq",
        "loom.code_test_facets",
        "loom.code_test_map",
        "loom.code_unit_risk",
        "loom.code_unit_signal",
        "loom.collections",
        "loom.conflict_allowlist",
        "loom.consolidation_runs",
        "loom.context_blobs",
        "loom.conversation_embeddings",
        "loom.conversation_threads",
        "loom.conversations",
        "loom.dataset_rows",
        "loom.datasets",
        "loom.delete_version_checks",
        "loom.derived_facts",
        "loom.document_blobs",
        "loom.documents",
        "loom.embedding_cache",
        "loom.entity_canonical_map",
        "loom.entity_communities",
        "loom.entity_degree",
        "loom.entity_edge_memories",
        "loom.entity_edge_rows",
        "loom.entity_edges",
        "loom.entity_summaries",
        "loom.entity_triple_memories",
        "loom.entity_triple_rows",
        "loom.entity_triples",
        "loom.entity_vectors",
        "loom.erasure_requests",
        "loom.erasure_reversals",
        "loom.events",
        "loom.extraction_policies",
        "loom.graph_projections",
        "loom.idempotency",
        "loom.knowledge_gaps",
        "loom.knowledge_group_members",
        "loom.knowledge_ingest_cancels",
        "loom.knowledge_ingestions",
        "loom.knowledge_mirrors",
        "loom.knowledge_source_listings",
        "loom.knowledge_sources",
        "loom.knowledge_uploads",
        "loom.learnings_queue",
        "loom.learnings_supporters",
        "loom.llm_response_cache",
        "loom.machine_identities",
        "loom.memory",
        "loom.memory_ask_precedents",
        "loom.memory_belief_sources",
        "loom.memory_categories",
        "loom.memory_chunk_sources",
        "loom.memory_chunks",
        "loom.memory_code_link_queue",
        "loom.memory_code_links",
        "loom.memory_doc_stats",
        "loom.memory_drops",
        "loom.memory_embeddings",
        "loom.memory_evidence",
        "loom.memory_exposure_breadth_hourly",
        "loom.memory_exposures",
        "loom.memory_extraction_queue",
        "loom.memory_projection_failures",
        "loom.memory_record_conflicts",
        "loom.memory_record_fields",
        "loom.memory_record_versions",
        "loom.memory_records",
        "loom.memory_reinforcement",
        "loom.memory_schema_fields",
        "loom.memory_schemas",
        "loom.memory_served_daily",
        "loom.memory_term_freq",
        "loom.memory_usage_events",
        "loom.memory_usage_totals",
        "loom.memory_versions",
        "loom.memory_webhook_outbox",
        "loom.memory_webhooks",
        "loom.namespace_calibration_result",
        "loom.namespace_grants",
        "loom.namespace_guides",
        "loom.namespace_ontology",
        "loom.namespace_policies",
        "loom.namespace_snapshots",
        "loom.namespace_types",
        "loom.namespace_visits",
        "loom.namespace_write_marks",
        "loom.namespaces",
        "loom.oauth_state",
        "loom.operator_edges",
        "loom.org_model_calls",
        "loom.policy_decisions",
        "loom.principal_groups",
        "loom.procedure_runs",
        "loom.profile_schemas",
        "loom.qa_fail_hours",
        "loom.qa_item_embeddings",
        "loom.qa_link_candidates",
        "loom.qa_rollup_days",
        "loom.qa_sources",
        "loom.qa_test_daily",
        "loom.query_nodes",
        "loom.query_history_sources",
        "loom.search_misses",
        "loom.search_trace_log",
        "loom.service_accounts",
        "loom.session_brief_revisions",
        "loom.session_briefs",
        "loom.session_outcomes_by_user",
        "loom.session_summaries",
        "loom.shared",
        "loom.sql_access",
        "loom.sso_links",
        "loom.task_events",
        "loom.task_inboxes",
        "loom.task_leases",
        "loom.task_poster_policies",
        "loom.task_webhook_cursor",
        "loom.tasks",
        "loom.user_state",
        "loom.working_memory",
    }
)

_VIEW = re.compile(r"^CREATE MATERIALIZED VIEW IF NOT EXISTS (\S+)(.*?);\n", re.M | re.S)
_TO = re.compile(r"\bTO (loom\w*\.\w+)")
_SOURCE = re.compile(r"\b(?:FROM|JOIN) (loom\w*\.\w+)")


def _org_tables() -> set[str]:
    names = {f"loom.{t}" for t in schema_ddl.tables()}
    names |= set(re.findall(r"^CREATE TABLE IF NOT EXISTS (loom_telemetry\.\w+)", _text(), re.M))
    return {n for n in names if "org" in schema_ddl.columns(n)}


def _text() -> str:
    return schema_ddl.schema_text()


def test_every_table_with_an_org_column_is_judged() -> None:
    tables = _org_tables()
    assert len(tables) >= 150, len(tables)
    assert not LOG_TABLES & _DATA_TABLES, LOG_TABLES & _DATA_TABLES
    unjudged = sorted(tables - LOG_TABLES - _DATA_TABLES)
    assert not unjudged, (
        "tables with an org column in neither cell_scan.LOG_TABLES nor _DATA_TABLES here: "
        f"{unjudged}"
    )
    assert not (LOG_TABLES | _DATA_TABLES) - tables, (LOG_TABLES | _DATA_TABLES) - tables


def test_every_table_views_fill_from_a_log_table_is_a_log_table() -> None:
    views = []
    for m in _VIEW.finditer(_text()):
        to = _TO.search(m.group(2))
        assert to, m.group(1)
        views.append((set(_SOURCE.findall(m.group(2))), to.group(1)))
    assert closure(LOGGED, views) == LOG_TABLES
