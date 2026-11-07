"""Unit tests for code-native schema facets.

Covers four requirements, all CH-free:
  1. files[] / concepts[] extracted from a synthetic coding fact.
  2. de-atomization guard — facet extraction never mutates the fact value.
  3. mirror-every-column — the new arrays are present on the loom.memory +
     loom.memory_embeddings insert dicts and survive build_*_row.
  4. graph projection emits a typed fixes/file (subject -> code-node) edge.

These run in milliseconds and need no LOOM_BASE_URL / CH container — the
facet parser is pure, the write paths take a stub CH client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
from server.memory.app.extract import ExtractedFact, extract_code_facets
from server.memory.app.extract.enrich import _apply_code_facets
from server.memory.domain.graph_quality import (
    CODE_EDGE_RELATIONS,
    classify_code_concept,
    code_facet_relation,
)
from server.memory.store import (
    EMBEDDING_PERSISTED_COLS,
    MEMORY_PERSISTED_COLS,
    MemoryRow,
    build_embedding_row,
    build_memory_version_row,
)

_SYNTHETIC = "fixed NPE in src/auth/login.py:42 by null-checking session"
_RICH = (
    "Resolved a NullPointerException in src/auth/login.py:42 by null-checking "
    "the session; the bug came from psycopg2.connect. See commit a1b2c3d."
)


# ----------------------------------------------------------------------
# 1. Deterministic facet extraction
# ----------------------------------------------------------------------


def test_extract_files_from_synthetic_coding_fact() -> None:
    files, _concepts = extract_code_facets(_SYNTHETIC)
    # The path is captured WITHOUT its :line suffix (line-agnostic).
    assert files == ["src/auth/login.py"]


def test_extract_files_and_concepts_from_rich_fact() -> None:
    files, concepts = extract_code_facets(_RICH)
    assert "src/auth/login.py" in files
    # Error token + dotted symbol + commit SHA all surface as concepts.
    assert "NullPointerException" in concepts
    assert "psycopg2.connect" in concepts
    assert "commit:a1b2c3d" in concepts


def test_non_code_prose_yields_no_facets() -> None:
    # A durable preference fact has zero code content — empty arrays.
    assert extract_code_facets("User loves Italian food, especially carbonara.") == ([], [])


def test_extensionless_conventional_filenames() -> None:
    files, _ = extract_code_facets("Edited the Dockerfile and Makefile to fix the build.")
    assert "Dockerfile" in files
    assert "Makefile" in files


def test_prose_slash_does_not_become_a_file() -> None:
    # "and/or" must not be mistaken for a path — the closed-class extension
    # gate prevents it.
    files, _ = extract_code_facets("the deploy is gated and/or blocked on review")
    assert files == []


def test_commit_sha_requires_a_commit_cue() -> None:
    # A bare hex run with no commit/sha cue is NOT a commit concept.
    _, concepts = extract_code_facets("the order id was deadbeef on the invoice")
    assert not any(c.startswith("commit:") for c in concepts)


def test_facets_capped_per_array() -> None:
    many = " ".join(f"pkg/mod{i}.py" for i in range(40))
    files, _ = extract_code_facets(many)
    assert len(files) <= 12


# ----------------------------------------------------------------------
# 2. De-atomization guard
# ----------------------------------------------------------------------


def test_apply_code_facets_never_mutates_fact_value() -> None:
    fact = ExtractedFact(memory_key="fix_login_npe", value=_SYNTHETIC)
    out = _apply_code_facets([fact], [], from_turns=True)
    assert len(out) == 1
    # The atomic value is byte-identical — facets are an additive side-channel.
    assert out[0].value == _SYNTHETIC
    assert out[0].memory_key == fact.memory_key
    assert out[0].files == ["src/auth/login.py"]


def test_apply_code_facets_pulls_from_cited_source_messages() -> None:
    # A path paraphrased OUT of the fact value but present in a cited source
    # message still gets linked via source_message_indices.
    fact = ExtractedFact(
        memory_key="fixed_login",
        value="Fixed the login bug by null-checking the session.",
        source_message_indices=[0],
    )
    messages = [{"role": "user", "content": "the crash was in src/auth/login.py"}]
    out = _apply_code_facets([fact], messages, from_turns=True)
    assert out[0].files == ["src/auth/login.py"]
    # Value still untouched.
    assert out[0].value == "Fixed the login bug by null-checking the session."
    # Under an operator policy only the value counts: the cited turn's path is
    # what the model may have left out because of the policy.
    ruled = _apply_code_facets([fact], messages, from_turns=False)
    assert ruled[0].files == []


def test_apply_code_facets_noop_on_non_code_facts() -> None:
    facts = [ExtractedFact(memory_key="food_pref", value="User loves carbonara.")]
    out = _apply_code_facets(facts, [], from_turns=True)
    # No code content → returns the SAME list object (no allocation), and the
    # fact carries empty facet arrays.
    assert out is facts
    assert out[0].files == []
    assert out[0].concepts == []


# ----------------------------------------------------------------------
# 3. Mirror-every-column
# ----------------------------------------------------------------------


def test_files_concepts_in_persisted_column_lists() -> None:
    assert "files" in MEMORY_PERSISTED_COLS
    assert "concepts" in MEMORY_PERSISTED_COLS
    assert "files" in EMBEDDING_PERSISTED_COLS
    assert "concepts" in EMBEDDING_PERSISTED_COLS


def test_build_memory_version_row_carries_facets_forward() -> None:
    existing = MemoryRow(
        memory_id="11111111-2222-3333-4444-555555555555",
        org="dev",
        namespace="ns",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="k",
        memory_type="semantic",
        value="x",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=__import__("datetime").datetime(2026, 1, 1),
        expires_at=None,
        files=["src/auth/login.py"],
        concepts=["NullPointerException"],
    )
    # A follow-up version row (e.g. a touch / restore) MUST mirror the arrays.
    row = build_memory_version_row(existing, version=2)
    assert row["files"] == ["src/auth/login.py"]
    assert row["concepts"] == ["NullPointerException"]
    # And an empty default is preserved as a list, not dropped.
    empty = existing.model_copy(update={"files": [], "concepts": []})
    row2 = build_memory_version_row(empty, version=3)
    assert row2["files"] == []
    assert row2["concepts"] == []


def test_build_embedding_row_defaults_and_overrides_facets() -> None:
    # Default: empty arrays (mirrors the schema DEFAULT []).
    default_row = build_embedding_row(org="dev", namespace="ns")
    assert default_row["files"] == []
    assert default_row["concepts"] == []
    # Override: facets land on the embedding row.
    row = build_embedding_row(
        org="dev",
        namespace="ns",
        files=["src/auth/login.py"],
        concepts=["psycopg2.connect"],
    )
    assert row["files"] == ["src/auth/login.py"]
    assert row["concepts"] == ["psycopg2.connect"]


def test_memory_row_facets_default_empty() -> None:
    # A row hydrated without facets reads as empty arrays.
    row = MemoryRow(
        memory_id="11111111-2222-3333-4444-555555555555",
        org="dev",
        namespace="ns",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="k",
        memory_type="semantic",
        value="x",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=__import__("datetime").datetime(2026, 1, 1),
        expires_at=None,
    )
    assert row.files == []
    assert row.concepts == []


# ----------------------------------------------------------------------
# 4. Graph projection — typed code-facet edges on the existing tables
# ----------------------------------------------------------------------


class _StubCH:
    """Captures inserts; query_all returns nothing (repair leg no-op)."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        return []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows, settings or {}))


