import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from server.llm import customer
from tests.unit.test_model_call_holds import _Embedder, _stub, _Stubs, _worker
from tests.unit.test_model_call_holds import agents as agents
from tests.unit.test_model_call_holds import clock as clock
from tests.unit.test_model_call_identity import directory as directory
from tests.unit.test_model_call_writers import (
    AGENT_8,
    AGENT_NS,
    AS_AGENT_8,
)
from tests.unit.test_model_call_writers import writers as writers


def test_reembed_uses_the_memory_writer(writers):
    from server.memory.app import reembed

    writers.writers[("acme", AGENT_NS, "a", "u1", "-", "k1")] = f"process:{AGENT_8}"
    seen = []

    class Embedder:
        model_name, dim = "new", 2

        async def embed_many(self, texts):
            seen.append(customer.headers(await customer.require()))
            return [[1.0, 0.0] for _ in texts]

    class CH:
        async def insert(self, *args, **kwargs):
            pass

    row = dict(
        org="acme",
        namespace=AGENT_NS,
        agent="a",
        user_id="u1",
        session_id="-",
        memory_key="k1",
        content_excerpt="agent-8 wrote this",
        embedding_model="old",
        embedding=[1.0, 0.0],
        memory_key_embedding=[1.0, 0.0],
        version=1,
    )
    asyncio.run(reembed._rewrite_page(CH(), Embedder(), "memory_embeddings", [row], key_only=False))
    assert seen == [AS_AGENT_8, AS_AGENT_8]


def test_provider_rebuild_uses_the_memory_writer(writers):
    from server.insights.app import provider_rebuild_batch as batch

    writers.writers[("acme", AGENT_NS, "a", "u1", "-", "k1")] = f"process:{AGENT_8}"
    seen = []

    class Embedder:
        model_name, dim = "new", 2

        async def embed_many(self, texts):
            seen.append(customer.headers(await customer.require()))
            return [[1.0, 0.0] for _ in texts]

    row = dict(
        org="acme",
        namespace=AGENT_NS,
        agent="a",
        user_id="u1",
        session_id="-",
        memory_key="k1",
        content_excerpt="agent-8 wrote this",
        embedding_model="old",
        embedding=[1.0, 0.0],
        memory_key_embedding=[1.0, 0.0],
        embedding_dim=2,
        memory_key_embedding_dim=2,
    )
    asyncio.run(batch.rebuild_batch("memory_embeddings", [row], Embedder()))
    assert seen == [AS_AGENT_8]


def test_a_held_writer_does_not_starve_its_neighbour(agents, clock, monkeypatch):
    store = _Stubs(
        [
            _stub("a1", created=1, version=10),
            _stub("a2", created=2, version=11),
            _stub("b1", created=3, version=12, agent="agent-a"),
        ]
    )
    embedder = _Embedder()
    run = _worker(monkeypatch, store, embedder)
    run(2)
    run(2)
    run(2)
    assert store.embedded == {"b1"}
    assert embedder.calls == [("agent-a", 2), ("agent-b", 1)]
    clock.advance(600)
    run(2)
    run(2)
    assert store.embedded == {"b1"}
    assert embedder.calls == [("agent-a", 2), ("agent-b", 1), ("agent-a", 2)]


def test_projection_writer_matches_the_failures_agent(writers):
    from server.memory.app import projection_failures

    writers.writers[("acme", AGENT_NS, "other-agent", "u1", "-", "k1")] = f"process:{AGENT_8}"
    writers.writers[("acme", AGENT_NS, "target-agent", "u1", "-", "k1")] = (
        "process:sa:cell-t1:agent-7"
    )
    failure = SimpleNamespace(
        org="acme",
        namespace=AGENT_NS,
        agent="target-agent",
        memory_keys=["k1"],
        projection="entity_vectors",
        payload={"user_id": "u1"},
    )
    assert asyncio.run(projection_failures._writer(failure)) == "sa:cell-t1:agent-7"


def test_model_free_projections_do_not_need_a_writer(writers):
    from server.memory.app import projection_failures

    writers.writers[("acme", AGENT_NS, "a", "u1", "s1", "k1")] = f"process:{AGENT_8}"
    writers.writers[("acme", AGENT_NS, "a", "u2", "s2", "k1")] = "process:another-writer"
    for projection in (
        "memory_belief_sources",
        "memory_chunk_sources",
        "entity_edges",
        "entity_triples",
    ):
        failure = SimpleNamespace(
            org="acme",
            namespace=AGENT_NS,
            agent="a",
            memory_keys=["k1"],
            projection=projection,
            payload={},
        )
        assert asyncio.run(projection_failures._writer(failure)) == ""


