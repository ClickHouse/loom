"""The erasure sagas' step order and what each step issues, pinned.

Each saga runs end to end against a recording ClickHouse stub that reports
one live row for every count, so every step reaches its mutation. The trace
is the ordered step names with the statements each one submitted, reduced to
(kind, table): `UPDATE` / `DELETE` for an `ALTER TABLE`, `LIGHTWEIGHT` for a
`DELETE FROM`. A refactor of how the steps are declared or submitted must
leave the trace unchanged; a change to the trace is a change to what erasure
does. Step names are stored in the ledger's `steps_completed`, so a rename is
a resume break too. CH-free.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.erasure.store import census as erasure_census
from tests.unit.erasure_saga_stub import ns_list as _ns_list
from tests.unit.erasure_saga_stub import saga_runner


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Any:
    return saga_runner(monkeypatch)


U, D, L = "UPDATE", "DELETE", "LIGHTWEIGHT"
USER_BACKENDS = ["langfuse.delete_user", "clickstack.delete_user"]
NS_BACKENDS = [
    "langfuse.delete_namespace",
    "clickstack.delete_namespace",
]
# The stub backends (nothing configured under the unit defaults) and the
# census (patched clean) issue no statements.
_SILENT: tuple[tuple[str, str], ...] = ()

# under the link worker's queue lease: taken first, released last
_CODE_LINKS = (
    ("OTHER", "INSERT"),
    (D, "loom.memory_code_links"),
    (D, "loom.memory_code_link_queue"),
    (D, "loom._migration_lock"),
)

USER_TRACE: list[tuple[str, tuple[tuple[str, str], ...]]] = [
    ("memory.tombstone", ((U, "loom.memory"),)),
    ("memory_embeddings.tombstone", ((U, "loom.memory_embeddings"),)),
    ("code_links.delete", _CODE_LINKS),
    ("working_memory.tombstone", ((U, "loom.working_memory"),)),
    ("shared.tombstone", ((U, "loom.shared"),)),
    ("shared_embeddings.tombstone", ((U, "loom.memory_embeddings"),)),
    ("agent_events.scrub", ((U, "loom.agent_events"),)),
    ("derived_facts.delete", ((D, "loom.derived_facts"),)),
    ("memory_reinforcement.delete", ((D, "loom.memory_reinforcement"),)),
    ("raw_evidence.delete", ((D, "loom.memory_evidence"), (D, "loom.memory_extraction_queue"))),
    ("memory_chunks.tombstone", ((U, "loom.memory_chunks"), (U, "loom.memory_chunks"))),
    (
        "source_links.tombstone",
        ((U, "loom.memory_belief_sources"), (U, "loom.memory_chunk_sources")),
    ),
    ("entity_graph.delete", ((D, "loom.entity_triples"), (D, "loom.entity_edges"))),
    ("entity_communities.delete", ((D, "loom.entity_communities"),)),
    ("entity_vectors.delete", ((L, "loom.entity_vectors"),)),
    (
        "graph_links.delete",
        (
            (D, "loom.entity_triple_rows"),
            (D, "loom.entity_edge_rows"),
            (D, "loom.entity_triple_memories"),
            (D, "loom.entity_edge_memories"),
        ),
    ),
    (
        "consolidated.tombstone",
        ((U, "loom.memory_embeddings"), (U, "loom.memory"), (D, "loom.memory_code_links")),
    ),
    ("llm_response_cache.delete", ((D, "loom.llm_response_cache"),)),
    ("ask_precedents.delete", ((D, "loom.memory_ask_precedents"),)),
    ("code_answers.delete", ((L, "loom.code_answers"),)),
    ("memory_versions.delete", ((D, "loom.memory_versions"), (D, "loom.memory_versions"))),
    ("summaries.tombstone", ((U, "loom.session_summaries"), (U, "loom.entity_summaries"))),
    (
        "records.tombstone",
        (
            (U, "loom.memory_record_fields"),
            (U, "loom.memory_record_conflicts"),
            (U, "loom.memory_records"),
            (D, "loom.memory_record_versions"),
        ),
    ),
    ("session_outcomes.delete", ((D, "loom.session_outcomes_by_user"),)),
    (
        "memory.retire",
        (
            (U, "loom.memory_embeddings"),
            (U, "loom.memory"),
            (D, "loom.memory_code_links"),
            (U, "loom.memory"),
            (D, "loom.llm_response_cache"),
            (D, "loom.memory_ask_precedents"),
            (L, "loom.code_answers"),
        ),
    ),
    ("chunks.supersede", ((U, "loom.chunks"),)),
    (
        "documents.tombstone",
        (
            (D, "loom.document_blobs"),
            (U, "loom.documents"),
            (D, "loom.blob_uploads"),
            ("OTHER", "INSERT"),  # the user's cancels on another writer's open job, kept
            (D, "loom.knowledge_uploads"),
            (D, "loom.knowledge_ingestions"),
            (D, "loom.knowledge_ingest_cancels"),
            (D, "loom.datasets"),
            (D, "loom.dataset_rows"),
            (U, "loom.documents"),
        ),
    ),
    ("grants.revoke", ((D, "loom.namespace_grants"),)),  # revoked rows of the user, deleted
    ("groups.remove_member", (("OTHER", "INSERT"),)),
    ("personal.retire", (("OTHER", "INSERT"),)),
    ("collections.forget_owner", (("OTHER", "INSERT"), ("OTHER", "INSERT"))),
    ("subject_last_seen.delete", ((D, "loom.subject_last_seen"),)),
    (
        "conversations.tombstone",
        (
            (U, "loom.conversations"),
            (U, "loom.conversation_threads"),
            (U, "loom.conversation_embeddings"),
        ),
    ),
    ("context_blobs.tombstone", ((U, "loom.context_blobs"),)),
    ("session_briefs.delete", ((D, "loom.session_briefs"), (D, "loom.session_brief_revisions"))),
    ("memory_exposures.delete", ((D, "loom.memory_exposures"),)),
    ("code_search_trace.delete", ((D, "loom.code_search_trace"),)),
    ("events.delete", ((D, "loom.events"),)),
    ("delete_version_checks.delete", ((D, "loom.delete_version_checks"),)),
    ("memory_webhook_outbox.delete", ((D, "loom.memory_webhook_outbox"),)),
    ("agent_runs.delete", ((D, "loom.agent_run_claims"), (D, "loom.agent_runs"))),
    # The fake finds no candidate left without a supporter: only the user's rows go.
    ("learnings_queue.delete", ((D, "loom.learnings_supporters"),)),
    ("consolidation_runs.delete", ((D, "loom.consolidation_runs"),)),
    ("projection_failures.delete", ((D, "loom.memory_projection_failures"),)),
    ("idempotency.delete", ((D, "loom.idempotency"), (L, "loom.embedding_cache"))),
]

NS_TRACE: list[tuple[str, tuple[tuple[str, str], ...]]] = [
    ("memory.tombstone", ((U, "loom.memory"),)),
    ("memory_embeddings.tombstone", ((U, "loom.memory_embeddings"),)),
    ("memory_versions.delete", ((D, "loom.memory_versions"),)),
    ("code_links.delete", _CODE_LINKS),
    ("working_memory.tombstone", ((U, "loom.working_memory"),)),
    ("entity_canonical_map.tombstone", ((U, "loom.entity_canonical_map"),)),
    ("derived_facts.delete", ((D, "loom.derived_facts"),)),
    ("memory_reinforcement.delete", ((D, "loom.memory_reinforcement"),)),
    ("raw_evidence.delete", ((D, "loom.memory_evidence"), (D, "loom.memory_extraction_queue"))),
    ("memory_chunks.tombstone", ((U, "loom.memory_chunks"),)),
    (
        "source_links.tombstone",
        ((U, "loom.memory_belief_sources"), (U, "loom.memory_chunk_sources")),
    ),
    (
        "graph_links.delete",
        (
            (D, "loom.entity_triple_memories"),
            (D, "loom.entity_edge_memories"),
            (D, "loom.entity_triple_rows"),
            (D, "loom.entity_edge_rows"),
        ),
    ),
    (
        "entity_graph.delete",
        (
            (D, "loom.entity_edges"),
            (D, "loom.entity_triples"),
            (D, "loom.entity_degree"),
        ),
    ),
    ("entity_vectors.delete", ((L, "loom.entity_vectors"),)),
    ("summaries.tombstone", ((U, "loom.session_summaries"), (U, "loom.entity_summaries"))),
    (
        "records.tombstone",
        (
            (U, "loom.memory_records"),
            (U, "loom.memory_record_fields"),
            (U, "loom.memory_record_conflicts"),
            (D, "loom.memory_record_versions"),
        ),
    ),
    ("entity_communities.delete", ((D, "loom.entity_communities"),)),
    ("ask_precedents.delete", ((D, "loom.memory_ask_precedents"),)),
    ("llm_response_cache.delete", ((D, "loom.llm_response_cache"),)),
    ("code_answers.delete", ((L, "loom.code_answers"),)),
    ("procedure_runs.delete", ((D, "loom.procedure_runs"),)),
    ("query_nodes.delete", ((D, "loom.query_nodes"),)),
    (
        "session_outcomes.delete",
        ((D, "loom.session_outcomes_by_user"),),
    ),
    (
        "term_aggregates.delete",
        ((D, "loom.memory_term_freq"), (D, "loom.memory_doc_stats"), (D, "loom.chunk_term_freq")),
    ),
    ("chunks.supersede", ((U, "loom.chunks"),)),
    (
        "documents.tombstone",
        (
            (D, "loom.document_blobs"),
            (U, "loom.documents"),
            (D, "loom.blob_uploads"),
            (D, "loom.knowledge_uploads"),
            (D, "loom.knowledge_ingestions"),
            (D, "loom.knowledge_ingest_cancels"),
            (D, "loom.datasets"),
            (D, "loom.dataset_rows"),
            (U, "loom.documents"),
        ),
    ),
    (
        "conversations.tombstone",
        (
            (U, "loom.conversations"),
            (U, "loom.conversation_threads"),
            (U, "loom.conversation_embeddings"),
        ),
    ),
    ("context_blobs.tombstone", ((U, "loom.context_blobs"),)),
    ("session_briefs.delete", ((D, "loom.session_briefs"), (D, "loom.session_brief_revisions"))),
    ("memory_exposures.delete", ((D, "loom.memory_exposures"),)),
    ("search_trace_log.delete", ((D, "loom.search_trace_log"),)),
    ("code_search_trace.delete", ((D, "loom.code_search_trace"), (D, "loom.code_search_outcome"))),
    ("events.delete", ((D, "loom.events"),)),
    ("memory_drops.delete", ((D, "loom.memory_drops"),)),
    ("delete_version_checks.delete", ((D, "loom.delete_version_checks"),)),
    ("memory_webhook_outbox.delete", ((D, "loom.memory_webhook_outbox"),)),
    ("learnings_queue.delete", ((D, "loom.learnings_supporters"), (D, "loom.learnings_queue"))),
    ("namespace_guides.delete", ((D, "loom.namespace_guides"),)),
    ("tasks.delete", ((D, "loom.tasks"), (D, "loom.task_events"))),
    ("agent_runs.delete", ((D, "loom.agent_run_claims"), (D, "loom.agent_runs"))),
    (
        "curation.tombstone",
        ((U, "loom.operator_edges"), (U, "loom.conflict_allowlist"), (U, "loom.memory_categories")),
    ),
    ("consolidation_runs.delete", ((D, "loom.consolidation_runs"),)),
    ("projection_failures.delete", ((D, "loom.memory_projection_failures"),)),
    ("idempotency.delete", ((D, "loom.idempotency"), (L, "loom.embedding_cache"))),
    ("code_index.purge", ()),
]

NS_PURGE_TRACE: list[tuple[str, tuple[tuple[str, str], ...]]] = [
    ("memory.tombstone", ((D, "loom.memory"),)),
    ("memory_embeddings.tombstone", ((L, "loom.memory_embeddings"),)),
    ("memory_versions.delete", ((D, "loom.memory_versions"),)),
    ("code_links.delete", _CODE_LINKS),
    ("working_memory.tombstone", ((D, "loom.working_memory"),)),
    ("entity_canonical_map.tombstone", ((D, "loom.entity_canonical_map"),)),
    ("derived_facts.delete", ((D, "loom.derived_facts"),)),
    ("memory_reinforcement.delete", ((D, "loom.memory_reinforcement"),)),
    ("raw_evidence.delete", ((L, "loom.memory_evidence"), (D, "loom.memory_extraction_queue"))),
    ("memory_chunks.tombstone", ((L, "loom.memory_chunks"),)),
    (
        "source_links.tombstone",
        ((D, "loom.memory_belief_sources"), (D, "loom.memory_chunk_sources")),
    ),
    (
        "graph_links.delete",
        (
            (D, "loom.entity_triple_memories"),
            (D, "loom.entity_edge_memories"),
            (D, "loom.entity_triple_rows"),
            (D, "loom.entity_edge_rows"),
        ),
    ),
    (
        "entity_graph.delete",
        (
            (D, "loom.entity_edges"),
            (D, "loom.entity_triples"),
            (D, "loom.entity_degree"),
        ),
    ),
    ("entity_vectors.delete", ((L, "loom.entity_vectors"),)),
    ("summaries.tombstone", ((L, "loom.session_summaries"), (D, "loom.entity_summaries"))),
    (
        "records.tombstone",
        (
            (D, "loom.memory_records"),
            (D, "loom.memory_record_fields"),
            (D, "loom.memory_record_conflicts"),
            (D, "loom.memory_record_versions"),
        ),
    ),
    ("entity_communities.delete", ((D, "loom.entity_communities"),)),
    ("ask_precedents.delete", ((D, "loom.memory_ask_precedents"),)),
    ("llm_response_cache.delete", ((D, "loom.llm_response_cache"),)),
    ("code_answers.delete", ((L, "loom.code_answers"),)),
    ("procedure_runs.delete", ((D, "loom.procedure_runs"),)),
    ("query_nodes.delete", ((D, "loom.query_nodes"),)),
    (
        "session_outcomes.delete",
        ((D, "loom.session_outcomes_by_user"),),
    ),
    (
        "term_aggregates.delete",
        ((D, "loom.memory_term_freq"), (D, "loom.memory_doc_stats"), (D, "loom.chunk_term_freq")),
    ),
    ("chunks.supersede", ((L, "loom.chunks"),)),
    (
        "documents.tombstone",
        (
            (D, "loom.document_blobs"),
            (U, "loom.documents"),
            (D, "loom.blob_uploads"),
            (D, "loom.knowledge_uploads"),
            (D, "loom.knowledge_ingestions"),
            (D, "loom.knowledge_ingest_cancels"),
            (D, "loom.datasets"),
            (D, "loom.dataset_rows"),
            (D, "loom.documents"),
        ),
    ),
    (
        "conversations.tombstone",
        (
            (D, "loom.conversations"),
            (D, "loom.conversation_threads"),
            (L, "loom.conversation_embeddings"),
        ),
    ),
    ("context_blobs.tombstone", ((D, "loom.context_blobs"),)),
    ("session_briefs.delete", ((D, "loom.session_briefs"), (D, "loom.session_brief_revisions"))),
    ("memory_exposures.delete", ((D, "loom.memory_exposures"),)),
    ("search_trace_log.delete", ((D, "loom.search_trace_log"),)),
    ("code_search_trace.delete", ((D, "loom.code_search_trace"), (D, "loom.code_search_outcome"))),
    ("events.delete", ((D, "loom.events"),)),
    ("memory_drops.delete", ((D, "loom.memory_drops"),)),
    ("delete_version_checks.delete", ((D, "loom.delete_version_checks"),)),
    ("memory_webhook_outbox.delete", ((D, "loom.memory_webhook_outbox"),)),
    ("learnings_queue.delete", ((D, "loom.learnings_supporters"), (D, "loom.learnings_queue"))),
    ("namespace_guides.delete", ((D, "loom.namespace_guides"),)),
    ("tasks.delete", ((D, "loom.tasks"), (D, "loom.task_events"))),
    ("agent_runs.delete", ((D, "loom.agent_run_claims"), (D, "loom.agent_runs"))),
    (
        "curation.tombstone",
        ((D, "loom.operator_edges"), (D, "loom.conflict_allowlist"), (D, "loom.memory_categories")),
    ),
    ("consolidation_runs.delete", ((D, "loom.consolidation_runs"),)),
    ("projection_failures.delete", ((D, "loom.memory_projection_failures"),)),
    ("idempotency.delete", ((D, "loom.idempotency"), (L, "loom.embedding_cache"))),
    ("code_index.purge", ()),
]


# A user purge: the memory rows the later steps read the user's keys from are stamped
# first and deleted, with the summaries built from them, in `memory.retire`.
USER_PURGE_TRACE: list[tuple[str, tuple[tuple[str, str], ...]]] = [
    ("memory.tombstone", ((U, "loom.memory"),)),
    ("memory_embeddings.tombstone", ((L, "loom.memory_embeddings"),)),
    ("code_links.delete", _CODE_LINKS),
    ("working_memory.tombstone", ((D, "loom.working_memory"),)),
    ("shared.tombstone", ((D, "loom.shared"),)),
    ("shared_embeddings.tombstone", ((L, "loom.memory_embeddings"),)),
    ("agent_events.scrub", ((U, "loom.agent_events"),)),
    ("derived_facts.delete", ((D, "loom.derived_facts"),)),
    ("memory_reinforcement.delete", ((D, "loom.memory_reinforcement"),)),
    (
        "raw_evidence.delete",
        (
            (L, "loom.memory_evidence"),
            (D, "loom.memory_extraction_queue"),
        ),
    ),
    ("memory_chunks.tombstone", ((L, "loom.memory_chunks"), (L, "loom.memory_chunks"))),
    (
        "source_links.tombstone",
        (
            (D, "loom.memory_belief_sources"),
            (D, "loom.memory_chunk_sources"),
        ),
    ),
    (
        "entity_graph.delete",
        (
            (D, "loom.entity_triples"),
            (D, "loom.entity_edges"),
        ),
    ),
    ("entity_communities.delete", ((D, "loom.entity_communities"),)),
    ("entity_vectors.delete", ((L, "loom.entity_vectors"),)),
    (
        "graph_links.delete",
        (
            (D, "loom.entity_triple_rows"),
            (D, "loom.entity_edge_rows"),
            (D, "loom.entity_triple_memories"),
            (D, "loom.entity_edge_memories"),
        ),
    ),
    (
        "consolidated.tombstone",
        (
            (L, "loom.memory_embeddings"),
            (U, "loom.memory"),
            (D, "loom.memory_code_links"),
        ),
    ),
    ("llm_response_cache.delete", ((D, "loom.llm_response_cache"),)),
    ("ask_precedents.delete", ((D, "loom.memory_ask_precedents"),)),
    ("code_answers.delete", ((L, "loom.code_answers"),)),
    ("memory_versions.delete", ((D, "loom.memory_versions"), (D, "loom.memory_versions"))),
    (
        "summaries.tombstone",
        (
            (L, "loom.session_summaries"),
            (D, "loom.entity_summaries"),
        ),
    ),
    (
        "records.tombstone",
        (
            (D, "loom.memory_record_fields"),
            (D, "loom.memory_record_conflicts"),
            (D, "loom.memory_records"),
            (D, "loom.memory_record_versions"),
        ),
    ),
    ("session_outcomes.delete", ((D, "loom.session_outcomes_by_user"),)),
    (
        "memory.retire",
        (
            (L, "loom.memory_embeddings"),
            (U, "loom.memory"),
            (D, "loom.memory_code_links"),
            (U, "loom.memory"),
            (D, "loom.llm_response_cache"),
            (D, "loom.memory_ask_precedents"),
            (L, "loom.code_answers"),
            (D, "loom.memory"),
            (D, "loom.memory"),
        ),
    ),
    ("chunks.supersede", ((L, "loom.chunks"),)),
    (
        "documents.tombstone",
        (
            (D, "loom.document_blobs"),
            (U, "loom.documents"),
            (D, "loom.blob_uploads"),
            ("OTHER", "INSERT"),
            (D, "loom.knowledge_uploads"),
            (D, "loom.knowledge_ingestions"),
            (D, "loom.knowledge_ingest_cancels"),
            (D, "loom.datasets"),
            (D, "loom.dataset_rows"),
            (U, "loom.documents"),
            (D, "loom.documents"),
        ),
    ),
    ("grants.revoke", ((D, "loom.namespace_grants"),)),
    ("groups.remove_member", (("OTHER", "INSERT"),)),
    ("personal.retire", (("OTHER", "INSERT"),)),
    (
        "collections.forget_owner",
        (
            ("OTHER", "INSERT"),
            ("OTHER", "INSERT"),
        ),
    ),
    ("subject_last_seen.delete", ((D, "loom.subject_last_seen"),)),
    (
        "conversations.tombstone",
        (
            (D, "loom.conversations"),
            (D, "loom.conversation_threads"),
            (L, "loom.conversation_embeddings"),
        ),
    ),
    ("context_blobs.tombstone", ((D, "loom.context_blobs"),)),
    (
        "session_briefs.delete",
        (
            (D, "loom.session_briefs"),
            (D, "loom.session_brief_revisions"),
        ),
    ),
    ("memory_exposures.delete", ((D, "loom.memory_exposures"),)),
    ("code_search_trace.delete", ((D, "loom.code_search_trace"),)),
    ("events.delete", ((D, "loom.events"),)),
    ("delete_version_checks.delete", ((D, "loom.delete_version_checks"),)),
    ("memory_webhook_outbox.delete", ((D, "loom.memory_webhook_outbox"),)),
    (
        "agent_runs.delete",
        (
            (D, "loom.agent_run_claims"),
            (D, "loom.agent_runs"),
        ),
    ),
    ("learnings_queue.delete", ((D, "loom.learnings_supporters"),)),
    ("consolidation_runs.delete", ((D, "loom.consolidation_runs"),)),
    ("projection_failures.delete", ((D, "loom.memory_projection_failures"),)),
    (
        "idempotency.delete",
        (
            (D, "loom.idempotency"),
            (L, "loom.embedding_cache"),
        ),
    ),
]


def _with_tail(
    trace: list[tuple[str, tuple[tuple[str, str], ...]]], backends: list[str]
) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    return trace + [(b, _SILENT) for b in backends] + [("verify.residue", _SILENT)]


async def test_user_saga_trace(run: Any) -> None:
    assert await run("user_id", "alice") == _with_tail(USER_TRACE, USER_BACKENDS)


async def test_user_purge_saga_trace(run: Any) -> None:
    got = await run("user_id", "alice", purge=True)
    assert got == _with_tail(USER_PURGE_TRACE, USER_BACKENDS)


@pytest.mark.parametrize(
    ("scope_type", "scope_value", "backends"),
    [
        ("namespace", "a", NS_BACKENDS),
        ("namespace_list", _ns_list(purge=False, external=True), NS_BACKENDS),
        ("namespace_list", _ns_list(purge=False, external=False), []),
    ],
)
async def test_namespace_saga_trace(
    run: Any, scope_type: str, scope_value: str, backends: list[str]
) -> None:
    assert await run(scope_type, scope_value) == _with_tail(NS_TRACE, backends)


@pytest.mark.parametrize("external", [True, False])
async def test_namespace_purge_saga_trace(run: Any, external: bool) -> None:
    """Under purge every tombstone becomes a delete: the wide tables by a
    lightweight DELETE, the narrow ones by an ALTER DELETE. Same steps."""
    got = await run("namespace_list", _ns_list(purge=True, external=external))
    assert got == _with_tail(NS_PURGE_TRACE, NS_BACKENDS if external else [])


@pytest.mark.parametrize(("scope_type", "scope_value"), [("user_id", "alice"), ("namespace", "a")])
async def test_every_census_owner_step_runs(run: Any, scope_type: str, scope_value: str) -> None:
    """A table the census counts is erased by a step the saga actually runs."""
    names = {name for name, _ in await run(scope_type, scope_value)}
    owners = set(erasure_census.step_owners(scope_type))
    assert owners <= names, sorted(owners - names)


async def test_unconfigured_backends_are_noted_as_stubs(run: Any) -> None:
    await run("namespace", "a")
    notes = {s.step: s.notes for s in run.last["out"].steps}
    assert {b: notes[b] for b in NS_BACKENDS} == dict.fromkeys(NS_BACKENDS, "stub: not configured")
    assert notes["memory.tombstone"] == ""


def test_a_code_link_erasure_waits_longer_than_a_linking_pass_runs() -> None:
    from server.erasure.app import saga_steps
    from server.workers.registry import _WORKERS, _spec_for

    (row,) = [w for w in _WORKERS if w.name == "memory_code_links"]
    assert _spec_for(row).deadline_seconds() < saga_steps._LINK_PASS_WAIT_S