def _bulk_input(**entry_kwargs) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="dev",
        namespace="ns",
        agent="user",
        user_id="alice",
        session_id="s1",
        entries=[_BulkSetEntry(memory_key="fix_login", value="v", **entry_kwargs)],
    )


async def _written(table_fn, inp) -> dict[str, list[dict[str, Any]]]:
    ch = _StubCH()
    await table_fn(inp, [_BulkSetResult(memory_key="fix_login", version=0)], ch=ch, entity_map={})
    return {t: rows for t, rows, _ in ch.inserts}


async def test_graph_emits_file_and_error_triples() -> None:
    from server.memory.app.projections import write_entity_triples

    inp = _bulk_input(
        files=["src/auth/login.py"],
        concepts=["NullPointerException"],
        relations=[("user", "fixed", "login bug")],
    )
    by_table = await _written(write_entity_triples, inp)
    triples = by_table.get("loom.entity_triples", [])
    rels = {(t["subject"], t["relation"], t["object"]) for t in triples}
    # File facet → a 'modifies' edge from the actor to the file node.
    assert ("user", "modifies", "src/auth/login.py") in rels
    # Error facet, with a fact verb that reads as a fix → a 'fixes' edge.
    assert ("user", "fixes", "nullpointerexception") in rels
    # Every emitted relation is a recognized code (or LLM) relation; the two
    # code edges use the closed-class code verbs.
    assert {"modifies", "fixes"} <= {t["relation"] for t in triples}


async def test_graph_emits_facet_triples_even_without_llm_relations() -> None:
    # An entry with facets but NO LLM relations still projects code edges —
    # the early `if not rels: continue` must not skip facet-only entries.
    from server.memory.app.projections import write_entity_triples

    inp = _bulk_input(files=["server/main.py"], concepts=[])
    by_table = await _written(write_entity_triples, inp)
    triples = by_table.get("loom.entity_triples", [])
    rels = {(t["subject"], t["relation"], t["object"]) for t in triples}
    assert ("user", "modifies", "server/main.py") in rels


