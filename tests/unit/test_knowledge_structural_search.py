"""Structural knowledge search: the exact-key leg, the format and block-type filters,
the revision, format, locator and block type each hit reads off its chunk, and the
located citations of knowledge.synthesize. Over stubbed statements; the live path is
tests/integration/test_knowledge_structural_search.py."""

from __future__ import annotations

import asyncio
import dataclasses
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
    assert query_keys("Where is orders_daily defined? server.port and db.Events, userId") == [
        "orders_daily",
        "server.port",
        "db.Events",
        "db.events",
        "userId",
        "userid",
    ]
    # A plain word or an acronym is a column as often as the word a question means;
    # alone it is a lookup.
    assert query_keys("what does the status of a refund show") == []
    assert query_keys("which table holds the user ID and its TTL") == []
    # A dash joins the segments of a dotted path, and only those.
    assert query_keys("what is pool.max-size set to") == ["pool.max-size"]
    assert query_keys("a well-known limit") == []
    assert query_keys("sessions") == ["sessions"] and query_keys("Events") == ["Events", "events"]
    assert query_keys("a 1 2 x") == [] and query_keys("") == []
    assert len(query_keys(" ".join(f"term_{i}" for i in range(100)))) == MAX_QUERY_KEYS


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
                    '{"block_type": "sql_statement", "section_path": [], "revision": 2,'
                    ' "format": "sql", "locator": {"line_start": 1, "line_end": 3}}'
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
    assert (hit.revision, hit.format) == (2, "sql")
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
    from server.knowledge.app.search import hybrid_search, lexical_search

    filters: dict[str, Any] = {"formats": ["sql", "toml"], "block_types": ["sql_statement"]}
    lexical: dict[str, Any] = {"tokens": ["events"], "idfs": [1.0], "avgdl": 10.0}
    ch = StubCH()
    await hybrid_search(_q(**filters), ch=ch)
    await lexical_search(_q(**filters, **lexical), ch=ch)
    await keys_search(_q(**filters, keys=["events"]), ch=ch)
    assert len(ch.calls) == 3
    for sql, params in ch.calls:
        assert "JSONExtractString(chunk_metadata, 'format') IN {formats:Array(String)}" in sql
        assert (
            "JSONExtractString(chunk_metadata, 'block_type') IN {block_types:Array(String)}" in sql
        )
        assert (params["formats"], params["block_types"]) == (["sql", "toml"], ["sql_statement"])
    await hybrid_search(_q(), ch=ch)
    assert "JSONExtractString" not in ch.calls[-1][0]


# ---- the handler --------------------------------------------------------------------------


def _handler_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.app import knowledge_search as h

    seen: dict[str, Any] = {}

    async def _embed(text: str) -> list[float]:
        return [0.1] * 4

    async def _sem(q: Any, **kw: Any) -> list[ChunkHit]:
        seen["query"] = q
        return [_hit("repo://a.md", distance=0.1)]

    async def _lex(q: Any, **kw: Any) -> list[ChunkHit]:
        # A lexical hit only with corpus statistics, as the real leg.
        seen["avgdl"] = q.avgdl
        return [_hit("repo://b.md", distance=0.4)] if q.avgdl > 0 else []

    async def _keyed(q: Any, **kw: Any) -> list[ChunkHit]:
        seen["keys"] = list(q.keys)
        hit = _hit("repo://retention.sql", distance=0.6, keys=("events",))
        return [dataclasses.replace(hit, revision=3, format="sql")]

    async def _stats(ch: Any, **kw: Any) -> tuple[int, float]:
        return 50, 130.0

    async def _idfs(ch: Any, **kw: Any) -> list[float]:
        return [1.0] * len(kw["tokens"])

    monkeypatch.setattr(h, "get_embedder", lambda: NS(model_name="m", embed=_embed))
    monkeypatch.setattr("server.knowledge.app.search.hybrid_search", _sem)
    monkeypatch.setattr("server.knowledge.app.search.lexical_search", _lex)
    monkeypatch.setattr("server.knowledge.app.search.keys_search", _keyed)
    monkeypatch.setattr(knowledge_bm25, "corpus_stats", _stats)
    monkeypatch.setattr(knowledge_bm25, "idfs_for", _idfs)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
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
                query="where is orders_daily created",
                namespace_scope="this",
                formats=["sql"],
                block_types=["sql_statement"],
            )
        )
    )
    assert "orders_daily" in seen["keys"]
    assert (seen["query"].formats, seen["query"].block_types) == (["sql"], ["sql_statement"])
    by = {r.source_uri: r for r in out.results}
    assert by["repo://retention.sql"].keys_matched == ["events"]
    assert (by["repo://retention.sql"].revision, by["repo://retention.sql"].format) == (3, "sql")
    assert (by["repo://a.md"].revision, by["repo://a.md"].format) == (0, "")


