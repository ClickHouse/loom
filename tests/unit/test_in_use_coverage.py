"""Every table with a column naming a namespace (any column whose name holds `namespace`,
starts with `ns_`, or is a scope) is either read by the in-use check of a new name, or
exempt here with the reason a later owner of the name would inherit nothing from its
rows. A new table must be put in one or the other: unlisted, this test fails."""

from __future__ import annotations

import re
from pathlib import Path

from server.erasure.store.census import namespace_tables
from server.erasure.store.steps_pg import PG_IN_USE_TABLES
from server.identity.store.namespace_census import STATE_TABLES

ROOT = Path(__file__).resolve().parents[2]
_CREATE = re.compile(r"CREATE\s+(?:\w+\s+)*TABLE\s+(?:IF NOT EXISTS\s+)?([\w.]+)\s*\(", re.I)
_ADD_COLUMN = re.compile(r"ALTER\s+TABLE[^;]*ADD\s+COLUMN\s+(?:IF NOT EXISTS\s+)?`?(\w+)", re.I)

_DERIVED = "derived from rows the check reads, or rebuilt from them"
_REQUEST = "records an admitted request (telemetry, audit, usage), not the name's content"
_CODE = "derived from the code index, whose content tables the census reads (code_chunks)"
_TRACKER = "a public tracker record or its rollup; no private content"
_CONFIG = "shapes or refuses writes; holds no content and names no outside target"
_TRANSIENT = "a short-lived claim or projection in Keeper"

EXEMPT: dict[str, str] = {
    "loom.namespaces": "the registry row, which every path reads before the check",
    "loom.analysis_canary": "the analysis probe's one row, at org '' and namespace '', a scope no tenant uses",
    # derived
    "loom.chunk_supersessions": _DERIVED,
    "loom.chunk_term_freq": _DERIVED,
    "loom.memory_term_freq": _DERIVED,
    "loom.memory_doc_stats": _DERIVED,
    "loom.entity_canonical_map": _DERIVED,
    "loom.namespace_calibration_result": _DERIVED,
    "loom.namespace_write_marks": _DERIVED,
    "loom.memory_code_links": _DERIVED,
    "loom.memory_code_link_queue": _DERIVED,
    "loom.blob_uploads": "records who uploaded which original; no content",
    "loom.namespace_snapshots": "counts and the source name of a snapshot; its payload is never stored",
    "loom.llm_response_cache": _DERIVED,
    "loom.code_answers": "an answer cache kept with its index's retired registry row; the memory rows it quotes are read",
    "loom.query_nodes": "beside its memory's rows, which the check reads, and erased with them",
    "loom.procedure_runs": "beside its memory's rows, which the check reads, and erased with them",
    "loom.qa_item_embeddings": _TRACKER,
    "loom.qa_fail_hours": _TRACKER,
    "loom.qa_link_candidates": _TRACKER,
    "loom.qa_rollup_days": _TRACKER,
    "loom.qa_test_daily": _TRACKER,
    "loom.code_backport_gaps": _CODE,
    "loom.erasure_reversals": "records an erasure; holds no content",
    "loom.machine_identities": "a bot's roles, written only for a registered name, whose row stays",
    "loom.service_accounts": "its authority on a name is its creator's or its own live grant",
    "loom.oauth_state": "flows, clients and refresh tokens for names a registered row holds",
    "loom.user_state": "a person's default pick; no authority",
    # requests
    "loom.agent_cost_rollup_daily": _REQUEST,
    "loom.agent_events": _REQUEST,
    "loom.agent_turn_usage": _REQUEST,
    "loom.audit_log": _REQUEST,
    "loom.policy_decisions": _REQUEST,
    "loom.memory_usage_events": _REQUEST,
    "loom.memory_usage_totals": _REQUEST,
    "loom.memory_served_daily": _REQUEST,
    "loom.memory_exposure_breadth_hourly": _REQUEST,
    "loom.namespace_visits": _REQUEST,
    "loom.session_outcomes_by_user": _REQUEST,
    "loom.worker_status": _REQUEST,
    # transient
    "loom.agent_run_claims": _TRANSIENT,
    "loom.graph_projections": _TRANSIENT,
    # configuration
    "loom.blacklist": _CONFIG,
    "loom.categories": _CONFIG,
    "loom.extraction_policies": _CONFIG,
    "loom.namespace_policies": _CONFIG,
    "loom.namespace_ontology": _CONFIG,
    "loom.namespace_types": _CONFIG,
    "loom.memory_schemas": _CONFIG,
    "loom.memory_schema_fields": _CONFIG,
    "loom.profile_schemas": _CONFIG,
    # the code index
    **{
        f"loom.{t}": _CODE
        for t in (
            "code_call_sites_unresolved code_caller_guards code_chunks_prefix "
            "code_context_packs code_coverage_files code_coverage_watches code_doc_stats "
            "code_edges code_files code_gh_items code_index_state code_pr_history "
            "code_pr_reviews code_review_briefs code_scip_batches code_scip_parts "
            "code_search_memo code_symbol_ids code_symbol_names code_symbol_stats "
            "code_symbols code_term_df code_term_freq code_test_facets code_test_map "
            "code_unit_risk code_unit_signal"
        ).split()
    },
}

PG_EXEMPT: dict[str, str] = {
    "loom.memory_deliveries": "which targets a revision still owes; beside its revision",
    "loom.erased_memories": "evidence that rows were erased; holds no content",
    "loom.audit_outbox": _REQUEST,
    "loom.scope_generations": "a counter per scope; no content",
}


def _names_a_namespace(column: str) -> bool:
    return "namespace" in column or column.startswith("ns_") or column in ("scope", "scope_value")


def _keyed_by_name(sql: str) -> set[str]:
    out: set[str] = set()
    for m in _CREATE.finditer(sql):
        body = sql[m.end() :].split("\n);", 1)[0].split("\n)\n", 1)[0]
        if any(_names_a_namespace(c) for c in re.findall(r"^\s*`?(\w+)`?\s", body, re.M)):
            out.add(m.group(1))
    added = [c for c in _ADD_COLUMN.findall(sql) if _names_a_namespace(c)]
    assert not added, f"a namespace column added by ALTER TABLE; list its table here: {added}"
    return out


def test_every_clickhouse_table_keyed_by_namespace_is_read_or_exempt() -> None:
    keyed = _keyed_by_name((ROOT / "schemas" / "schema.sql").read_text())
    read = set(namespace_tables()) | set(STATE_TABLES)
    missing = keyed - read - set(EXEMPT)
    assert not missing, f"read by the in-use check, or exempt with a reason: {sorted(missing)}"
    assert not (read & set(EXEMPT)), "a table is both read and exempt"
    assert set(EXEMPT) <= keyed, f"exempt but gone: {sorted(set(EXEMPT) - keyed)}"


def test_every_postgres_table_keyed_by_namespace_is_read_or_exempt() -> None:
    sql = "\n".join(
        p.read_text() for p in sorted((ROOT / "schemas" / "pg_migrations").glob("*.sql"))
    )
    keyed = _keyed_by_name(sql)
    read = set(PG_IN_USE_TABLES)
    missing = keyed - read - set(PG_EXEMPT)
    assert not (read & set(PG_EXEMPT)), "a table is both read and exempt"
    assert not missing, f"read by the in-use check, or exempt with a reason: {sorted(missing)}"
    assert set(PG_EXEMPT) <= keyed, f"exempt but gone: {sorted(set(PG_EXEMPT) - keyed)}"