async def test_graph_edges_include_facet_nodes() -> None:
    # File + concept facet nodes join the co-occurrence edge plane.
    from server.memory.app.projections import write_entity_edges

    inp = _bulk_input(files=["src/auth/login.py"], concepts=["NullPointerException"])
    by_table = await _written(write_entity_edges, inp)
    edges = by_table.get("loom.entity_edges", [])
    nodes = {e["entity_a"] for e in edges} | {e["entity_b"] for e in edges}
    assert "src/auth/login.py" in nodes
    assert "nullpointerexception" in nodes


def test_embedding_worker_carry_signals_includes_facets() -> None:
    # The re-embed path rebuilds the stub via build_embedding_row; without
    # carrying facets forward, FINAL resurrects the schema default [].
    from server.memory.workers.embedding import _carry_signals

    stub_row = {
        "org": "dev",
        "namespace": "ns",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": "k",
        "memory_type": "semantic",
        "scope": "private",
        "files": ["server/main.py"],
        "concepts": ["KeyError"],
    }
    carried = _carry_signals(stub_row)
    assert carried["files"] == ["server/main.py"]
    assert carried["concepts"] == ["KeyError"]
    # And build_embedding_row accepts them without raising on an unknown col.
    row = build_embedding_row(**carried)
    assert row["files"] == ["server/main.py"]
    assert row["concepts"] == ["KeyError"]


def test_classify_and_relation_helpers() -> None:
    assert classify_code_concept("NullPointerException") == "error"
    assert classify_code_concept("psycopg2.connect") == "function"
    assert classify_code_concept("numpy") == "library"
    assert classify_code_concept("commit:abc1234") == "commit"
    assert code_facet_relation("file", []) == "modifies"
    assert code_facet_relation("error", ["fixed"]) == "fixes"
    assert code_facet_relation("error", ["saw"]) == "causes"
    # The closed-class code relations are all present.
    assert {"imports", "modifies", "fixes", "causes", "blocked_by"} <= CODE_EDGE_RELATIONS


