"""Unit tests for the dedup operator ops."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.store.dedup import CanonicalRef, DuplicateCluster
from server.memory.store.rows import MemoryRow


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


CANON = "11111111-1111-1111-1111-111111111111"
DUP = "22222222-2222-2222-2222-222222222222"


def _row(mid: str, key: str, **over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": mid,
        "org": "clickhouse",
        "namespace": "finance",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": key,
        "memory_type": "semantic",
        "value": "v",
        "confidence": 0.8,
        "version": 1,
        "pinned": False,
        "created_at": datetime(2026, 5, 18),
        "expires_at": None,
    }
    return MemoryRow(**(base | over))


def _pair(**dup_over: Any) -> dict[str, MemoryRow]:
    """A canonical row and its duplicate; both share key `k` under two agents."""
    return {
        CANON: _row(CANON, "k", agent="writer-1"),
        DUP: _row(
            DUP,
            "k",
            **({"agent": "writer-2", "status": "duplicate", "duplicate_of": CANON} | dup_over),
        ),
    }


def _fake_store(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, MemoryRow]
) -> tuple[list[str], list[str]]:
    from server.memory import store as memory_repo

    tombstoned: list[str] = []
    cleared: list[str] = []

    async def fake_get_by_id(memory_id: str, *, org: str, **_k: Any) -> MemoryRow | None:
        row = rows.get(memory_id)
        return row if row is not None and row.org == org else None  # the read's org filter

    async def fake_tombstone(org: str, mid: str, *, ch: Any = None) -> list[Any]:
        tombstoned.append(mid)
        return []

    async def fake_clear(org: str, mid: str, *, ch: Any = None) -> None:
        cleared.append(mid)

    monkeypatch.setattr(memory_repo, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(memory_repo, "tombstone_by_id", fake_tombstone)
    monkeypatch.setattr(memory_repo, "clear_duplicate_tag", fake_clear)
    return tombstoned, cleared


def _resolve_input(decision: str, **over: Any) -> Any:
    from server.memory.api.models_graph import MemoryDedupResolveInput

    base: dict[str, Any] = {
        "org": "clickhouse",
        "namespace": "finance",
        "canonical_id": CANON,
        "duplicate_id": DUP,
        "decision": decision,
        "confirm_canonical_key": "k",
    }
    return MemoryDedupResolveInput(**(base | over))


@pytest.mark.asyncio
async def test_resolve_keep_canonical_tombstones_the_duplicate_of_a_shared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical and duplicate share key `k` under two agents: the
    duplicate id is the one tombstoned, never the canonical."""
    from server.memory.app.dedup import handle_memory_dedup_resolve

    tombstoned, cleared = _fake_store(monkeypatch, _pair())
    out = await handle_memory_dedup_resolve(_resolve_input("keep_canonical"))
    assert out.duplicate_tombstoned is True
    assert out.canonical_tombstoned is False
    assert tombstoned == [DUP]
    assert cleared == []


@pytest.mark.asyncio
async def test_resolve_keep_duplicate_swaps_via_tombstone_and_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.dedup import handle_memory_dedup_resolve

    tombstoned, cleared = _fake_store(monkeypatch, _pair())
    out = await handle_memory_dedup_resolve(_resolve_input("keep_duplicate"))
    assert out.canonical_tombstoned is True
    assert tombstoned == [CANON]
    assert cleared == [DUP]


@pytest.mark.asyncio
async def test_resolve_keep_both_clears_duplicate_tag_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.dedup import handle_memory_dedup_resolve

    tombstoned, cleared = _fake_store(monkeypatch, _pair())
    out = await handle_memory_dedup_resolve(_resolve_input("keep_both"))
    assert out.canonical_tombstoned is False
    assert out.duplicate_tombstoned is False
    assert (out.canonical_key, out.duplicate_key) == ("k", "k")
    assert tombstoned == []
    assert cleared == [DUP]


