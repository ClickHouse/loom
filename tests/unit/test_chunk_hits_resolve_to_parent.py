"""A memory.set chunk hit names its parent, resolves to it, and can be capped away.

memory.set writes a long value's verbatim chunks, so a memory.search can
return `chunk:memory.set:<key>:<n>` keys. Each must name its parent on the
wire, `memory.get` must read it (the session id carries a colon), `bulk_get`
must find it, and a `chunk_max_share` of 0 must not be undone by the
deployment's chunk floor. Each of those is pinned here.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app.handlers import chunk_parent_key, split_chunk_key
from server.memory.app.retrieval.pool import effective_chunk_floor
from server.memory.store.rows import MemoryRow

# ------------------------------------------------------------- the id


def test_split_chunk_key_keeps_the_session_ids_own_colons() -> None:
    assert split_chunk_key("chunk:memory.set:lesson_0c5d:0") == ("memory.set:lesson_0c5d", 0)
    assert split_chunk_key("chunk:s1:12") == ("s1", 12)
    assert split_chunk_key("lesson_0c5d") == ("", None)
    assert split_chunk_key("chunk:") == ("", None)
    assert split_chunk_key("chunk:s1:notanumber") == ("", None)
    assert split_chunk_key("chunk::3") == ("", None)


def test_chunk_parent_key_reads_the_parent_from_a_memory_set_chunk_id() -> None:
    assert chunk_parent_key("chunk:memory.set:lesson_0c5d:2") == "lesson_0c5d"
    # a key that itself carries colons survives
    assert chunk_parent_key("chunk:memory.set:team/kind/id:v2:0") == "team/kind/id:v2"
    # conversation chunks and non-chunk keys name no parent here
    assert chunk_parent_key("chunk:sess-9:0") == ""
    assert chunk_parent_key("lesson_0c5d") == ""


# ------------------------------------------------------------- the wire


def test_search_hit_carries_parent_memory_key_for_chunk_hits() -> None:
    from server.memory.api.models_search import _MemorySearchHit, _SearchTraceHit

    required = {
        "content_excerpt": "",
        "distance": 0.3,
        "similarity": 0.7,
        "score": 0.7,
        "memory_type": "semantic",
    }
    h = _MemorySearchHit(
        memory_key="chunk:memory.set:lesson_0c5d:0",
        source="chunk",
        parent_memory_key="lesson_0c5d",
        **required,
    )
    assert h.parent_memory_key == "lesson_0c5d"
    assert h.model_dump()["parent_memory_key"] == "lesson_0c5d"  # on the wire
    assert _MemorySearchHit(memory_key="lesson_0c5d", **required).parent_memory_key == ""
    assert "parent_memory_key" in _SearchTraceHit.model_fields


def test_handler_puts_the_parent_on_the_wire_at_every_conversion() -> None:
    """Every place the handler turns an internal hit into a wire hit copies
    the parent key; a chunk hit without one on the wire is the defect."""
    from server.memory.app.search.vector_legs import _plane_fields
    from tests.unit._memory_search_pkg import handler_source

    src = handler_source()
    # no conversion sets `source` on its own; each spreads _plane_fields (the main
    # and supplemental vector legs share one, `_wire_hit`)
    assert 'source=getattr(h, "source"' not in src
    assert src.count("**_plane_fields(h),") >= 3
    from types import SimpleNamespace

    assert _plane_fields(SimpleNamespace(source="chunk", parent_memory_key="lesson_a")) == {
        "source": "chunk",
        "parent_memory_key": "lesson_a",
    }
    assert _plane_fields(SimpleNamespace()) == {"source": "fact", "parent_memory_key": ""}


# ------------------------------------------------------------- memory.get


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append(params or {})
        return self.rows


@pytest.mark.asyncio
async def test_memory_get_resolves_a_memory_set_chunk_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_crud import MemoryGetInput
    from server.memory.app import handlers as M
    from server.memory.app import restore as _restore_mod
    from server.memory.app import stack as _stack_mod

    ch = _CH(
        [
            {
                "chunk_index": 0,
                "content": "first passage",
                "parent_memory_key": "lesson_0c5d",
                "embedding_dim": 2,
            },
            {
                "chunk_index": 1,
                "content": "second passage",
                "parent_memory_key": "lesson_0c5d",
                "embedding_dim": 2,
            },
        ]
    )
    monkeypatch.setattr(M, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", M.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", M.get_client)
    out = await M.handle_memory_get(
        MemoryGetInput(
            org="o",
            namespace="n",
            agent="clickgap",
            memory_key="chunk:memory.set:lesson_0c5d:0",
        )
    )
    assert out.found is True
    assert out.value["parent_memory_key"] == "lesson_0c5d"
    assert out.value["session_id"] == "memory.set:lesson_0c5d"
    assert out.value["chunk_index"] == 0
    # the read asked loom.memory_chunks for the whole session id, not "memory.set"
    assert ch.calls[0]["sid"] == "memory.set:lesson_0c5d"
    assert (ch.calls[0]["lo"], ch.calls[0]["hi"]) == (0, 1)


# ------------------------------------------------------------- bulk_get


def _row(key: str) -> MemoryRow:
    return MemoryRow(
        org="o",
        namespace="ns",
        user_id="-",
        session_id="-",
        memory_id="00000000-0000-0000-0000-000000000001",
        memory_key=key,
        value={"v": key},
        memory_type="semantic",
        confidence=0.7,
        version=5,
        created_at=datetime(2026, 9, 1),
        updated_at=None,
        expires_at=None,
        valid_at=None,
        valid_to=None,
        pinned=False,
        agent="clickgap",
        status="active",
        tags=["kind:lesson"],
        entities=[],
        char_limit=None,
        verified_by="",
        verified_at=None,
        verified_sha256="",
        sensitivity="",
        recall_count=2,
        last_recalled_at=None,
    )


@pytest.mark.asyncio
async def test_bulk_get_answers_a_chunk_id_with_its_parent_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_bulk import MemoryBulkGetInput
    from server.memory.app import query as Q

    asked: list[list[str]] = []

    async def fake_get_many(identity, keys, **kw):
        asked.append(list(keys))
        # chunk ids are search hits, not stored keys
        return [_row(k) for k in keys if k != "gone" and not k.startswith("chunk:")]

    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    out = await Q.handle_memory_bulk_get(
        MemoryBulkGetInput(
            org="o",
            namespace="n",
            agent="clickgap",
            memory_keys=[
                "chunk:memory.set:lesson_a:0",
                "lesson_a",
                "chunk:memory.set:lesson_a:2",
                "chunk:memory.set:gone:1",
                "lesson_b",
            ],
            fields=["value", "canonical_key"],  # no usage counters: no read past the fake
        )
    )
    # one read: the keys as asked, then the parents not already among them
    assert asked == [
        [
            "chunk:memory.set:lesson_a:0",
            "lesson_a",
            "chunk:memory.set:lesson_a:2",
            "chunk:memory.set:gone:1",
            "lesson_b",
            "gone",
        ]
    ]
    assert set(out.results) == {
        "chunk:memory.set:lesson_a:0",
        "lesson_a",
        "chunk:memory.set:lesson_a:2",
        "lesson_b",
    }
    for k in ("chunk:memory.set:lesson_a:0", "chunk:memory.set:lesson_a:2"):
        assert out.results[k].value == {"v": "lesson_a"}
        assert out.results[k].canonical_key == "lesson_a"  # the fold target, on the wire
        # and the alias says so on the wire, whatever the projection, which it still applies
        dumped = out.results[k].model_dump()
        assert dumped["resolved_from"] == k and "tags" not in dumped
    assert "resolved_from" not in out.results["lesson_a"].model_dump()
    assert (
        out.results["lesson_a"].canonical_key is None
    )  # a plain row keeps the canonical-key shape
    assert out.missing == ["chunk:memory.set:gone:1"]


# ------------------------------------------------------------- the floor


def test_effective_chunk_floor_never_exceeds_the_cap() -> None:
    assert effective_chunk_floor(0.4, 1.0) == 0.4
    assert effective_chunk_floor(0.4, 0.0) == 0.0  # chunk_max_share=0 is fact-only
    assert effective_chunk_floor(0.4, 0.2) == 0.2
    assert effective_chunk_floor(0.0, 0.5) == 0.0
    assert effective_chunk_floor(-1.0, 0.5) == 0.0


def test_finish_pool_with_a_zero_cap_serves_no_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment floor (0.4 here) does not promote chunks back into a
    window the request capped at zero."""
    import asyncio

    from server.memory.app.retrieval import pool as S
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_chunk_min_share", 0.4)

    async def no_backfill(ch, q, hits):
        return hits

    monkeypatch.setattr(S, "_recency_backfill", no_backfill)

    def hit(key: str, score: float, source: str) -> Any:
        return SimpleNamespace(
            memory_key=key,
            score=score,
            rank_score=score,
            source=source,
            created_at="",
            valid_at="",
            temporal_anchor="",
        )

    hits = [hit(f"chunk:memory.set:l{i}:0", 0.9 - i * 0.01, "chunk") for i in range(6)]
    hits += [hit(f"lesson_{i}", 0.6 - i * 0.01, "fact") for i in range(6)]
    q = SimpleNamespace(
        top_k=5, recency_intent=False, recency_pool_weight=0.0, chunk_max_share=0.0, scope="private"
    )
    window = asyncio.run(S._finish_pool(None, list(hits), q))  # type: ignore[arg-type]
    assert [h.memory_key for h in window] == [f"lesson_{i}" for i in range(5)]

    # and with no cap set the deployment floor still applies as before
    q2 = SimpleNamespace(
        top_k=5,
        recency_intent=False,
        recency_pool_weight=0.0,
        chunk_max_share=None,
        scope="private",
    )
    window2 = asyncio.run(S._finish_pool(None, list(hits), q2))  # type: ignore[arg-type]
    assert sum(1 for h in window2 if h.source == "chunk") >= 2


