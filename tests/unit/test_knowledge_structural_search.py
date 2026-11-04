"""Structural knowledge search: the exact-key leg, the format and block-type filters,
the locator, block type and revision each hit carries, and the located citations of
knowledge.synthesize. Over stubbed statements; the live path is
tests/integration/test_knowledge_structural_search.py."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.knowledge.app.search import ChunkHit, HybridSearchQuery, fuse_rrf, keys_search
from server.knowledge.domain.exact_keys import MAX_QUERY_KEYS, query_keys


class StubCH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        return self.rows


def _q(**kw: Any) -> HybridSearchQuery:
    base: dict[str, Any] = {
        "org": "o",
        "namespaces": ("ns",),
        "embedding_model": "m",
        "query_vec": [0.1] * 4,
        "top_k": 5,
    }
    base.update(kw)
    return HybridSearchQuery(**base)


def _hit(uri: str, *, distance: float = 0.3, keys: tuple[str, ...] = ()) -> ChunkHit:
    return ChunkHit(
        namespace="ns",
        source_uri=uri,
        chunk_idx=0,
        content=uri,
        tags=[],
        distance=distance,
        keys_matched=keys,
    )


# ---- the query's keys ---------------------------------------------------------------


def test_a_query_names_its_identifier_shaped_terms_as_written_and_lowercased() -> None:
    assert query_keys("Where is Events defined? server.port and db.Events") == [
        "Where",
        "where",
        "is",
        "Events",
        "events",
        "defined",
        "server.port",
        "and",
        "db.Events",
        "db.events",
    ]
    assert query_keys("a 1 2 x") == [] and query_keys("") == []
    assert len(query_keys(" ".join(f"term{i}" for i in range(100)))) == MAX_QUERY_KEYS


# ---- the exact-key leg ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_keys_leg_matches_the_chunks_keys_and_reads_their_structure() -> None:
    ch = StubCH(
        rows=[
            {
                "namespace": "ns",
                "source_uri": "repo://retention.sql",
                "chunk_idx": 0,
                "content": "CREATE TABLE events (...)",
                "context": "",
                "tags": [],
                "chunk_metadata": (
                    '{"block_type": "sql_statement", "section_path": [],'
                    ' "locator": {"line_start": 1, "line_end": 3}}'
                ),
                "distance": 0.4,
                "matched": ["events"],
            }
        ]
    )
    (hit,) = await keys_search(_q(keys=["events", "Events"]), ch=ch)
    sql, params = ch.calls[0]
    assert "hasAny(keys, {keys:Array(String)})" in sql
    assert "ORDER BY length(matched) DESC, distance ASC" in sql
    assert params["keys"] == ["events", "Events"] and params["org"] == "o"
    assert (hit.keys_matched, hit.block_type) == (("events",), "sql_statement")
    assert hit.locator == {"line_start": 1, "line_end": 3}
    assert await keys_search(_q(keys=[]), ch=StubCH()) == []


def test_fusion_gives_a_keyed_hit_its_own_term_and_keeps_its_keys() -> None:
    sem = [_hit("a", distance=0.1), _hit("b", distance=0.2)]
    keyed = [_hit("c", distance=0.5, keys=("events",)), _hit("b", keys=("server.port",))]
    by = {h.source_uri: h for h in fuse_rrf(sem, [], top_k=10, keyed=keyed)}
    assert math.isclose(by["c"].score, 1 / 61)  # keyed-only: the key leg's term
    assert math.isclose(by["b"].score, 1 / 62 + 1 / 62)  # both legs
    assert by["c"].keys_matched == ("events",) and by["b"].keys_matched == ("server.port",)
    assert by["a"].keys_matched == ()


# ---- the filters ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_format_and_block_type_filters_narrow_every_leg() -> None:
    from server.knowledge.app.search import hybrid_search

    ch = StubCH()
    await hybrid_search(_q(formats=["sql", "toml"], block_types=["sql_statement"]), ch=ch)
    sql, params = ch.calls[0]
    assert "JSONExtractString(chunk_metadata, 'block_type') IN {block_types:Array(String)}" in sql
    assert (
        "(namespace, source_uri) IN (SELECT namespace, source_uri FROM loom.documents FINAL" in sql
    )
    assert "superseded_at IS NULL AND format IN {formats:Array(String)}" in sql
    assert (params["formats"], params["block_types"]) == (["sql", "toml"], ["sql_statement"])
    await hybrid_search(_q(), ch=ch)
    assert "block_type" not in ch.calls[1][0] and "loom.documents" not in ch.calls[1][0]


# ---- the handler --------------------------------------------------------------------------


def _handler_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.app import knowledge_search as h
    from server.knowledge.store import documents as documents_repo

    seen: dict[str, Any] = {}

    async def _embed(text: str) -> list[float]:
        return [0.1] * 4

    async def _sem(q: Any, **kw: Any) -> list[ChunkHit]:
        seen["query"] = q
        return [_hit("repo://a.md", distance=0.1)]

    async def _lex(q: Any, **kw: Any) -> list[ChunkHit]:
        return []

    async def _keyed(q: Any, **kw: Any) -> list[ChunkHit]:
        seen["keys"] = list(q.keys)
        return [_hit("repo://retention.sql", distance=0.6, keys=("events",))]

    async def _stats(ch: Any, **kw: Any) -> tuple[int, float]:
        return 50, 130.0

    async def _idfs(ch: Any, **kw: Any) -> list[float]:
        return [1.0] * len(kw["tokens"])

    async def _revisions(org: str, nss: Any, uris: Any, **kw: Any) -> dict[Any, Any]:
        seen["revision_uris"] = list(uris)
        return {("ns", "repo://retention.sql"): (3, "sql")}

    monkeypatch.setattr(h, "get_embedder", lambda: NS(model_name="m", embed=_embed))
    monkeypatch.setattr("server.knowledge.app.search.hybrid_search", _sem)
    monkeypatch.setattr("server.knowledge.app.search.lexical_search", _lex)
    monkeypatch.setattr("server.knowledge.app.search.keys_search", _keyed)
    monkeypatch.setattr(knowledge_bm25, "corpus_stats", _stats)
    monkeypatch.setattr(knowledge_bm25, "idfs_for", _idfs)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    monkeypatch.setattr(documents_repo, "revisions_and_formats", _revisions)
    return seen


def test_a_hybrid_search_fuses_the_keys_leg_and_cites_each_hits_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search as h

    seen = _handler_world(monkeypatch)
    out = asyncio.run(
        h.handle_knowledge_search(
            KnowledgeSearchInput(
                org="o",
                namespace="ns",
                query="where is events created",
                namespace_scope="this",
                formats=["sql"],
                block_types=["sql_statement"],
            )
        )
    )
    assert "events" in seen["keys"]
    assert (seen["query"].formats, seen["query"].block_types) == (["sql"], ["sql_statement"])
    by = {r.source_uri: r for r in out.results}
    assert by["repo://retention.sql"].keys_matched == ["events"]
    assert (by["repo://retention.sql"].revision, by["repo://retention.sql"].format) == (3, "sql")
    assert (by["repo://a.md"].revision, by["repo://a.md"].format) == (0, "")
    assert sorted(seen["revision_uris"]) == ["repo://a.md", "repo://retention.sql"]


def test_a_semantic_search_runs_no_keys_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search as h

    seen = _handler_world(monkeypatch)
    asyncio.run(
        h.handle_knowledge_search(
            KnowledgeSearchInput(
                org="o",
                namespace="ns",
                query="where is events created",
                namespace_scope="this",
                search_mode="semantic",
            )
        )
    )
    assert "keys" not in seen


# ---- located citations ----------------------------------------------------------------------


def test_cited_chunks_expand_ranges_over_what_was_read() -> None:
    from server.memory.domain.corpus_synthesis import cited_chunks

    read = {("a", 0), ("a", 1), ("a", 2), ("b", 4)}
    assert cited_chunks(["a#0-2", "b#4", "a#1", "b#9"], read) == [
        ("a", 0),
        ("a", 1),
        ("a", 2),
        ("b", 4),
    ]
    assert cited_chunks(["a#1; b#4"], read) == [("a", 1), ("b", 4)]


def test_synthesize_names_each_cited_chunks_revision_and_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput
    from server.knowledge.app import synthesize as synth
    from server.knowledge.store import documents as documents_repo
    from server.llm import client as llm_client
    from server.memory.domain.corpus_synthesis import CorpusChunk

    chunks = [
        CorpusChunk(
            "repo://retention.sql", 0, "CREATE TABLE events ...", locator={"line_start": 1}
        ),
        CorpusChunk("repo://retention.sql", 1, "SELECT count() ...", locator={"line_start": 5}),
    ]

    async def _corpus(input: Any) -> tuple[list[CorpusChunk], int]:
        return chunks, 1

    async def _not_over(*a: Any, **k: Any) -> bool:
        return False

    async def _revisions(org: str, nss: Any, uris: Any, **kw: Any) -> dict[Any, Any]:
        return {("ns", "repo://retention.sql"): (2, "sql")}

    class _LLM:
        model = "fake"

        async def complete(self, *a: Any, **k: Any) -> Any:
            return NS(text="Raw events are kept 30 days [repo://retention.sql#0].")

    monkeypatch.setattr(synth, "_corpus_chunks", _corpus)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", _not_over)
    monkeypatch.setattr(documents_repo, "revisions_and_formats", _revisions)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _LLM())
    out = asyncio.run(
        synth.handle_knowledge_synthesize(
            KnowledgeSynthesizeInput(
                org="o", namespace="ns", source_uris=["repo://retention.sql"], task="How long?"
            )
        )
    )
    assert out.citations == ["repo://retention.sql#0"]
    (cited,) = out.cited_chunks
    assert (cited.source_uri, cited.chunk_idx, cited.revision) == ("repo://retention.sql", 0, 2)
    assert cited.locator == {"line_start": 1}
