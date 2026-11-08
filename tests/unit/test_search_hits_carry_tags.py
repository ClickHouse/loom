"""A search hit carries its row's tags when the caller asks.

Without `tags` on the hit, a consumer whose lesson kind and source live there
pays one memory.get per hit after the search. The dense fact leg already reads
`e.tags` (for `speaker` / `about_other`); the service hit keeps them, the wire hit
carries them under `include_tags=true`, and the gaps (lexical-leg hits) are
point-read once. Off, the key is absent from the payload — not empty — so
every existing client's response is byte-identical.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.app.retrieval import vector_search
from server.memory.domain.search_types import SearchHit, SearchQuery


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        if "FROM loom.memory_embeddings AS e" not in sql:
            return []
        return self.rows


def _row(key: str, tags: list[str]) -> dict[str, Any]:
    return {
        "memory_key": key,
        "memory_type": "semantic",
        "content_excerpt": f"about {key}",
        "distance": 0.2,
        "rank_score": 0.8,
        "temporal_anchor": "",
        "valid_at": "",
        "created_at": "",
        "source_resource_id": "",
        "session_id": "",
        "tags": tags,
    }


def _q() -> SearchQuery:
    return SearchQuery(
        org="o",
        namespace="n",
        agent="a",
        scope="private",
        embedding_model="fake",
        query_vec=[0.1] * 4,
        top_k=5,
        search_mode="semantic",
        execution_planes=("fact",),
    )


def _wire(key: str, **kw: Any) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt="x",
        distance=0.2,
        similarity=0.8,
        **kw,
    )


# ------------------------------------------------------------------ service


@pytest.mark.asyncio
async def test_dense_leg_keeps_the_rows_tags_on_the_service_hit() -> None:
    ch = _CH([_row("k1", ["subsystem:Backups", "kind:lesson"]), _row("k2", [])])
    hits = await vector_search(_q(), ch=ch)
    fact_sql = next(s for s, _ in ch.calls if "FROM loom.memory_embeddings AS e" in s)
    assert "e.tags AS tags" in fact_sql  # the column was already read; no new bytes
    by_key = {h.memory_key: h for h in hits}
    assert by_key["k1"].tags == ["subsystem:Backups", "kind:lesson"]
    assert by_key["k2"].tags == []


def test_service_hit_defaults_to_no_tags() -> None:
    h = SearchHit(memory_key="k", memory_type="semantic", content_excerpt="x", distance=0.2)
    assert h.tags == []


# --------------------------------------------------------------------- wire


def test_include_tags_is_off_by_default() -> None:
    inp = MemorySearchInput(org="o", namespace="n", query="q")
    assert inp.include_tags is False
    assert MemorySearchInput.model_fields["include_tags"].default is False


def test_wire_hit_omits_tags_unless_asked() -> None:
    plain = _wire("k").model_dump(mode="json")
    assert "tags" not in plain  # absent, not empty: byte-identical for old clients
    asked_empty = _wire("k", tags=[]).model_dump(mode="json")
    assert asked_empty["tags"] == []
    asked = _wire("k", tags=["kind:lesson"]).model_dump(mode="json")
    assert asked["tags"] == ["kind:lesson"]
    # the same rule holds through the response envelope
    out = MemorySearchOutput(results=[_wire("k"), _wire("j", tags=["a"])], embedding_model="m")
    dumped = out.model_dump(mode="json")["results"]
    assert "tags" not in dumped[0] and dumped[1]["tags"] == ["a"]


def test_wire_hit_without_tags_round_trips_through_a_parked_page() -> None:
    # A parked page re-validates the dumped hit; an absent key must read back
    # as "not requested" (None), never as an empty list.
    dumped = _wire("k").model_dump(mode="json")
    back = _MemorySearchHit.model_validate(dumped)
    assert back.tags is None


# --------------------------------------------------------------------- fill


class _FillCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return self.rows


def _inp(**kw: Any) -> MemorySearchInput:
    return MemorySearchInput(org="o", namespace="n", query="q", **kw)


@pytest.mark.asyncio
async def test_fill_is_a_no_op_when_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import search_tag_filter as tag_belt  # the tag read lives here
    from server.memory.app.search import payload as mod

    ch = _FillCH([{"namespace": "n", "memory_key": "k", "tags": ["x"]}])
    monkeypatch.setattr(tag_belt, "get_client", lambda: ch)
    hits = [_wire("k")]
    assert await mod._fill_hit_tags(hits, _inp()) == 0
    assert ch.calls == []
    assert hits[0].tags is None
    assert "tags" not in hits[0].model_dump(mode="json")


@pytest.mark.asyncio
async def test_fill_point_reads_only_the_gaps_and_blanks_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import search_tag_filter as tag_belt  # the tag read lives here
    from server.memory.app.search import payload as mod

    ch = _FillCH([{"namespace": "n", "memory_key": "lex", "tags": ["kind:lesson", "source:pin"]}])
    monkeypatch.setattr(tag_belt, "get_client", lambda: ch)
    dense = _wire("dense", tags=["subsystem:Backups"])  # the leg already carried them
    lex = _wire("lex")  # lexical-leg hit: the leg does not select tags
    chunk = _wire("chunk:abc", source="chunk")
    digest = _wire("digest_1", source="summary")
    missed = _wire("gone")  # a fact the read did not find
    hits = [dense, lex, chunk, digest, missed]
    work: dict[str, int] = {}
    filled = await mod._fill_hit_tags(hits, _inp(include_tags=True), ["n"], work=work)
    assert filled == 1
    assert work == {"tags_unread": 1}  # `gone`: its [] is unknown, not empty
    assert len(ch.calls) == 1
    sql, params = ch.calls[0]
    assert "FROM loom.memory_embeddings FINAL" in sql
    assert "PREWHERE" not in sql
    assert params["keys"] == ["gone", "lex"]  # only the gaps, not the whole window
    assert params["nss"] == ["n"]
    assert dense.tags == ["subsystem:Backups"]
    assert lex.tags == ["kind:lesson", "source:pin"]
    assert chunk.tags == [] and digest.tags == []  # asked → the key is present, empty
    for h in hits:
        assert "tags" in h.model_dump(mode="json")


@pytest.mark.asyncio
async def test_fill_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import search_tag_filter as tag_belt  # the tag read lives here
    from server.memory.app.search import payload as mod

    class _Boom:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            raise RuntimeError("replica away")

    monkeypatch.setattr(tag_belt, "get_client", lambda: _Boom())
    hits = [_wire("k")]
    work: dict[str, int] = {}
    assert await mod._fill_hit_tags(hits, _inp(include_tags=True), work=work) == 0
    assert hits[0].tags == []  # asked, unreadable: present and empty, never a failed search
    assert work == {"tags_unread": 1}  # ... and counted as not read
