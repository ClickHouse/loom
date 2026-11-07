"""Every ReplacingMergeTree table declares how its writers keep its columns.

Under FINAL a ReplacingMergeTree serves the newest row of a key whole. A
re-insert that omits a column lands that column's DEFAULT over the value the
previous row held, silently. So each table in schemas/schema.sql is classified
here, and a new ReplacingMergeTree table fails this test until it is:

- `builder`: its writers go through a column-set constant or row builder, and
  the named test holds that set to the table's columns;
- `view`: only the named materialized view writes it, and
  test_insert_mv_targets / test_refresh_mv_targets pin the view's columns;
- `replace`: every rewrite is `INSERT ... SELECT * REPLACE (...)` in the named
  file, which carries every column it does not change;
- `insert_once`: no writer re-inserts a key (the value says why);
- `gap`: not yet classified; the value owns the writers, as in
  tests/inventory/gaps.toml. The gap list only shrinks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.schema_ddl import ddl, engine, tables, views

ROOT = Path(__file__).resolve().parents[2]
# The owners tests/inventory/gaps.toml uses.
OWNERS = {"code", "memory", "platform"}

WRITE_PATHS: dict[str, tuple[str, str]] = {
    "provider_configuration": ("builder", "test_provider_configuration.py"),
    "collections": ("builder", "test_sharing.py"),
    "principal_groups": ("builder", "test_sharing.py"),
    "sso_links": ("builder", "test_sso_links.py"),
    "scim_users": ("builder", "test_scim.py"),
    "delete_version_checks": ("builder", "test_delete_version_sql.py"),
    "memory_webhooks": ("builder", "test_memory_webhooks.py"),
    "knowledge_sources": ("builder", "test_knowledge_sources.py"),
    "machine_identities": ("builder", "test_machines.py"),
    "org_model_calls": ("builder", "test_model_calls_switch.py"),
    "extraction_policies": ("builder", "test_extraction_policy.py"),
    "profile_schemas": ("builder", "test_profile_schema.py"),
    "deployment_config": ("builder", "test_deployment_config.py"),
    "document_blobs": ("builder", "test_document_object_store.py"),
    "runtime_overrides": ("builder", "test_runtime_overrides.py"),
    "signing_keys": ("builder", "test_signing_keyring.py"),
    "carry_acks": ("builder", "test_rmt_carry.py"),
    "storage_authority": ("builder", "test_pg_authority.py"),
    "frontend_assets": ("builder", "test_shared_state_writers.py"),
    "embedder_calibration": ("builder", "test_shared_state_writers.py"),
    "usage_acted_credits": ("builder", "test_shared_state_writers.py"),
    "agent_runs": ("gap", "platform"),
    "agent_turn_usage": ("gap", "platform"),
    "blacklist": ("gap", "platform"),
    "blacklist_version": ("gap", "platform"),
    "categories": ("gap", "platform"),
    "chunk_supersessions": ("gap", "platform"),
    "code_answers": ("gap", "code"),
    "code_backport_gaps": ("builder", "test_repository_column_sets_match_schema.py"),
    "code_call_sites_unresolved": ("gap", "code"),
    "code_caller_guards": ("gap", "code"),
    "code_chunks": ("gap", "code"),
    "code_chunks_prefix": ("gap", "code"),
    "code_context_packs": ("gap", "code"),
    "code_coverage_files": ("builder", "test_repository_column_sets_match_schema.py"),
    "memory_code_links": ("builder", "test_repository_column_sets_match_schema.py"),
    "code_coverage_watches": ("builder", "test_repository_column_sets_match_schema.py"),
    "code_doc_stats": ("view", "code_doc_stats_mv"),
    "code_edges": ("gap", "code"),
    "code_files": ("gap", "code"),
    "code_gh_items": ("gap", "code"),
    "code_index_state": ("gap", "code"),
    "code_pr_history": ("gap", "code"),
    "code_pr_reviews": ("gap", "code"),
    "code_repos": ("gap", "code"),
    "code_review_briefs": ("gap", "code"),
    "code_scip_batches": ("gap", "code"),
    "code_search_memo": ("gap", "code"),
    "code_summaries": ("gap", "code"),
    "code_symbol_ids": ("view", "code_symbol_ids_mv"),
    "code_symbol_names": ("view", "code_symbol_names_mv"),
    "code_symbol_stats": ("gap", "code"),
    "code_symbols": ("gap", "code"),
    "code_test_facets": ("gap", "code"),
    "code_test_map": ("gap", "code"),
    "code_unit_risk": ("gap", "code"),
    "conflict_allowlist": ("gap", "memory"),
    "context_blobs": ("gap", "memory"),
    "conversation_embeddings": ("gap", "memory"),
    "conversation_threads": ("gap", "memory"),
    "documents": ("builder", "test_document_row_builder.py"),
    "knowledge_ingestions": ("builder", "test_knowledge_ingest.py"),
    "datasets": ("builder", "test_knowledge_datasets.py"),
    "embedding_cache": ("gap", "platform"),
    "entity_canonical_map": ("gap", "memory"),
    "entity_communities": ("gap", "memory"),
    "entity_edge_memories": ("gap", "memory"),
    "entity_edge_rows": (
        "insert_once",
        "every column is in the sort key: a re-insert is the same row",
    ),
    "entity_summaries": ("gap", "memory"),
    "entity_triple_memories": ("gap", "memory"),
    "entity_triple_rows": (
        "insert_once",
        "every column is in the sort key: a re-insert is the same row",
    ),
    "entity_vectors": ("gap", "memory"),
    "erasure_requests": ("gap", "platform"),
    "erasure_reversals": ("gap", "platform"),
    "idempotency": ("gap", "platform"),
    "learnings_queue": ("gap", "memory"),
    "llm_response_cache": ("gap", "platform"),
    "memory": ("builder", "test_rmt_write_is_typed.py"),
    "memory_ask_precedents": ("gap", "memory"),
    "memory_belief_sources": ("gap", "memory"),
    "memory_categories": ("gap", "memory"),
    "memory_chunk_sources": ("gap", "memory"),
    "memory_chunks": ("gap", "memory"),
    "memory_doc_stats": ("view", "memory_doc_stats_refresh"),
    "memory_embeddings": ("builder", "test_rmt_write_is_typed.py"),
    "memory_evidence": ("gap", "memory"),
    "memory_extraction_queue": ("gap", "memory"),
    "memory_record_conflicts": ("gap", "memory"),
    "memory_record_fields": ("gap", "memory"),
    "memory_record_versions": ("view", "memory_record_versions_mv"),
    "memory_records": ("gap", "memory"),
    "memory_reinforcement": ("builder", "test_reinforcement_side_table.py"),
    "memory_schema_fields": ("gap", "memory"),
    "memory_schemas": ("gap", "memory"),
    "memory_versions": ("view", "memory_versions_mv"),
    "namespace_calibration_result": ("gap", "memory"),
    "namespace_grants": ("gap", "platform"),
    "namespace_ontology": ("gap", "platform"),
    "namespace_policies": ("builder", "test_repository_column_sets_match_schema.py"),
    "namespace_types": ("gap", "platform"),
    "namespace_visits": ("gap", "platform"),
    "namespaces": ("gap", "platform"),
    "oauth_state": ("gap", "platform"),
    "operator_edges": ("gap", "memory"),
    "qa_fail_hours": ("builder", "test_repository_column_sets_match_schema.py"),
    "qa_item_embeddings": ("builder", "test_repository_column_sets_match_schema.py"),
    "qa_link_candidates": ("builder", "test_repository_column_sets_match_schema.py"),
    "qa_rollup_days": ("builder", "test_repository_column_sets_match_schema.py"),
    "qa_sources": ("gap", "platform"),
    "qa_test_daily": ("builder", "test_repository_column_sets_match_schema.py"),
    "revoked_tokens": ("gap", "platform"),
    "service_accounts": ("gap", "platform"),
    "session_briefs": ("builder", "test_session_brief.py"),
    "session_outcomes_by_user": ("gap", "memory"),
    "session_summaries": ("gap", "memory"),
    "shared": ("builder", "test_shared_and_documents_list_repo.py"),
    "task_inboxes": ("builder", "test_repository_column_sets_match_schema.py"),
    "task_poster_policies": ("builder", "test_repository_column_sets_match_schema.py"),
    "task_webhook_cursor": ("builder", "test_repository_column_sets_match_schema.py"),
    "tasks": ("builder", "test_repository_column_sets_match_schema.py"),
    "user_state": ("gap", "platform"),
    "worker_status": ("gap", "platform"),
    "working_memory": ("gap", "memory"),
}


def _rmt_tables() -> set[str]:
    return {name for name in tables() if engine(name) == "ReplacingMergeTree"}


def test_every_replacing_table_is_classified() -> None:
    rmt = _rmt_tables()
    assert not rmt - set(WRITE_PATHS), f"classify in WRITE_PATHS: {sorted(rmt - set(WRITE_PATHS))}"
    assert not set(WRITE_PATHS) - rmt, f"not a ReplacingMergeTree: {sorted(set(WRITE_PATHS) - rmt)}"


def _named(name: str, text: str) -> bool:
    return re.search(rf"\bloom\.{name}\b|[\"']{name}[\"']", text) is not None


@pytest.mark.parametrize(("name", "path"), sorted(WRITE_PATHS.items()))
def test_each_classification_carries_its_evidence(name: str, path: tuple[str, str]) -> None:
    kind, value = path
    if kind == "builder":
        test = ROOT / "tests" / "unit" / value
        assert test.is_file() and _named(name, test.read_text()), (name, value)
    elif kind == "view":
        assert value in views(), (name, value)
        assert re.search(rf"\bTO loom\.{name}\n", ddl(value)), (name, value)
    elif kind == "replace":
        src = (ROOT / value).read_text()
        assert "SELECT * REPLACE" in src and _named(name, src), (name, value)
    elif kind == "insert_once":
        assert len(value) >= 20, (name, "say why no writer re-inserts a key")
    else:
        assert kind == "gap" and value in OWNERS, (name, path)


def test_the_gap_list_only_shrinks() -> None:
    """A new table is classified, not parked: lower the bound as gaps close."""
    assert sum(kind == "gap" for kind, _v in WRITE_PATHS.values()) <= 76