def test_a_facet_too_long_to_be_a_name_mints_no_node() -> None:
    from server.memory.domain.graph_quality import MAX_ENTITY_CHARS, facet_triples

    for huge in ("x" * (MAX_ENTITY_CHARS + 1), "\ufb01" * (MAX_ENTITY_CHARS // 2 + 1)):
        triples = list(facet_triples("bot", [], [huge], [huge, "src/app.py"], [huge, "billing"]))
        assert triples and all(len(t[2]) <= MAX_ENTITY_CHARS for t in triples)
        assert [t[2] for t in triples] == ["src/app.py", "billing"]


def _edge_nodes(**facets: list[str]) -> list[str]:
    from server.memory.app.projections import edge_nodes

    empty = {"entities": [], "files": [], "concepts": [], "topics": []}
    return edge_nodes(SimpleNamespace(**{**empty, **facets}), {})


def test_edge_nodes_are_the_entities_and_the_facet_nodes() -> None:
    nodes = _edge_nodes(
        entities=["alice"],
        files=["src/app.py"],
        concepts=["KeyError", "commit:ABC1234"],
        topics=["the billing"],
    )
    assert nodes == ["alice", "billing", "commit:abc1234", "keyerror", "src/app.py"]


def test_a_commit_name_is_stored_case_folded_whole_at_the_cap() -> None:
    from server.memory.domain.graph_quality import MAX_ENTITY_CHARS, facet_triples

    # 256 as written; .lower() would make it 505 ("İ" is "i" and a dot above).
    raw = "commit:" + "İ" * (MAX_ENTITY_CHARS - len("commit:"))
    node = "commit:" + "i" * (MAX_ENTITY_CHARS - len("commit:"))
    assert [t[2] for t in facet_triples("bot", [], [], [raw], [])] == [node]
    assert _edge_nodes(concepts=[raw]) == [node]
    # A ligature folds too.
    assert [t[2] for t in facet_triples("bot", [], [], ["error:ﬁle"], [])] == ["error:file"]


def test_a_commit_name_that_folds_past_the_cap_mints_no_node() -> None:
    from server.memory.domain.graph_quality import MAX_ENTITY_CHARS, facet_triples

    # 256 as written, 2 * 249 + 7 once folded ("ﬁ" is "fi").
    folds_long = "commit:" + "ﬁ" * (MAX_ENTITY_CHARS - len("commit:"))
    too_long = "error:" + "x" * MAX_ENTITY_CHARS
    for raw in (folds_long, too_long):
        assert list(facet_triples("bot", [], [raw], [raw], [raw])) == []
        assert _edge_nodes(files=[raw], concepts=[raw], topics=[raw]) == []


@pytest.mark.asyncio
async def test_an_error_topic_is_one_node_to_the_triples_the_edges_and_the_graph_seed() -> None:
    from server.memory.app.retrieval import fetch_graph_neighbors
    from server.memory.domain.graph_quality import facet_triples

    seen: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            seen.append(params or {})
            return []

    for facet in ("files", "concepts", "topics"):
        lists: dict[str, list[str]] = {"files": [], "concepts": [], "topics": []}
        lists[facet] = ["error:Timeouts"]
        triples = facet_triples("bot", [], lists["files"], lists["concepts"], lists["topics"])
        assert [t[2] for t in triples] == ["error:timeouts"]
        assert _edge_nodes(**{facet: ["error:Timeouts"]}) == ["error:timeouts"]
    await fetch_graph_neighbors(
        _CH(), org="o", namespace="n", seed_entities=["error:Timeouts"], hub_skip_degree=0
    )
    assert seen and seen[0]["seeds"] == ["error:timeouts"]


@pytest.mark.asyncio
async def test_an_error_name_in_entities_relations_and_a_facet_is_one_node() -> None:
    from server.memory.app.projections import write_entity_edges, write_entity_triples

    inp = _bulk_input(
        entities=["deploy", "error:Timeouts"],
        relations=[("deploy", "causes", "error:Timeouts")],
        topics=["error:Timeouts"],
    )
    edges = (await _written(write_entity_edges, inp))["loom.entity_edges"]
    assert {(e["entity_a"], e["entity_b"]) for e in edges} == {("deploy", "error:timeouts")}
    triples = (await _written(write_entity_triples, inp))["loom.entity_triples"]
    assert {t["object"] for t in triples} == {"error:timeouts"}


@pytest.mark.asyncio
async def test_memory_search_scope_seeds_an_error_name_at_its_edge_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search import scope

    monkeypatch.setattr(scope, "get_client", lambda: _StubCH())
    done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    done.set_result(None)
    run = scope._SearchRun(
        rerank_override="", rerank_cfg_task=None, calibration_task=done, fanout_task=None
    )
    body = MemorySearchInput(
        org="o", namespace="n", agent="a", query="q", entity_filter=["error:Timeouts"]
    )
    await scope._resolve_entity_scope(body, run, [], {})
    assert run.effective_entity_filter == _edge_nodes(topics=["error:Timeouts"])


@pytest.mark.asyncio
async def test_a_query_triple_names_an_error_facet_as_it_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.llm import client as llm_client
    from server.llm.client import LLMResult
    from server.memory.app import search_llm
    from server.memory.domain.graph_quality import facet_triples

    class _LLM:
        async def complete(self, system: str, user: str, **kw: Any) -> LLMResult:
            text = '[{"s": "deploy", "v": "causes", "o": "error:Timeouts"}]'
            return LLMResult(text=text, tokens_in=1, tokens_out=1, provider="fake", model="m")

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _LLM())
    monkeypatch.setattr(search_llm, "_QUERY_ENTITY_CACHE", {})
    (triple,) = await search_llm.extract_query_triples_llm("what causes error:Timeouts")
    (stored,) = facet_triples("bot", [], [], ["error:Timeouts"], [])
    assert triple[2] == stored[2] == "error:timeouts"


def test_a_prose_name_that_starts_with_error_is_normalised_as_prose() -> None:
    from server.memory.domain.graph_quality import canonical_entity_text

    assert canonical_entity_text("Error: the Thing's") == "error: the thing"
    assert canonical_entity_text("error:`Timeouts`") == "error: timeout"  # markup is a blank
    assert canonical_entity_text("error:Timeouts") == "error:timeouts"


def test_a_wrapped_or_spaced_code_facet_name_is_the_bare_names_node() -> None:
    from server.memory.domain.graph_quality import canonical_entity, canonical_entity_text

    for bare in ("error:Timeouts", "error:HTTPError(404)", "error:can't connect", "commit:ABC123"):
        node = canonical_entity_text(bare)
        for written in (
            f"`{bare}`",
            f"**{bare}**",
            f"(({bare}))",
            f'"{bare}',
            f"the [{bare}].",
            f"{bare},",
            f" {bare} ",
        ):
            assert canonical_entity_text(written) == node, written
    assert canonical_entity("error:a \t b").entity_id == "error:a_b"


def test_a_facet_of_only_punctuation_mints_no_node() -> None:
    from server.memory.domain.graph_quality import facet_triples

    assert list(facet_triples("a", [], ["``"], ["``", "."], ["``"])) == []
