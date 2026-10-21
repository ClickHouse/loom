"""Unit tests for the dedup operator ops."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.store.dedup import CanonicalRef, DuplicateCluster, KeyedRow


@pytest.mark.asyncio
async def test_memory_dedup_list_enriches_with_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryDedupListInput
    from server.memory.app.dedup import handle_memory_dedup_list

    async def fake_clusters(*a, **kw):
        return [
            DuplicateCluster.from_row(x)
            for x in [
                {
                    "canonical_id": "canon-uuid-1",
                    "dup_count": 2,
                    "dups": [
                        ("dup-uuid-A", "key_A", "agent-1", datetime(2026, 5, 18)),
                        ("dup-uuid-B", "key_B", "agent-2", datetime(2026, 5, 18)),
                    ],
                },
            ]
        ]

    async def fake_canonical(org, mid, *, ch=None):
        return CanonicalRef.from_row(
            {
                "memory_id": "canon-uuid-1",
                "memory_key": "canonical_key",
                "agent": "canonical-agent",
                "value": "value",
                "created_at": datetime(2026, 5, 17),
            }
        )

    monkeypatch.setattr(memory_repo, "find_duplicate_clusters", fake_clusters)
    monkeypatch.setattr(memory_repo, "get_canonical_row", fake_canonical)

    out = await handle_memory_dedup_list(
        MemoryDedupListInput(
            org="clickhouse",
            namespace="finance",
        )
    )
    assert len(out.clusters) == 1
    c = out.clusters[0]
    assert c.canonical_id == "canon-uuid-1"
    assert c.canonical_key == "canonical_key"
    assert c.canonical_agent == "canonical-agent"
    assert c.dup_count == 2
    assert [d.memory_key for d in c.duplicates] == ["key_A", "key_B"]


def _row(mid: str, key: str) -> dict[str, Any]:
    return {
        "memory_id": mid,
        "memory_key": key,
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "status": "duplicate",
        "duplicate_of": "canon-uuid",
        "created_at": datetime(2026, 5, 18),
    }


@pytest.mark.asyncio
async def test_resolve_keep_canonical_tombstones_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.memory.app.dedup import handle_memory_dedup_resolve

    fake_rows = {
        "canon_key": _row("canon-uuid", "canon_key"),
        "dup_key": _row("dup-uuid", "dup_key"),
    }

    async def fake_find(org, ns, key, *, ch=None):
        return KeyedRow.from_row(fake_rows.get(key))

    tombstoned: list[str] = []

    async def fake_tombstone(org, mid, *, version=None, ch=None):
        tombstoned.append(mid)

    clear_calls: list[str] = []

    async def fake_clear(org, mid, *, ch=None):
        clear_calls.append(mid)

    monkeypatch.setattr(memory_repo, "find_row_by_key_namespace", fake_find)
    monkeypatch.setattr(memory_repo, "tombstone_by_id", fake_tombstone)

    async def fake_get_by_id(*_a, **_k):  # the lock guard reads the head
        return None

    monkeypatch.setattr(memory_repo, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(memory_repo, "clear_duplicate_tag", fake_clear)

    out = await handle_memory_dedup_resolve(
        MemoryDedupResolveInput(
            org="clickhouse",
            namespace="finance",
            canonical_key="canon_key",
            duplicate_key="dup_key",
            decision="keep_canonical",
            confirm_canonical_key="canon_key",
        )
    )
    assert out.duplicate_tombstoned is True
    assert out.canonical_tombstoned is False
    assert tombstoned == ["dup-uuid"]
    assert clear_calls == []


@pytest.mark.asyncio
async def test_resolve_keep_duplicate_swaps_via_tombstone_and_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.memory.app.dedup import handle_memory_dedup_resolve

    fake_rows = {
        "canon_key": _row("canon-uuid", "canon_key"),
        "dup_key": _row("dup-uuid", "dup_key"),
    }

    async def fake_find(org, ns, key, *, ch=None):
        return KeyedRow.from_row(fake_rows.get(key))

    tombstoned: list[str] = []

    async def fake_tombstone(org, mid, *, version=None, ch=None):
        tombstoned.append(mid)

    cleared: list[str] = []

    async def fake_clear(org, mid, *, ch=None):
        cleared.append(mid)

    monkeypatch.setattr(memory_repo, "find_row_by_key_namespace", fake_find)
    monkeypatch.setattr(memory_repo, "tombstone_by_id", fake_tombstone)

    async def fake_get_by_id(*_a, **_k):  # the lock guard reads the head
        return None

    monkeypatch.setattr(memory_repo, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(memory_repo, "clear_duplicate_tag", fake_clear)

    out = await handle_memory_dedup_resolve(
        MemoryDedupResolveInput(
            org="clickhouse",
            namespace="finance",
            canonical_key="canon_key",
            duplicate_key="dup_key",
            decision="keep_duplicate",
            confirm_canonical_key="canon_key",
        )
    )
    assert out.canonical_tombstoned is True
    assert tombstoned == ["canon-uuid"]
    assert cleared == ["dup-uuid"]


@pytest.mark.asyncio
async def test_resolve_keep_both_clears_duplicate_tag_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.memory.app.dedup import handle_memory_dedup_resolve

    fake_rows = {
        "canon_key": _row("canon-uuid", "canon_key"),
        "dup_key": _row("dup-uuid", "dup_key"),
    }

    async def fake_find(org, ns, key, *, ch=None):
        return KeyedRow.from_row(fake_rows.get(key))

    tombstoned: list[str] = []

    async def fake_tombstone(*a, **kw):
        tombstoned.append("called")

    cleared: list[str] = []

    async def fake_clear(org, mid, *, ch=None):
        cleared.append(mid)

    monkeypatch.setattr(memory_repo, "find_row_by_key_namespace", fake_find)
    monkeypatch.setattr(memory_repo, "tombstone_by_id", fake_tombstone)

    async def fake_get_by_id(*_a, **_k):  # the lock guard reads the head
        return None

    monkeypatch.setattr(memory_repo, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(memory_repo, "clear_duplicate_tag", fake_clear)

    out = await handle_memory_dedup_resolve(
        MemoryDedupResolveInput(
            org="clickhouse",
            namespace="finance",
            canonical_key="canon_key",
            duplicate_key="dup_key",
            decision="keep_both",
            confirm_canonical_key="canon_key",
        )
    )
    assert out.canonical_tombstoned is False
    assert out.duplicate_tombstoned is False
    assert tombstoned == []
    assert cleared == ["dup-uuid"]


@pytest.mark.asyncio
async def test_resolve_confirm_mismatch_raises_400() -> None:
    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.memory.app.dedup import handle_memory_dedup_resolve

    with pytest.raises(HTTPException) as exc:
        await handle_memory_dedup_resolve(
            MemoryDedupResolveInput(
                org="clickhouse",
                namespace="finance",
                canonical_key="A",
                duplicate_key="B",
                decision="keep_canonical",
                confirm_canonical_key="WRONG",
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_CANONICAL_KEY_MISMATCH"


@pytest.mark.asyncio
async def test_resolve_missing_pair_raises_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.memory.app.dedup import handle_memory_dedup_resolve

    async def fake_find(*a, **kw):
        return None

    monkeypatch.setattr(memory_repo, "find_row_by_key_namespace", fake_find)

    with pytest.raises(HTTPException) as exc:
        await handle_memory_dedup_resolve(
            MemoryDedupResolveInput(
                org="clickhouse",
                namespace="finance",
                canonical_key="A",
                duplicate_key="B",
                decision="keep_canonical",
                confirm_canonical_key="A",
            )
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "DEDUP_PAIR_NOT_FOUND"


@pytest.mark.asyncio
async def test_knowledge_dedup_list_doc_clusters_only_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeDedupListInput
    from server.knowledge.app.dedup import handle_knowledge_dedup_list
    from server.knowledge.store import chunks as knowledge_repo

    async def fake_doc_clusters(*a, **kw):
        return [
            {
                "content_sha256": "hash-1",
                "doc_count": 2,
                "docs": [
                    ("doc-A", "s3://docs/A.pdf", "Q1 policy", datetime(2026, 5, 18)),
                    ("doc-B", "s3://docs/B.pdf", "Q1 policy v2", datetime(2026, 5, 18)),
                ],
            },
        ]

    chunk_called = {"v": False}

    async def fake_chunk_clusters(*a, **kw):
        chunk_called["v"] = True
        return []

    monkeypatch.setattr(knowledge_repo, "find_doc_duplicate_clusters", fake_doc_clusters)
    monkeypatch.setattr(knowledge_repo, "find_chunk_duplicate_clusters", fake_chunk_clusters)

    out = await handle_knowledge_dedup_list(
        KnowledgeDedupListInput(
            org="clickhouse",
            namespace="finance",
        )
    )
    assert len(out.doc_clusters) == 1
    assert out.doc_clusters[0].doc_count == 2
    assert chunk_called["v"] is False
    assert out.chunk_clusters == []


@pytest.mark.asyncio
async def test_knowledge_dedup_list_loads_chunk_clusters_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeDedupListInput
    from server.knowledge.app.dedup import handle_knowledge_dedup_list
    from server.knowledge.store import chunks as knowledge_repo

    async def fake_doc_clusters(*a, **kw):
        return []

    async def fake_chunk_clusters(*a, **kw):
        return [
            {
                "content_sha256": "chunk-hash-1",
                "chunk_count": 5,
                "uri_count": 3,
                "samples": [("s3://docs/A.pdf", 0), ("s3://docs/B.pdf", 2)],
            },
        ]

    monkeypatch.setattr(knowledge_repo, "find_doc_duplicate_clusters", fake_doc_clusters)
    monkeypatch.setattr(knowledge_repo, "find_chunk_duplicate_clusters", fake_chunk_clusters)

    out = await handle_knowledge_dedup_list(
        KnowledgeDedupListInput(
            org="clickhouse",
            namespace="finance",
            include_chunk_clusters=True,
        )
    )
    assert len(out.chunk_clusters) == 1
    assert out.chunk_clusters[0].chunk_count == 5
    assert out.chunk_clusters[0].uri_count == 3


@pytest.mark.asyncio
async def test_knowledge_resolve_keep_canonical_supersedes_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeDedupResolveInput
    from server.knowledge.app.dedup import handle_knowledge_dedup_resolve
    from server.knowledge.store import chunks as knowledge_repo

    superseded: list[str] = []

    async def fake_supersede(org, ns, uri, *, when, ch=None):
        superseded.append(uri)

    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", fake_supersede)

    out = await handle_knowledge_dedup_resolve(
        KnowledgeDedupResolveInput(
            org="clickhouse",
            namespace="finance",
            canonical_uri="s3://docs/A.pdf",
            duplicate_uri="s3://docs/B.pdf",
            decision="keep_canonical",
            confirm_duplicate_uri="s3://docs/B.pdf",
        )
    )
    assert out.superseded_uri == "s3://docs/B.pdf"
    assert superseded == ["s3://docs/B.pdf"]


@pytest.mark.asyncio
async def test_knowledge_resolve_swap_supersedes_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeDedupResolveInput
    from server.knowledge.app.dedup import handle_knowledge_dedup_resolve
    from server.knowledge.store import chunks as knowledge_repo

    superseded: list[str] = []

    async def fake_supersede(org, ns, uri, *, when, ch=None):
        superseded.append(uri)

    monkeypatch.setattr(knowledge_repo, "mark_doc_superseded", fake_supersede)

    out = await handle_knowledge_dedup_resolve(
        KnowledgeDedupResolveInput(
            org="clickhouse",
            namespace="finance",
            canonical_uri="s3://docs/A.pdf",
            duplicate_uri="s3://docs/B.pdf",
            decision="swap",
            confirm_duplicate_uri="s3://docs/B.pdf",
        )
    )
    assert out.superseded_uri == "s3://docs/A.pdf"
    assert superseded == ["s3://docs/A.pdf"]


@pytest.mark.asyncio
async def test_knowledge_resolve_confirm_mismatch_raises_400() -> None:
    from server.knowledge.api.models_knowledge import KnowledgeDedupResolveInput
    from server.knowledge.app.dedup import handle_knowledge_dedup_resolve

    with pytest.raises(HTTPException) as exc:
        await handle_knowledge_dedup_resolve(
            KnowledgeDedupResolveInput(
                org="clickhouse",
                namespace="finance",
                canonical_uri="A",
                duplicate_uri="B",
                decision="keep_canonical",
                confirm_duplicate_uri="WRONG",
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_DUPLICATE_URI_MISMATCH"


@pytest.mark.asyncio
async def test_dedup_ops_registered_admin_audited_appropriately() -> None:
    from server.knowledge.api.ops_knowledge import OPERATIONS as KOPS
    from server.memory.api.ops_memory import OPERATIONS as MOPS
    from server.operations.base import Role

    mem_list = next(o for o in MOPS if o.name == "memory.dedup_list")
    assert mem_list.required_role == Role.ADMIN
    assert mem_list.audit is False

    mem_resolve = next(o for o in MOPS if o.name == "memory.dedup_resolve")
    assert mem_resolve.required_role == Role.ADMIN
    assert mem_resolve.audit is True

    kn_list = next(o for o in KOPS if o.name == "knowledge.dedup_list")
    assert kn_list.required_role == Role.ADMIN
    assert kn_list.audit is False

    kn_resolve = next(o for o in KOPS if o.name == "knowledge.dedup_resolve")
    assert kn_resolve.required_role == Role.ADMIN
    assert kn_resolve.audit is True


@pytest.mark.asyncio
async def test_clear_duplicate_tag_writes_a_full_version_and_mirrors_it(monkeypatch) -> None:
    """The flip is a new version row (history and rollback see it through
    memory_versions; an ALTER UPDATE mutation never reaches the view) carrying
    every column, and the search mirror follows it to 'active'."""
    from server.memory.store import dedup
    from server.memory.store import rows as mr
    from tests.unit.test_rmt_write_is_typed import _every_column_row

    existing = _every_column_row().model_copy(
        update={"status": "duplicate", "duplicate_of": "canon-id"}
    )

    async def _get_by_id(mid, *, org=None, ch=None, fresh=False, **_):
        assert (mid, org, fresh) == (existing.memory_id, "src-org", True)
        return existing

    class _CH:
        def __init__(self) -> None:
            self.inserts: list[tuple[str, list[dict]]] = []
            self.executed: list[str] = []

        async def execute(self, sql, params=None):
            self.executed.append(sql)

        async def query_all(self, sql, params=None):
            emb = {c: None for c in mr.EMBEDDING_PERSISTED_COLS}
            identity = {
                "org": "src-org",
                "namespace": "src-ns",
                "agent": "a",
                "user_id": "u",
                "session_id": "s",
                "memory_key": "k",
            }
            return [emb | identity | {"status": "duplicate"}]

        async def insert(self, table, rows, settings=None):
            self.inserts.append((table, rows))

    monkeypatch.setattr(dedup, "get_by_id", _get_by_id)
    ch = _CH()
    assert await dedup.clear_duplicate_tag("src-org", existing.memory_id, ch=ch) is True
    assert ch.executed == []  # no mutation
    (table, rows), (emb_table, emb_rows) = ch.inserts
    assert table == "loom.memory" and set(rows[0]) == set(mr.MEMORY_PERSISTED_COLS)
    row = rows[0]
    assert (row["status"], row["duplicate_of"]) == ("active", "")
    assert row["version"] > existing.version
    # every other column carried from the live row
    assert row["tags"] == ["t1"] and row["evidence_count"] == 4 and row["recall_count"] == 3
    assert emb_table == "loom.memory_embeddings" and emb_rows[0]["status"] == "active"


@pytest.mark.asyncio
async def test_clear_duplicate_tag_on_a_missing_row_writes_nothing(monkeypatch) -> None:
    from server.memory.store import dedup

    async def _none(*_a, **_k):
        return None

    class _CH:
        async def insert(self, *a, **k):
            raise AssertionError("no write")

    monkeypatch.setattr(dedup, "get_by_id", _none)
    assert await dedup.clear_duplicate_tag("o", "x", ch=_CH()) is False