def test_chunk_repair_uses_the_sources_session(writers):
    from server.memory.app import projection_failures

    writers.writers[("acme", AGENT_NS, "a", "u1", "s1", "k1")] = f"process:{AGENT_8}"
    writers.writers[("acme", AGENT_NS, "a", "u2", "s2", "k1")] = "process:another-writer"
    failure = SimpleNamespace(
        org="acme",
        namespace=AGENT_NS,
        agent="a",
        memory_keys=["k1"],
        projection="memory_chunks",
        payload={},
        source_resource_id="memory.set_from_messages:s1:source",
    )
    assert asyncio.run(projection_failures._writer(failure)) == AGENT_8


def test_ambiguous_repairs_leave_room_for_other_failures(monkeypatch, clock):
    from server.llm.identity import IdentityUnread
    from server.memory.app import projection_failures
    from server.memory.workers import projection_repair
    from tests.unit.switch_state import switch_off

    switch_off()
    failures = [
        SimpleNamespace(
            failure_id=key,
            org="acme",
            namespace="ns",
            projection="memory_chunks",
            payload={},
            created_at=datetime(2026, 1, 1),
        )
        for key in ("ambiguous", "healthy")
    ]
    repaired = []
    marked = []

    class CH:
        async def execute(self, sql, params=None):
            marked.extend(params["failure_ids"])

    async def scan(*, limit, exclude_orgs, after):
        start = next(
            (i + 1 for i, f in enumerate(failures) if after == (f.created_at, f.failure_id)), 0
        )
        return failures[start : start + limit]

    async def writer(failure, **kwargs):
        if failure.failure_id == "ambiguous":
            raise IdentityUnread("more than one writer")
        return ""

    async def retry(failure, **kwargs):
        repaired.append(failure.failure_id)
        return projection_failures.ProjectionFailureRetryResult(
            failure.failure_id, failure.projection, retried=True, resolved=True
        )

    monkeypatch.setattr(projection_failures, "scan_active_projection_failures", scan)
    monkeypatch.setattr(projection_failures, "_retry", retry)
    monkeypatch.setattr(projection_failures, "_writer", writer)
    monkeypatch.setattr(projection_failures, "get_client", lambda: CH())
    config = projection_repair.ProjectionRepairConfig(max_failures_per_pass=1)
    first = asyncio.run(projection_repair.run_pass(config))
    second = asyncio.run(projection_repair.run_pass(config))
    assert (first.retried, first.resolved, first.failed) == (0, 0, [])
    assert (second.retried, second.resolved) == (1, 1)
    assert repaired == ["healthy"]
    assert marked == ["healthy"]


def test_projection_retry_uses_the_original_writer_in_an_admin_context(writers, monkeypatch):
    from server.llm import customer
    from server.memory.app import projection_failures
    from server.memory.store import conflict_resolver

    writers.writers[("acme", AGENT_NS, "a", "u1", "s1", "k1")] = "human:sso-alice"
    failure = projection_failures.ProjectionFailureRow(
        failure_id="repair",
        org="acme",
        namespace=AGENT_NS,
        agent="a",
        projection="memory_chunks",
        source_operation="memory.set_from_messages",
        source_resource_id="memory.set_from_messages:s1:source",
        memory_keys=["k1"],
        error_type="TimeoutError",
        error_message="",
        payload={"user_id": "u1"},
    )
    seen, marked = [], []

    class CH:
        async def execute(self, sql, params=None):
            marked.extend(params["failure_ids"])

    async def not_erasing(org, **kwargs):
        return False

    async def replay(failure, **kwargs):
        seen.append(customer.attribution())

    monkeypatch.setattr(conflict_resolver, "under_erasure", not_erasing)
    monkeypatch.setattr(projection_failures, "_retry_memory_chunks", replay)
    with customer.for_org("admin-org", namespace="admin-ns", subject="admin"):
        original = customer.attribution()
        (result,) = asyncio.run(projection_failures.retry_failures([failure], ch=CH()))
        assert customer.attribution() == original
    assert [(a.org, a.namespace, a.subject, a.user_id) for a in seen] == [
        ("acme", AGENT_NS, "sso-alice", "u1")
    ]
    assert result.resolved and marked == ["repair"]


