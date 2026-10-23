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

import contextvars
import json
import re
from typing import Any

import pytest

from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.app import saga
from server.erasure.store import census as erasure_census
from server.erasure.store import ledger as erasure_ledger

_STEP: contextvars.ContextVar[str] = contextvars.ContextVar("step", default="")
_ALTER = re.compile(r"ALTER TABLE\s+(\S+)\s+(UPDATE|DELETE)\b")
_LIGHT = re.compile(r"DELETE FROM\s+(\S+)")


class _AnyRow(dict[str, Any]):
    """A read row with a value in every column."""

    def __missing__(self, key: str) -> str:
        return "x"


class _TraceCH:
    """One live row per count, one row for each scope read a step mutates by; every
    mutation is done at the first poll."""

    def __init__(self, trace: dict[str, list[tuple[str, str]]]) -> None:
        self.trace = trace
        # (step, table): a step's INSERT ... SELECT rewrite makes its rows stop matching.
        self.rewritten: set[tuple[str, str]] = set()
        self.executed: list[str] = []  # every statement sent to execute, in order

    async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
        if "system.tables" in sql:
            return {"engine": "KeeperMap"}
        if " AS keys" in sql:
            return {"keys": ["k"]}
        if "count()" not in sql:
            return None
        done = any(f"FROM {t} " in sql for step, t in self.rewritten if step == _STEP.get())
        return {"n": 0 if done else 1}

    async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
        if "system.mutations" in sql:
            if "is_done = 0" in sql:
                return []
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]
        if sql.startswith("SELECT group_id FROM loom.principal_groups"):
            return [{"group_id": "g"}]
        if sql.startswith(("SELECT DISTINCT", "SELECT namespace,")):
            return [_AnyRow()]
        if "blob_path" in sql and "loom.documents" in sql:
            return [{"source_uri": "doc://a", "blob_path": "o/a/blob"}]
        return []

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
        if m := _ALTER.search(sql):
            entry = (m.group(2), m.group(1))
        elif m := _LIGHT.search(sql):
            entry = ("LIGHTWEIGHT", m.group(1))
        else:  # pragma: no cover - a new statement shape shows up as a failure
            entry = ("OTHER", sql.split()[0])
            if sql.startswith("INSERT INTO "):
                self.rewritten.add((_STEP.get(), sql.split()[2]))
        self.trace.setdefault(_STEP.get(), []).append(entry)
        self.executed.append(sql)


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Any:
    trace: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    last: dict[str, Any] = {}
    ch = _TraceCH(trace)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _noop(*a: Any, **kw: Any) -> None:
        return None

    async def _clean(*a: Any, **kw: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(erasure_ledger, "insert_request_checkpoint", _noop)
    monkeypatch.setattr("server.observability.audit.emit", _noop)
    monkeypatch.setattr("server.core.namespace_write_marks.bump", _noop)
    monkeypatch.setattr(erasure_census, "residue_census", _clean)

    real_do_step = saga._do_step

    async def _traced(*, step_name: str, **kw: Any) -> Any:
        order.append(step_name)
        token = _STEP.set(step_name)
        try:
            return await real_do_step(step_name=step_name, **kw)
        finally:
            _STEP.reset(token)

    monkeypatch.setattr(saga, "_do_step", _traced)

    async def _run(
        scope_type: str, scope_value: str, purge: bool = False
    ) -> list[tuple[str, tuple[Any, ...]]]:
        order.clear()
        trace.clear()
        ch.executed.clear()
        inp = ErasureRequestInput(
            org="o", scope_type=scope_type, scope_value=scope_value, requester="ops", purge=purge
        )
        inp, row, erasure_id, now = await saga.begin_erasure(inp)
        out = await saga.run_saga(
            input=inp,
            row=row,
            erasure_id=erasure_id,
            requested_at=now,
            previously_completed=set(),
        )
        assert out.status == "completed", out.failure_reason
        assert [s.step for s in out.steps] == order
        last["out"] = out
        return [(name, tuple(trace.get(name, ()))) for name in order]

    _run.last = last  # type: ignore[attr-defined]
    _run.ch = ch  # type: ignore[attr-defined]
    return _run


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
    ("memory_versions.delete", ((D, "loom.memory_versions"),)),
    ("code_links.delete", _CODE_LINKS),
    ("working_memory.tombstone", ((U, "loom.working_memory"),)),
    ("shared.tombstone", ((U, "loom.shared"),)),
    ("shared_embeddings.tombstone", ((U, "loom.memory_embeddings"),)),
    ("agent_events.scrub", ((U, "loom.agent_events"),)),
    ("derived_facts.delete", ((D, "loom.derived_facts"),)),
    ("memory_reinforcement.delete", ((D, "loom.memory_reinforcement"),)),
    ("raw_evidence.delete", ((D, "loom.memory_evidence"), (D, "loom.memory_extraction_queue"))),
    ("memory_chunks.tombstone", ((U, "loom.memory_chunks"),)),
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
            (D, "loom.entity_triple_memories"),
            (D, "loom.entity_triple_rows"),
            (D, "loom.entity_edge_memories"),
            (D, "loom.entity_edge_rows"),
        ),
    ),
    ("llm_response_cache.delete", ((D, "loom.llm_response_cache"),)),
    ("ask_precedents.delete", ((D, "loom.memory_ask_precedents"),)),
    (
        "consolidated.tombstone",
        ((U, "loom.memory"), (U, "loom.memory_embeddings"), (D, "loom.memory_code_links")),
    ),
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
    ("chunks.supersede", ((U, "loom.chunks"),)),
    (
        "documents.tombstone",
        (
            (D, "loom.document_blobs"),
            (U, "loom.documents"),
            (D, "loom.blob_uploads"),
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
            (D, "loom.entity_triple_rows"),
            (D, "loom.entity_edge_memories"),
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
            (D, "loom.entity_triple_rows"),
            (D, "loom.entity_edge_memories"),
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


def _with_tail(
    trace: list[tuple[str, tuple[tuple[str, str], ...]]], backends: list[str]
) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    return trace + [(b, _SILENT) for b in backends] + [("verify.residue", _SILENT)]


def _ns_list(purge: bool, external: bool) -> str:
    return json.dumps({"namespaces": ["a", "b"], "purge": purge, "external": external})


async def test_user_saga_trace(run: Any) -> None:
    assert await run("user_id", "alice") == _with_tail(USER_TRACE, USER_BACKENDS)


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