def test_a_semantic_search_runs_no_keys_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search as h

    seen = _handler_world(monkeypatch)
    asyncio.run(
        h.handle_knowledge_search(
            KnowledgeSearchInput(
                org="o",
                namespace="ns",
                query="where is orders_daily created",
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
    from server.llm import client as llm_client
    from server.memory.domain.corpus_synthesis import CorpusChunk

    chunks = [
        CorpusChunk(
            "repo://retention.sql", 0, "CREATE TABLE ...", revision=2, locator={"line_start": 1}
        ),
        CorpusChunk(
            "repo://retention.sql", 1, "SELECT count() ...", revision=2, locator={"line_start": 5}
        ),
    ]

    async def _corpus(input: Any) -> tuple[list[CorpusChunk], int]:
        return chunks, 1

    async def _not_over(*a: Any, **k: Any) -> bool:
        return False

    class _LLM:
        model = "fake"

        async def complete(self, *a: Any, **k: Any) -> Any:
            return NS(text="Raw events are kept 30 days [repo://retention.sql#0].")

    monkeypatch.setattr(synth, "_corpus_chunks", _corpus)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", _not_over)
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


def test_a_hybrid_query_of_short_identifiers_still_runs_the_keys_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`id` or `db.ts` has no BM25 token (they take three letters); its keys still count."""
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search as h

    seen = _handler_world(monkeypatch)
    out = asyncio.run(
        h.handle_knowledge_search(
            KnowledgeSearchInput(org="o", namespace="ns", query="db.ts", namespace_scope="this")
        )
    )
    assert seen["keys"] == ["db.ts"]
    assert "repo://retention.sql" in {r.source_uri for r in out.results}


@pytest.mark.parametrize(
    "query",
    [
        "db.ts",  # no BM25 token: the vector and exact-key legs
        "where is orders_daily",  # all three legs; the public pass finds no word or key
    ],
)
def test_a_private_passs_exact_key_hit_scores_on_the_public_passs_scale(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Each pass of a private-first search fuses, so a pass whose keys matched and
    one whose legs found nothing but vectors score on one scale: the keyed hit
    stands level with the public pass's first hit, not under all of its raw
    similarities."""
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search as h

    _handler_world(monkeypatch)

    async def _sem(q: Any, **kw: Any) -> list[ChunkHit]:
        # Under the private-first threshold, so the public pass runs too.
        ns = q.namespaces[0]
        return [
            dataclasses.replace(_hit(f"repo://{ns}-{i}.md", distance=0.72), namespace=ns)
            for i in range(3)
        ]

    async def _keyed(q: Any, **kw: Any) -> list[ChunkHit]:
        if "priv" not in q.namespaces:
            return []
        hit = _hit("repo://schema.sql", distance=0.75, keys=tuple(q.keys[:1]))
        return [dataclasses.replace(hit, namespace="priv")]

    async def _lex(q: Any, **kw: Any) -> list[ChunkHit]:
        hit = dataclasses.replace(_hit("repo://schema.sql", distance=0.75), namespace="priv")
        return [hit] if "priv" in q.namespaces else []

    async def _fanout(*a: Any, **kw: Any) -> tuple[list[str], list[str]]:
        return ["priv", "pub"], []

    async def _visibility(org: str, namespaces: list[str]) -> dict[str, str]:
        return {"priv": "private", "pub": "public"}

    monkeypatch.setattr("server.knowledge.app.search.hybrid_search", _sem)
    monkeypatch.setattr("server.knowledge.app.search.keys_search", _keyed)
    monkeypatch.setattr("server.knowledge.app.search.lexical_search", _lex)
    monkeypatch.setattr(h, "namespaces_to_search", _fanout)
    monkeypatch.setattr(h.namespace_visibility, "visibility_map", _visibility)
    out = asyncio.run(
        h.handle_knowledge_search(
            KnowledgeSearchInput(org="o", namespace="priv", query=query, namespace_scope="all_mine")
        )
    )
    order = [r.source_uri for r in out.results]
    assert {r.namespace for r in out.results} == {"priv", "pub"}
    assert order.index("repo://schema.sql") < order.index("repo://pub-1.md"), order


def test_a_chunks_metadata_reads_its_structure() -> None:
    from server.knowledge.domain.chunk_metadata import read_structure

    s = read_structure(
        '{"section_path": ["Ops", "Retention"], "revision": 4, "format": "markdown",'
        ' "block_type": "paragraph", "locator": {"line_start": 9}}'
    )
    assert (s.section_path, s.revision, s.format) == (("Ops", "Retention"), 4, "markdown")
    assert (s.block_type, s.locator) == ("paragraph", {"line_start": 9})
    empty = read_structure("not json")
    assert (empty.section_path, empty.revision, empty.format, empty.locator) == ((), 0, "", None)
    assert read_structure('{"revision": "7"}').revision == 0
    # The outline's text fields, from the same reader; an empty heading is no segment.
    text = read_structure(
        '{"section_path": ["Ops", ""], "section_depth": 2, "section_idx": 3, "char_start": 40}'
    )
    assert (text.section_path, text.section_depth, text.section_idx, text.char_start) == (
        ("Ops",),
        2,
        3,
        40,
    )
    assert read_structure('{"section_depth": true}').section_depth == 0


def test_a_cold_corpus_fuses_the_vector_and_exact_key_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No corpus statistics yet: the lexical leg finds nothing, and a hybrid search
    is the vector leg fused with the exact-key leg."""
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import bm25 as knowledge_bm25
    from server.knowledge.app import knowledge_search as h

    seen = _handler_world(monkeypatch)

    async def _cold(ch: Any, **kw: Any) -> tuple[int, float]:
        return 0, 0.0

    monkeypatch.setattr(knowledge_bm25, "corpus_stats", _cold)
    out = asyncio.run(
        h.handle_knowledge_search(
            KnowledgeSearchInput(
                org="o", namespace="ns", query="where is orders_daily", namespace_scope="this"
            )
        )
    )
    assert "orders_daily" in seen["keys"] and seen["avgdl"] == 0
    by = {r.source_uri: r for r in out.results}
    assert by["repo://retention.sql"].keys_matched == ["events"]
    assert "repo://a.md" in by and "repo://b.md" not in by