@pytest.mark.parametrize("stale_first", [True, False])
def test_vector_repair_names_only_the_writers_of_values_still_held(
    writers, monkeypatch, stale_first
):
    from server.llm import customer
    from server.memory.app import projection_failures, projections
    from server.memory.store import conflict_resolver

    writers.writers[("acme", AGENT_NS, "a", "u1", "s1", "k1")] = "human:sso-bob"
    writers.writers[("acme", AGENT_NS, "a", "u1", "s1", "k2")] = "human:sso-alice"
    ids = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    entries = [
        {"memory_key": key, "memory_type": "semantic", "value": value, "entities": [value]}
        for key, value in (("k1", "first"), ("k2", "second"))
    ]
    failure = projection_failures.ProjectionFailureRow(
        failure_id="repair",
        org="acme",
        namespace=AGENT_NS,
        agent="a",
        projection="entity_vectors",
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=["k1", "k2"],
        error_type="TimeoutError",
        error_message="",
        payload={
            "entries": entries,
            "results": [
                {"memory_key": entry["memory_key"], "memory_id": mid, "version": 1}
                for entry, mid in zip(entries, ids, strict=True)
            ],
            "user_id": "u1",
            "session_id": "s1",
        },
    )
    replayed, marked, reads = [], [], []

    class CH:
        async def query_all(self, sql, params=None):
            reads.append(params["versions"])
            return [
                {"id": mid, "sha": sha}
                for mid, sha in params["versions"]
                if not stale_first or mid != ids[0]
            ]

        async def execute(self, sql, params=None):
            marked.extend(params["failure_ids"])

    async def not_erasing(org, **kwargs):
        return False

    async def replay(input, results, embedder, **kwargs):
        replayed.append(([e.memory_key for e in input.entries], customer.attribution().subject))

    monkeypatch.setattr(conflict_resolver, "under_erasure", not_erasing)
    monkeypatch.setattr(projections, "write_entity_vectors", replay)
    (result,) = asyncio.run(projection_failures.retry_failures([failure], ch=CH()))
    if stale_first:
        assert result.resolved and replayed == [(["k2"], "sso-alice")]
        assert marked == ["repair"]
    else:
        assert result.reason == "model_calls_held" and not result.retried
        assert not result.resolved and not result.error
        assert replayed == marked == []
    assert len(reads) == 1


def test_structured_profiles_use_the_newest_principal_in_their_facts(writers, monkeypatch):
    import json

    from server.memory.store import profile_sources
    from server.memory.workers import structured_profile
    from server.settings import settings
    from tests.unit.test_structured_profile_worker import _one_user, _schema

    _one_user(monkeypatch)
    templates = json.loads(settings.inference_identity_headers_json)
    templates.update({"X-User": "{user_id}", "X-Namespace": "{namespace}"})
    monkeypatch.setattr(settings, "inference_identity_headers_json", json.dumps(templates))
    seen = []

    async def facts(**kwargs):
        return [
            dict(memory_id=str(version), value="a fact", since="", actor=actor, version=version)
            for actor, version in (
                ("process:sa:cell-t1:agent-7", 10),
                ("worker:structured-profile", 30),
                (f"process:{AGENT_8}", 20),
            )
        ]

    async def ask(*args):
        seen.append(customer.headers(await customer.require()))
        return {"tz": "UTC"}

    monkeypatch.setattr(profile_sources, "user_facts", facts)
    monkeypatch.setattr(structured_profile, "_ask", ask)
    asyncio.run(structured_profile.build_profile(_schema("acme", AGENT_NS), "u1"))
    assert seen == [{**AS_AGENT_8, "X-User": "u1", "X-Namespace": AGENT_NS}]


def test_chunk_repairs_use_the_user_recorded_in_the_source_evidence(writers, monkeypatch):
    import json

    from server.memory.app import chunk_projection, projection_failures
    from server.settings import settings

    templates = json.loads(settings.inference_identity_headers_json)
    templates["X-User"] = "{user_id}"
    monkeypatch.setattr(settings, "inference_identity_headers_json", json.dumps(templates))
    seen = []

    class CH:
        async def query_all(self, *args, **kwargs):
            return [dict(role="user", content="a conversation turn", user_id="u1")]

    async def chunks(*args, **kwargs):
        seen.append(customer.headers(await customer.require()))

    monkeypatch.setattr(chunk_projection, "write_memory_chunks", chunks)
    failure = SimpleNamespace(
        org="acme",
        namespace=AGENT_NS,
        agent="a",
        memory_keys=["k"],
        source_resource_id="memory.set_from_messages:s1:source",
    )
    with customer.for_org("acme", namespace=AGENT_NS, subject=AGENT_8):
        asyncio.run(projection_failures._retry_memory_chunks(failure, ch=CH()))
    assert seen == [{**AS_AGENT_8, "X-User": "u1"}]