@pytest.mark.asyncio
async def test_bulk_get_answers_a_stored_key_shaped_like_a_chunk_id_with_its_own_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # memory.set takes such a key; its own row answers, not its "parent"'s.
    from server.memory import store as memory_repo
    from server.memory.api.models_bulk import MemoryBulkGetInput
    from server.memory.app import query as Q

    async def fake_get_many(identity, keys, **kw):
        return [_row(k) for k in keys]

    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    key = "chunk:memory.set:a:0"
    out = await Q.handle_memory_bulk_get(
        MemoryBulkGetInput(
            org="o", namespace="n", agent="clickgap", memory_keys=[key], fields=["value"]
        )
    )
    assert out.results[key].value == {"v": key} and out.results[key].canonical_key is None


@pytest.mark.asyncio
async def test_bulk_get_never_serves_or_audits_a_parent_nobody_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The export holds a high row `x` home and never asks for it; a stored key shaped like one
    # of its chunk ids must not bring `x`'s value (or a sensitive-read audit row) back.
    from server.memory import store as memory_repo
    from server.memory.api.models_bulk import MemoryBulkGetInput
    from server.memory.app import query as Q
    from server.observability import audit

    async def fake_get_many(identity, keys, **kw):
        high = {"sensitivity": "high", "memory_id": "00000000-0000-0000-0000-000000000002"}
        return [_row(k).model_copy(update=high) if k == "x" else _row(k) for k in keys]

    audited: list[Any] = []

    async def _emit_many(_event: str, **kw: Any) -> None:
        audited.extend(kw["subjects"])

    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    monkeypatch.setattr(audit, "emit_many", _emit_many)
    key = "chunk:memory.set:x:0"
    out = await Q.handle_memory_bulk_get(
        MemoryBulkGetInput(
            org="o", namespace="n", agent="clickgap", memory_keys=[key], fields=["value"]
        )
    )
    assert list(out.results) == [key] and audited == []


