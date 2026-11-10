"""The migration journal and the schema it produces.

CH-free: these parse the migration files and the canonical dump. `make
schema-check` pins the dump byte for byte against a replay; these pin the
storage contracts a later edit must not break.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.schema_ddl import ddl, objects, schema_text, tables, ttl, views

MIGRATIONS = Path(__file__).resolve().parents[2] / "schemas" / "migrations"
BASELINE = MIGRATIONS / "016_baseline.sql"

# Census tables (they hold memory content in an erasure scope) that expire by
# TTL, each with why expiry is not deleting what a writer asked to keep. Every
# other census table carries no TTL: Loom deletes memory only when its writer
# asks.
TRANSIENT_TABLES = {
    "events": "the raw event stream; the per-event views copy what is kept",
    "search_trace_log": "a per-search trace for tuning, not a record anyone wrote",
    "memory_exposures": "an access log of which rows a read served",
    "code_search_trace": "a per-search trace of code search",
    "code_search_outcome": "the outcome of each traced code search",
    "context_blobs": "offloaded context whose writer sets expires_at",
    "idempotency": "replay records of idempotency keys, gone at expires_at",
    "llm_response_cache": "a cache of model responses, refilled on a miss",
    "memory_ask_precedents": "cached answers reused as precedents, recomputed on a miss",
    "memory_drops": "a review log of writes that were dropped",
    "memory_extraction_queue": "a work queue; the facts it yields live in loom.memory",
    "memory_code_link_queue": "the code link worker's queue of memory writes (keys only)",
    "consolidation_runs": "the consolidation worker's run ledger",
    "delete_version_checks": "memory.delete_version's owed checks (identity and key only)",
    "memory_webhook_outbox": "memory webhook events to send (identity, key and id only)",
    "memory_projection_failures": "the repair queue; a resolved entry is done",
    "knowledge_uploads": "a verified upload waiting for its ingestion; the document keeps the file",
    "knowledge_ingestions": "the ingestion job queue and its status; the document holds the result",
    "knowledge_ingest_cancels": "a cancel of an ingestion job, a row of its own beside the job",
}

# Objects that are gone and must not come back.
REMOVED_OBJECTS = (
    "handoffs",
    "handoff_transitions",
    "vector_indexes_applied",
    "qa_items",
    "qa_edges",
    "qa_component_risk",
    "qa_release",
    "code_call_guards",
    "memory_chunk_links",
    "memory_simhash_directory",
    "citation_feedback_rollup",
    "entity_nodes",
    "agent_cost_rollup",
    "memory_term_freq_mv",
    "memory_doc_stats_mv",
    "events_audit_log_mv",
    "events_code_search_outcome_mv",
    "events_memory_exposures_mv",
    "events_query_nodes_mv",
    "events_search_trace_log_mv",
    "memory_exposure_breadth_hourly_mv",
    "subject_last_seen_mv",
    "memory_code_anchors",
    "schema_proposals",
    "chunk_entities",
    "entity_edge_chunks",
    "entity_triple_embeddings",
    "session_outcomes",
    "namespace_calibration_corpus",
)


def test_the_journal_files() -> None:
    assert sorted(p.name for p in MIGRATIONS.glob("*.sql")) == [
        "000_migrations.sql",
        "002_task_leases.sql",
        "016_baseline.sql",
        "017_code_term_df.sql",
        "018_drop_events_fanout_views.sql",
        "019_drop_unread_tables.sql",
        "020_ns_versions_drop_calibration_corpus.sql",
        "021_code_test_map_coverage.sql",
        "022_provider_configuration.sql",
        "023_provider_participants.sql",
        "024_deployment_config.sql",
        "025_agent_run_claims.sql",
        "026_memory_code_links.sql",
        "027_run_id_indexes.sql",
        "028_oauth_claims.sql",
        "029_grant_claims.sql",
        "030_namespace_released_aliases.sql",
        "031_agent_run_budgets.sql",
        "032_table_settings.sql",
        "033_clickhouse_only_state.sql",
        "034_graph_projections.sql",
        "035_instances.sql",
        "036_shared_replica_state.sql",
        "037_carry_acks.sql",
        "038_embedding_write_sha.sql",
        "039_oauth_sessions.sql",
        "040_oauth_signin_state.sql",
        "041_sso_links.sql",
        "042_machine_identities.sql",
        "043_scim_users.sql",
        "044_service_account_lineage.sql",
        "045_request_sli.sql",
        "054_embedding_cache_org.sql",
        "055_delete_version_checks.sql",
        "056_org_model_calls.sql",
        "057_acl_versions.sql",
        "058_collections_and_groups.sql",
        "059_conversation_saved_messages.sql",
        "060_conversation_saved_at.sql",
        "061_profile_schemas.sql",
        "062_extraction_policies.sql",
        "063_memory_webhooks.sql",
        "064_learnings_agent_and_supporters.sql",
        "065_entity_triple_rows.sql",
        "066_projection_failure_abandoned.sql",
        "067_entity_triple_seen_at.sql",
        "068_graph_restore_id.sql",
        "069_entity_edge_rows.sql",
        "070_entity_edge_close_id.sql",
        "071_projection_failure_ttl.sql",
        "072_namespace_guides.sql",
        "073_extraction_queue_enqueued_by.sql",
        "076_machine_platform_admin.sql",
        "077_machine_access_token_ttl.sql",
        "078_document_blob_location.sql",
        "079_chunk_context.sql",
        "080_knowledge_sources.sql",
        "081_erasure_request_actor.sql",
        "082_knowledge_ingestion.sql",
        "083_knowledge_datasets.sql",
        "084_memory_served_daily.sql",
        "085_storage_authority.sql",
        "086_code_answer_memory_scope.sql",
        "087_analysis_secrets.sql",
        "088_assistant_service.sql",
        "089_code_repo_row_kind.sql",
        "090_sql_surface.sql",
        "091_erasure_census.sql",
        "092_collection_titles.sql",
    ]


def test_the_baseline_covers_every_file_before_it() -> None:
    from server.core.schema.journal import baseline_covers, migration_number

    # Numbered after the newest file it replaces: the migrator records it for a
    # ledger that holds that file and refuses a ledger holding only older ones.
    assert baseline_covers(BASELINE) == migration_number(BASELINE.name) == 16


def test_the_baseline_only_creates_and_every_create_is_idempotent() -> None:
    body = "\n".join(
        line for line in BASELINE.read_text().splitlines() if not line.startswith("--")
    )
    heads = re.findall(r"^(CREATE|ALTER|DROP|INSERT|OPTIMIZE)\b.*$", body, re.M)
    assert heads and set(heads) == {"CREATE"}
    assert body.count("CREATE ") == body.count("CREATE TABLE IF NOT EXISTS") + body.count(
        "CREATE MATERIALIZED VIEW IF NOT EXISTS"
    )


def test_the_dump_declares_exactly_what_the_journal_leaves() -> None:
    """A replay applies each file's creates and drops in order; the dump is
    what remains."""
    from server.core.schema.journal import live_objects

    live = live_objects(MIGRATIONS.glob("*.sql"))
    dumped = objects()
    # The loom_telemetry objects are dumped and counted beside loom's.
    telemetry = re.findall(
        r"^CREATE (TABLE|MATERIALIZED VIEW) IF NOT EXISTS (loom_telemetry\.\w+)",
        schema_text(),
        re.M,
    )
    assert dumped | {name for _, name in telemetry} == {n for n in live if not n.startswith("_")}
    kinds = [kind for kind, _ in telemetry]
    n_tables = len(tables()) + kinds.count("TABLE")
    n_views = len(views()) + kinds.count("MATERIALIZED VIEW")
    n_plain = len(dumped) - len(tables()) - len(views())
    counts = f"-- Counts: {n_tables} tables + {n_plain} views + {n_views} materialized views."
    assert counts in schema_text()


def test_removed_objects_stay_removed() -> None:
    """Gone from the dump and from what a replay leaves; a table a later
    file drops may still appear in the baseline that first created it."""
    from server.core.schema.journal import live_objects

    live = live_objects(MIGRATIONS.glob("*.sql"))
    for name in REMOVED_OBJECTS:
        assert f"loom.{name}\n" not in schema_text(), name
        assert name not in live, name


def test_memory_content_tables_carry_no_ttl_unless_transient() -> None:
    from server.erasure.store.census import _CENSUS

    census = {sub.table.removeprefix("loom.") for sub in _CENSUS}
    expiring = {name for name in census if ttl(name) is not None}
    assert expiring == set(TRANSIENT_TABLES), (
        f"census tables with a TTL not declared transient: {sorted(expiring - set(TRANSIENT_TABLES))}; "
        f"declared transient without a TTL: {sorted(set(TRANSIENT_TABLES) - expiring)}"
    )


def test_code_chunks_is_partitioned_by_repository_index() -> None:
    """Each repository index has its own parts, so a retired index leaves by
    DROP PARTITION. A partition key is CREATE-only, so the baseline declares it."""
    key = "\nPARTITION BY (org, namespace)\nORDER BY (org, namespace, embedding_model, path, chunk_id)"
    assert key in ddl("code_chunks")
    baseline = BASELINE.read_text()
    create = baseline.split("CREATE TABLE IF NOT EXISTS loom.code_chunks\n", 1)[1].split(";", 1)[0]
    assert key in create


def test_no_vector_index_is_declared() -> None:
    """No table declares an HNSW index: vector search is an exact cosineDistance scan."""
    assert "vector_similarity(" not in schema_text()
    assert "vector_similarity(" not in BASELINE.read_text()


def test_vector_columns_leave_the_codec_to_the_migrator() -> None:
    """The migrator creates vector columns coded where the server has the
    Quantized codec; the baseline itself stays portable."""
    from server.core.schema.vector_codec_ddl import CODED_TABLES

    assert "Quantized(" not in BASELINE.read_text()
    for table, (cols, _chain) in CODED_TABLES.items():
        for col in cols:
            assert re.search(rf"`{col}` Array\((BFloat16|Float32)\)", ddl(table)), (table, col)


def test_bm25_stats_count_distinct_documents_from_embedded_rows_only() -> None:
    """df is uniqExact over documents, not a per-insert sum a re-embed would
    inflate, and the stat views skip dim=0 stub rows."""
    assert "AggregateFunction(uniqExact" in ddl("memory_term_freq")
    assert "SummingMergeTree" not in ddl("memory_term_freq")
    assert "excerpt_length" in ddl("memory_doc_stats")
    for view in ("memory_term_freq_refresh", "memory_doc_stats_refresh"):
        assert "embedding_dim > 0" in ddl(view), view