@pytest.mark.asyncio
async def test_resolve_confirm_mismatch_raises_400(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.dedup import handle_memory_dedup_resolve

    tombstoned, _ = _fake_store(monkeypatch, _pair())
    with pytest.raises(HTTPException) as exc:
        await handle_memory_dedup_resolve(
            _resolve_input("keep_canonical", confirm_canonical_key="WRONG")
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_CANONICAL_KEY_MISMATCH"
    assert tombstoned == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "duplicate_id"),
    [
        pytest.param({}, DUP, id="missing"),
        pytest.param({"not-a-uuid": _pair()[DUP]}, "not-a-uuid", id="not-a-uuid"),
        pytest.param(
            _pair(duplicate_of="33333333-3333-3333-3333-333333333333"),
            DUP,
            id="other-canonical",
        ),
        pytest.param(_pair(status="active"), DUP, id="not-a-duplicate"),
        pytest.param(_pair(namespace="other"), DUP, id="duplicate-in-other-namespace"),
        pytest.param(
            _pair() | {CANON: _row(CANON, "k", namespace="other")},
            DUP,
            id="canonical-in-other-namespace",
        ),
        pytest.param(_pair(org="other-org"), DUP, id="duplicate-in-other-org"),
        pytest.param(
            _pair() | {CANON: _row(CANON, "k", org="other-org")},
            DUP,
            id="canonical-in-other-org",
        ),
        pytest.param({DUP: _pair()[DUP]}, DUP, id="canonical-gone"),
    ],
)
async def test_resolve_a_row_that_is_not_the_pair_raises_404(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, MemoryRow], duplicate_id: str
) -> None:
    """A live row that is not this pair's duplicate is never tombstoned."""
    from server.memory.app.dedup import handle_memory_dedup_resolve

    tombstoned, cleared = _fake_store(monkeypatch, rows)
    with pytest.raises(HTTPException) as exc:
        await handle_memory_dedup_resolve(
            _resolve_input("keep_canonical", duplicate_id=duplicate_id)
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "DEDUP_PAIR_NOT_FOUND"
    assert (tombstoned, cleared) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["{%s}", "urn:uuid:%s", "%s"])
async def test_resolve_reads_the_dashed_form_of_any_uuid_spelling(
    monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """ClickHouse parses only the dashed form, so the braced and urn: spellings
    Python accepts are read as the row they name."""
    from server.memory.app.dedup import handle_memory_dedup_resolve

    tombstoned, _ = _fake_store(monkeypatch, _pair())
    await handle_memory_dedup_resolve(
        _resolve_input(
            "keep_canonical",
            canonical_id=spelling % CANON.upper(),
            duplicate_id=spelling % DUP,
        )
    )
    assert tombstoned == [DUP]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "locked"), [("keep_canonical", DUP), ("keep_duplicate", CANON)]
)
async def test_resolve_refuses_to_tombstone_a_locked_side(
    monkeypatch: pytest.MonkeyPatch, decision: str, locked: str
) -> None:
    from server.memory.app.dedup import handle_memory_dedup_resolve

    rows = _pair()
    rows[locked] = rows[locked].model_copy(update={"lock_mode": "no_delete"})
    tombstoned, cleared = _fake_store(monkeypatch, rows)
    with pytest.raises(HTTPException) as exc:
        await handle_memory_dedup_resolve(_resolve_input(decision))
    assert exc.value.status_code == 423
    assert (tombstoned, cleared) == ([], [])


_OLD_CALL: dict[str, Any] = {
    "org": "clickhouse",
    "namespace": "finance",
    "canonical_key": "k",
    "duplicate_key": "k",
    "decision": "keep_canonical",
    "confirm_canonical_key": "k",
}


@pytest.mark.asyncio
async def test_resolve_serves_the_removed_keys_and_names_them_in_ignored_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`canonical_key` / `duplicate_key` are removed: a call that carries the ids
    is served by id, and the keys it still sends are named, never applied."""
    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.memory.app.dedup import handle_memory_dedup_resolve
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import arg_check

    op = OPERATIONS_BY_NAME["memory.dedup_resolve"]
    args = _OLD_CALL | {"canonical_id": CANON, "duplicate_id": DUP, "duplicate_key": "other"}
    assert arg_check.named_argument_error(op, args) is None
    tombstoned, _ = _fake_store(monkeypatch, _pair())
    out = await handle_memory_dedup_resolve(MemoryDedupResolveInput.model_validate(args))
    assert tombstoned == [DUP]
    body = arg_check.response_body(op, args, out)
    assert body["ignored_args"] == ["canonical_key", "duplicate_key"]
    assert arg_check.header_value(op, args, out) == "canonical_key,duplicate_key"


@pytest.mark.asyncio
async def test_resolve_names_no_ignored_args_when_the_keys_are_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.dedup import handle_memory_dedup_resolve

    _fake_store(monkeypatch, _pair())
    out = await handle_memory_dedup_resolve(_resolve_input("keep_canonical"))
    assert out.ignored_args == []


def test_resolve_with_only_the_removed_keys_fails_validation_naming_the_ids() -> None:
    """Without the ids the call names no pair: validation fails on the two id
    fields before the handler runs, so nothing is tombstoned."""
    from pydantic import ValidationError

    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import arg_check

    op = OPERATIONS_BY_NAME["memory.dedup_resolve"]
    assert arg_check.named_argument_error(op, _OLD_CALL) is None
    with pytest.raises(ValidationError) as exc:
        MemoryDedupResolveInput.model_validate(_OLD_CALL)
    missing = {e["loc"][0] for e in exc.value.errors() if e["type"] == "missing"}
    assert missing == {"canonical_id", "duplicate_id"}


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