@pytest.mark.asyncio
async def test_bulk_get_records_the_served_rows_own_keys_as_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A chunk id answers with its parent's row: the exposure ledger names that row once,
    # however many of its chunk ids were asked.
    from server.memory import store as memory_repo
    from server.memory.api.models_bulk import MemoryBulkGetInput
    from server.memory.app import query as Q

    async def fake_get_many(identity, keys, **kw):
        ids = {
            "x": "00000000-0000-0000-0000-00000000000a",
            "y": "00000000-0000-0000-0000-00000000000b",
        }
        return [_row(k).model_copy(update={"memory_id": ids[k]}) for k in keys if k in ids]

    exposed: list[list[str]] = []
    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    monkeypatch.setattr(Q, "_record_read_exposure", lambda _op, _inp, keys: exposed.append(keys))
    await Q.handle_memory_bulk_get(
        MemoryBulkGetInput(
            org="o",
            namespace="n",
            agent="clickgap",
            memory_keys=["chunk:memory.set:x:0", "chunk:memory.set:x:1", "y"],
            fields=["value"],  # no usage counters: no read past the fake
        )
    )
    assert exposed == [["x", "y"]]


@pytest.mark.asyncio
async def test_get_by_id_never_serves_a_high_row_past_its_retention_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryGetByIdInput
    from server.memory.app import handlers

    expired = _row("vault").model_copy(
        update={"sensitivity": "high", "expires_at": datetime(2020, 1, 1)}
    )

    async def fake_get_by_id(*_a: Any, **_k: Any) -> Any:
        return expired

    monkeypatch.setattr(memory_repo, "get_by_id", fake_get_by_id)
    out = await handlers.handle_memory_get_by_id(
        MemoryGetByIdInput(org="o", memory_id=str(expired.memory_id))
    )
    assert out.found is False
