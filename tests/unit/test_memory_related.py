"""Unit tests for memory.related (entity-overlap recall).

Boundaries we stub:
  - memory_repo.find_related_by_entities  (CH access)
  - memory_repo.get_by_key                 (used to distinguish
                                            source-missing from
                                            source-has-no-entities)
"""

from __future__ import annotations

from typing import Any

import pytest


def _row(*, key: str, overlap: int, shared: list[str], mtype: str = "semantic") -> dict[str, Any]:
    return {
        "memory_key": key,
        "memory_type": mtype,
        "content_excerpt": f"excerpt for {key}",
        "shared_entities": shared,
        "overlap_count": overlap,
    }


@pytest.mark.asyncio
async def test_returns_hits_ranked_by_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryRelatedInput
    from server.memory.app.dedup import handle_memory_related

    source_entities = ["kafka", "schema-registry", "module-foo"]
    rows = [
        _row(key="bar", overlap=3, shared=["kafka", "schema-registry", "module-foo"]),
        _row(key="baz", overlap=2, shared=["kafka", "schema-registry"]),
        _row(key="qux", overlap=1, shared=["module-foo"]),
    ]

    async def fake_find(identity, memory_key, *, top_k, min_overlap, ch=None):
        assert memory_key == "src"
        assert top_k == 10
        assert min_overlap == 1
        return (source_entities, rows)

    monkeypatch.setattr(memory_repo, "find_related_by_entities", fake_find)

    out = await handle_memory_related(
        MemoryRelatedInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="src",
        )
    )
    assert out.reason == ""
    assert out.source_entities == source_entities
    assert [h.memory_key for h in out.results] == ["bar", "baz", "qux"]
    assert out.results[0].overlap_count == 3
    assert out.results[0].shared_entities == [
        "kafka",
        "schema-registry",
        "module-foo",
    ]


@pytest.mark.asyncio
async def test_source_not_found_returns_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryRelatedInput
    from server.memory.app.dedup import handle_memory_related

    async def fake_find(*a, **kw):
        # Source missing → repo returns ([], [])
        return ([], [])

    async def fake_get_by_key(identity, memory_key, *, ch=None, **_kw):
        return None  # confirms row doesn't exist

    monkeypatch.setattr(memory_repo, "find_related_by_entities", fake_find)
    monkeypatch.setattr(memory_repo, "get_by_key", fake_get_by_key)

    out = await handle_memory_related(
        MemoryRelatedInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="missing",
        )
    )
    assert out.results == []
    assert out.source_entities == []
    assert out.reason == "source_not_found"


@pytest.mark.asyncio
async def test_source_has_no_entities_returns_distinct_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row exists but extractor hasn't tagged it. Distinct reason so
    callers can prompt the operator to enable the extractor instead of
    interpreting the result as 'this memory has no neighbors'."""
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryRelatedInput
    from server.memory.app.dedup import handle_memory_related

    async def fake_find(*a, **kw):
        return ([], [])

    async def fake_get_by_key(identity, memory_key, *, ch=None, **_kw):
        # Minimal stand-in: anything not None will do; the handler only
        # checks `is None` to set the reason code.
        class _Row:
            pass

        return _Row()

    monkeypatch.setattr(memory_repo, "find_related_by_entities", fake_find)
    monkeypatch.setattr(memory_repo, "get_by_key", fake_get_by_key)

    out = await handle_memory_related(
        MemoryRelatedInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="src",
        )
    )
    assert out.results == []
    assert out.source_entities == []
    assert out.reason == "source_has_no_entities"


@pytest.mark.asyncio
async def test_min_overlap_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min_overlap > 1 reaches the repo unchanged (the repo enforces
    the threshold inside the SQL); we only verify the wire-to-repo
    plumbing here."""
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryRelatedInput
    from server.memory.app.dedup import handle_memory_related

    seen = {}

    async def fake_find(identity, memory_key, *, top_k, min_overlap, ch=None):
        seen["top_k"] = top_k
        seen["min_overlap"] = min_overlap
        return (["kafka"], [_row(key="bar", overlap=2, shared=["kafka", "x"])])

    monkeypatch.setattr(memory_repo, "find_related_by_entities", fake_find)

    out = await handle_memory_related(
        MemoryRelatedInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="src",
            top_k=5,
            min_overlap=2,
        )
    )
    assert seen["top_k"] == 5
    assert seen["min_overlap"] == 2
    assert len(out.results) == 1


@pytest.mark.asyncio
async def test_empty_namespace_returns_empty_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source has entities but nothing else in the namespace overlaps —
    valid result, distinct from the precondition-failure paths
    (no reason code; just an empty hit list)."""
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryRelatedInput
    from server.memory.app.dedup import handle_memory_related

    async def fake_find(*a, **kw):
        return (["kafka"], [])

    monkeypatch.setattr(memory_repo, "find_related_by_entities", fake_find)

    out = await handle_memory_related(
        MemoryRelatedInput(
            org="clickhouse",
            namespace="finance",
            agent="reviewer",
            memory_key="src",
        )
    )
    assert out.results == []
    assert out.source_entities == ["kafka"]
    assert out.reason == ""  # not a precondition failure


@pytest.mark.asyncio
async def test_operation_registered_with_viewer_role() -> None:
    """memory.related is read-side; VIEWER must be able to call it."""
    from server.memory.api.ops_memory import OPERATIONS
    from server.operations.base import Role

    op = next(o for o in OPERATIONS if o.name == "memory.related")
    assert op.required_role == Role.VIEWER
    assert op.idempotent is True
    assert op.audit is False
