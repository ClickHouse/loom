"""Every statement the chunk leg builds passes the ClickHouse analyzer.

A join that exposes a `session_id` column next to a bare `session_id`
predicate fails with code 207 (ambiguous identifier), and the neighbor-window
statement then falls open to "no neighbors". Unit tests cannot see that
because none of them hands the built SQL to a real server. This test
captures the three chunk statements (candidates, FINAL re-read, neighbor
window) through the stub client and runs each through `EXPLAIN QUERY TREE`
against the CI ClickHouse — analysis only, no rows read, so the empty CI
tables are enough. A future join or alias that shadows a bare identifier
fails here, not on staging.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.unit.schema_ddl import view_select  # noqa: E402
from tests.unit.test_chunk_two_stage import _stage2_row  # noqa: E402
from tests.unit.test_memory_search_service import StubCH, _q  # noqa: E402


def _cands_one_trip(*rows: tuple[str, int, float]) -> list[dict[str, Any]]:
    return [
        {"session_id": s, "chunk_index": i, "distance": d, "parent_memory_key": f"p_{s}"}
        for s, i, d in rows
    ]


async def _capture_chunk_statements() -> list[tuple[str, dict[str, Any]]]:
    """Every statement shape the chunk leg can build:

    - stage 1 (candidates) and the one-trip stage 2 with the window branch
      and the keyed date joins (a query that carries words);
    - the one-trip stage 2 without the window branch (a query without words);
    - the fallback window statement (an anchor the prefetch did not cover).
    """
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    calls: list[tuple[str, dict[str, Any]]] = []
    # one trip, with windows
    ch = StubCH()
    ch.query_all_returns.append(_cands_one_trip(("s1", 3, 0.1), ("s2", 0, 0.2)))
    ch.query_all_returns.append([_stage2_row("s1", 3), _stage2_row("s2", 0)])
    q = _q(entity_filter=["nate"], query_text="binoculars trip", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    assert len(ch.query_all_calls) == 2, "candidates, one-trip re-read"
    calls += ch.query_all_calls
    # one trip, no words → no window branch
    ch = StubCH()
    ch.query_all_returns.append(_cands_one_trip(("s1", 3, 0.1)))
    ch.query_all_returns.append([_stage2_row("s1", 3)])
    q = _q(entity_filter=[], query_text="42", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    assert len(ch.query_all_calls) == 2
    calls.append(ch.query_all_calls[1])
    # fallback window statement for an anchor outside the prefetch
    ch = StubCH()
    ch.query_all_returns.append(_cands_one_trip(("s1", 3, 0.1)))
    ch.query_all_returns.append([dict(_stage2_row("s9", 9), is_neighbor=0)])
    ch.query_all_returns.append([])
    q = _q(entity_filter=[], query_text="binoculars trip", top_k=5)
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    assert len(ch.query_all_calls) == 3
    calls.append(ch.query_all_calls[2])
    return calls


@pytest.mark.asyncio
async def test_every_chunk_statement_passes_the_analyzer(ch_client) -> None:
    statements = await _capture_chunk_statements()
    assert len(statements) == 4
    for i, (sql, params) in enumerate(statements):
        clean = re.sub(
            r"SETTINGS use_skip_indexes = 0(, optimize_move_to_prewhere_if_final = 1)?", "", sql
        )
        assert "PREWHERE" not in clean  # plain WHERE under FINAL; the key-only move is a setting
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {clean}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"chunk statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{clean}"
            ) from exc


@pytest.mark.asyncio
async def test_edge_hop_statement_with_per_match_cap_passes_the_analyzer(ch_client) -> None:
    """`LIMIT k BY from_symbol_id` between ORDER BY and the flat
    LIMIT is analyzer-checked here with the real parameter types."""
    from server.code.store.index import CodeRepo

    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.edges_for_symbols(
        "o", "n", ["s1", "s2"], direction="callees", limit=500, per_match=12
    )
    await repo.edges_for_symbols("o", "n", ["s1"], direction="callers", limit=1000)
    # The graph leg's per-anchor read (cap + confidence floor),
    # the statement the by_from projection serves in the callee direction.
    await repo.edges_for_symbols(
        "o",
        "n",
        ["s1", "s2"],
        direction="callees",
        limit=200,
        min_confidence=0.7,
        per_match=10,
    )
    for sql, params in ch.query_all_calls:
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"edge statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


class _StubCHWithOne(StubCH):
    """StubCH plus query_one, recorded in the same call list."""

    async def query_one(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return {"n": 0}


@pytest.mark.asyncio
async def test_constrained_search_statements_pass_the_analyzer(ch_client) -> None:
    """The pattern / glob / extension predicates sit in the
    plain WHERE of the lexical leg, the vector leg, the pre-count and both
    grep shapes. Captured through the stub with the real parameter types
    and analyzer-checked here; the vector leg's SETTINGS fragment is
    dropped for CH builds that do not know the codec settings."""
    from server.code.domain.search_scope import build_scope
    from server.code.store.index import CodeRepo

    ch = _StubCHWithOne()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    scope = build_scope(
        pattern="parseQuotaKey",
        path_globs=["src/**", "*.{h,cpp}"],
        exclude_globs=["**/tests/*"],
        langs=["cpp", "python"],
    )
    regex_scope = build_scope(pattern="parse\\w+Key", regex=True, case_sensitive=True)
    await repo.lexical_search_chunks(
        "o", "n", "m", ["quota"], [1.5], path_prefix="src/", scope=scope
    )
    await repo.vector_search_chunks("o", "n", "m", [0.1, 0.2, 0.3], scope=scope)
    await repo.vector_search_chunks("o", "n", "m", [0.1, 0.2, 0.3], scope=regex_scope)
    await repo.scope_file_count("o", "n", "m", scope, path_prefix="src/")
    await repo.grep_chunks("o", "n", "needle", scope=scope)
    await repo.grep_files("o", "n", "needle", regex=True, scope=scope)
    # the same scans under a request deadline: break-on-timeout settings
    await repo.grep_chunks("o", "n", "needle", scope=scope, max_execution_s=1.25)
    await repo.grep_files("o", "n", "needle", scope=scope, max_execution_s=0.5)
    await repo.grep_message_chunks("o", "n", ["cannot convert"], scope=scope, max_execution_s=2.0)
    assert len(ch.query_all_calls) == 9
    for i, (sql, params) in enumerate(ch.query_all_calls):
        clean = sql.split("SETTINGS")[0] if "cosineDistance" in sql else sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {clean}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"constrained statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{clean}"
            ) from exc


@pytest.mark.asyncio
async def test_lexical_leg_statement_passes_the_analyzer(ch_client) -> None:
    """The lexical leg's statement, with and without a path prefix."""
    from server.code.store.index import CodeRepo

    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    tokens, idfs = ["merge", "tree", "select", "parts"], [2.5, 0.7, 1.1, 3.0]
    await repo.lexical_search_chunks("o", "n", "m", tokens, idfs)
    await repo.lexical_search_chunks("o", "n", "m", tokens, idfs, path_prefix="src/")
    assert len(ch.query_all_calls) == 2
    for sql, params in ch.query_all_calls:
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"lexical statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_track_path_statement_passes_the_analyzer(ch_client) -> None:
    """The track_path read scores one file's chunks with the
    lexical leg's expression and a masked cosine (arrayResize pads a missing
    vector so cosineDistance never throws). Both shapes — with and without a
    query vector — are analyzer-checked with the real parameter types."""
    from server.code.store.index import CodeRepo

    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.paths_under_prefix("o", "n", "m", "src/Access/QuotaCache")
    await repo.track_path_chunks(
        "o", "n", "m", ["src/a.cpp", "src/a.h"], ["retry", "upload"], [1.5, 2.0], [0.1, 0.2]
    )
    await repo.track_path_chunks("o", "n", "m", ["src/a.cpp"], [], [], None)
    assert len(ch.query_all_calls) == 3
    for sql, params in ch.query_all_calls:
        assert "PREWHERE" not in sql.upper(), "the code plane filters in plain WHERE"
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"track_path statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_history_intent_statements_pass_the_analyzer(ch_client) -> None:
    """The PR-plane reads behind the history section — the
    issue-link read and prs_by_number with its two new aggregates."""
    from server.code.store.index import CodeRepo

    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.prs_linking_issue("o", "n", [117585, 114783], limit=6)
    await repo.prs_by_number("o", "n", [114783])
    await repo.items_by_number("o", "n", [117585, 114783])
    assert len(ch.query_all_calls) == 3
    for sql, params in ch.query_all_calls:
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"history statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_second_model_vector_statements_pass_the_analyzer(ch_client) -> None:
    """The shortlist read over a label's own table (here the
    served table — the map is empty in CI) and the keyed exact rescore over
    the shortlisted `(path, chunk_id)` keys, both plain WHERE."""
    from server.code.store.index import CodeRepo

    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.vector_shortlist_ids("o", "n", "static:m", [0.1] * 256, n=1000, path_prefix="src/")
    await repo.exact_rerank_by_ids(
        "o",
        "n",
        "text-embedding-3-large",
        [0.1] * 3072,
        [("src/a.cpp", "00000000-0000-0000-0000-000000000001")],
        pool=200,
    )
    assert len(ch.query_all_calls) == 3  # shortlist + the exact stage's two reads
    for sql, params in ch.query_all_calls:
        assert "PREWHERE" not in sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"second-model statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_code_plane_liveness_reads_pass_the_analyzer(ch_client) -> None:
    """The code plane's live-row reads decide liveness
    with `argMax(tombstoned_at IS NULL, version) = 1` — the plain
    `argMax(tombstoned_at, version) IS NULL` skips NULL arguments and reads
    a key that was tombstoned and later re-inserted as dead (the ingest
    key-diff then never retires it). Every rewritten statement is captured
    through the stub with its real parameter types and analyzer-checked."""
    from server.code.app.index.packs import _callers_of
    from server.code.store.index import CodeRepo

    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.live_edges_for_path("o", "n", "src/a.cpp", "treesitter")
    await repo.live_call_misses_for_path("o", "n", "src/a.cpp")
    await repo.live_call_misses_for_names("o", "n", ["foo", "bar"], limit=100)
    await repo.live_edges_to_symbols("o", "n", ["s1"], edge_kinds=["calls"], max_confidence=0.65)
    await repo.live_edges_to_symbols("o", "n", ["s1"], edge_kinds=["includes"])
    await repo.edges_for_symbols("o", "n", ["s1"], direction="callers", limit=10)
    await repo.include_edges("o", "n")
    await repo.symbol_fan_in("o", "n")
    await repo.symbol_definition_sites("o", "n")
    await _callers_of(repo, "o", "n", [{"symbol_id": "s1"}])
    assert len(ch.query_all_calls) >= 10
    for i, (sql, params) in enumerate(ch.query_all_calls):
        assert "argMax(tombstoned_at, version)" not in sql, f"statement #{i} skips NULL"
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"liveness statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_name_candidates_side_table_statements_pass_the_analyzer(ch_client) -> None:
    """`name_candidates` reads the widened `code_symbol_names` alone: one
    GROUP BY with argMax by version and the tombstone liveness form, captured
    through the stub with its real parameter types and analyzer-checked
    against the migrated CI schema."""
    from server.code.store.index import CodeRepo

    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.name_candidates("o", "n", ["foo", "Bar::baz"])
    assert len(ch.query_all_calls) == 1, "one read"
    for i, (sql, params) in enumerate(ch.query_all_calls):
        assert "PREWHERE" not in sql
        assert "argMax(tombstoned_at, version)" not in sql, f"statement #{i} skips NULL"
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"name_candidates statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc
    read = ch.query_all_calls[0][0]
    assert "FROM loom.code_symbol_names" in read and "FINAL" not in read
    assert "argMax(kind, version) AS kind" in read
    assert "argMax(tombstoned_at IS NULL, version) AS is_live" in read


@pytest.mark.asyncio
async def test_bulk_get_and_include_tags_statements_pass_the_analyzer(ch_client) -> None:
    """`get_many_by_keys` under the wildcard agent opens the
    sentinel user and session axes and takes the newest row per key with
    `ORDER BY … LIMIT 1 BY memory_key` under FINAL; the include_tags fill
    point-reads `any(tags)` over the served keys. Both shapes are checked
    here with their real parameter types."""
    from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
    from server.memory.app import search_tag_filter as tag_mod
    from server.memory.app.search import payload as mod
    from server.memory.store import Identity, get_many_by_keys

    ch = StubCH()
    for ident in (
        Identity(org="o", namespace="n", agent="*", user_id="-", session_id="-"),
        Identity(org="o", namespace="n", agent="*", user_id="alice", session_id="-"),
        Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-"),
    ):
        await get_many_by_keys(ident, ["k1", "k2"], ch=ch, scope=WHOLE_NAMESPACE)
    saved = tag_mod.get_client
    tag_mod.get_client = lambda: ch  # type: ignore[assignment]
    try:
        hit = _MemorySearchHit(
            memory_key="k1",
            memory_type="semantic",
            content_excerpt="x",
            distance=0.1,
            similarity=0.9,
        )
        await mod._fill_hit_tags(
            [hit], MemorySearchInput(org="o", namespace="n", query="q", include_tags=True), ["n"]
        )
    finally:
        tag_mod.get_client = saved  # type: ignore[assignment]
    assert any("LIMIT 1 BY memory_key" in sql for sql, _ in ch.query_all_calls)
    assert any("any(tags) AS tags" in sql for sql, _ in ch.query_all_calls)
    for sql, params in ch.query_all_calls:
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_llm_response_cache_statements_pass_the_analyzer_and_round_trip(ch_client) -> None:
    """The exact-prompt response cache's point read is
    analyzer-checked with its real parameter types, and its write is run for
    real against the migrated table and read back through the same SELECT —
    the two statements the wrapper issues."""
    from server.llm.cache import CachedLLMClient, cache_key, read_sql
    from server.llm.client import LLMResult

    class _Stub(StubCH):
        async def query_one(self, sql, params=None):
            self.query_all_calls.append((sql, params or {}))
            return None

    class _Inner:
        provider = "fake"
        model = "analyzer-model"

        async def complete(self, system, user, max_tokens=1024, response_format=None, **kw):
            return LLMResult(text="", tokens_in=0, tokens_out=0, provider="fake", model=self.model)

    stub = _Stub()
    await CachedLLMClient(_Inner(), org="o", ch=stub).complete("sys", "user", max_tokens=600)
    assert len(stub.query_all_calls) == 1
    sql, params = stub.query_all_calls[0]
    assert sql == read_sql()
    try:
        ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
    except Exception as exc:  # pragma: no cover - the assertion message is the point
        raise AssertionError(
            f"cache read rejected by the analyzer: {str(exc)[:300]}\n{sql}"
        ) from exc

    key = cache_key("sys", f"user {id(stub)}", 600)
    ch_client.insert(
        "loom.llm_response_cache",
        [["analyzer-model", key, "o", "n", "ask_synthesize", "answer [k1]", 12, 3]],
        column_names=[
            "model",
            "key",
            "org",
            "namespace",
            "stage",
            "text",
            "tokens_in",
            "tokens_out",
        ],
    )
    res = ch_client.query(read_sql(), parameters={"model": "analyzer-model", "key": key})
    assert res.result_rows and res.result_rows[0][0] == "answer [k1]"
    assert tuple(res.result_rows[0][1:]) == (12, 3)


class _RecordingCH:
    """Async stub for the erasure repo: records every statement, counts 0."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return {"n": 0}

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return []

    async def execute(self, sql, params=None):  # pragma: no cover - counts are 0
        self.calls.append((sql, params or {}))


async def test_erasure_census_and_orphan_statements_pass_the_analyzer(ch_client) -> None:
    """The census over a namespace list (entity_vectors included),
    the entity_vectors delete's count in both scopes, the failed-saga listing,
    the orphan-graph listing and the two-gauge read — every SELECT the census
    tail, the re-verify CLI and the state probe issue."""
    from server.erasure.store import census as erasure_census
    from server.erasure.store import health as erasure_health
    from server.erasure.store import ledger as erasure_ledger
    from server.erasure.store import steps_graph as graph_steps

    ch = _RecordingCH()
    await erasure_census.residue_census("o", "namespace", ["a", "b"], ch=ch)  # type: ignore[arg-type]
    await erasure_census.residue_census("o", "user_id", "alice", ch=ch)  # type: ignore[arg-type]
    await graph_steps.delete_entity_vectors_by_namespace("o", ["a", "b"], ch=ch)  # type: ignore[arg-type]
    await graph_steps.delete_entity_vectors_by_user("o", "alice", ch=ch)  # type: ignore[arg-type]
    await erasure_ledger.list_failed_requests(org="o", ch=ch)  # type: ignore[arg-type]
    await erasure_ledger.list_failed_requests(ch=ch)  # type: ignore[arg-type]
    # The stuck-saga scan carries `residue` so the worker re-opens its owners.
    await erasure_ledger.list_stuck_in_progress(
        stale_threshold_seconds=300, max_attempts=5, batch_size=10, ch=ch
    )  # type: ignore[arg-type]
    await erasure_health.orphan_graph_namespaces(org="o", ch=ch)  # type: ignore[arg-type]
    await erasure_health.integrity_counts(ch=ch)  # type: ignore[arg-type]
    assert any(
        "loom.entity_vectors" in sql and "namespace IN {ns:Array(String)}" in sql
        for sql, _ in ch.calls
    )
    assert len(ch.calls) >= 22 + 18 + 7
    for i, (sql, params) in enumerate(ch.calls):
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"erasure statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


def test_state_probe_invariants_statement_passes_the_analyzer(ch_client, monkeypatch) -> None:
    """The slow-clock probe statement with its two invariant subqueries, with the
    parameters the shared probe client binds."""
    from server.observability import otel_exporters, otel_probes
    from server.observability.otel_probe_client import _probe_client_reset

    captured: list[tuple[str, dict[str, Any]]] = []

    class _Rows:
        result_rows: list[Any] = []

    class _Sync:
        def query(self, sql: str, **kwargs: Any) -> _Rows:
            captured.append((sql, dict(kwargs.get("parameters") or {})))
            return _Rows()

        def close(self) -> None:
            pass

    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: _Sync())
    try:
        assert otel_probes._probe_invariants() == {}
    finally:
        _probe_client_reset()
    assert len(captured) == 1 and "AS orphan_graph_namespaces" in captured[0][0]
    sql, params = captured[0]
    try:
        ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
    except Exception as exc:  # pragma: no cover - the assertion message is the point
        raise AssertionError(f"probe statement rejected by the analyzer: {str(exc)[:300]}") from exc


async def test_namespace_census_statements_pass_the_analyzer(ch_client) -> None:
    """The registry census, the bench-gc
    candidate read and the first-write existence read, each with its real
    parameter types."""
    from server.identity.app import namespace_autoregister
    from server.identity.store import namespace_census

    ch = _StubCHWithOne()
    await namespace_census.unregistered_live(org="o", ch=ch)
    await namespace_census.bench_gc_candidates(
        idle_hours=24, org="o", namespaces=("lme-a", "mb-b"), limit=200, ch=ch
    )
    namespace_autoregister.forget_all()
    await namespace_autoregister.ensure_registered(
        "o", "lme-z", subject="s", op_name="memory.set", ch=ch
    )
    statements = [
        (sql, params)
        for sql, params in ch.query_all_calls
        if sql.lstrip().upper().startswith("SELECT")
    ]
    assert len(statements) == 3, [s[:40] for s, _ in statements]
    for sql, params in statements:
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"census statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_key_match_statements_pass_the_analyzer(ch_client) -> None:
    """The key-match arm's full statement and its
    component-only form (keyed to the pool, no wide column), each under FINAL.
    The component form keeps the full
    statement's two statistics CTEs and replaces the wide pass with a read
    keyed to `{pool_keys}`; the analyzer must accept both the lambda aliases
    and the map lookup on the scalar subquery."""
    from server.memory.app.search.sql import key_match_component_sql, key_match_sql

    where = " AND ".join(
        [
            "e.org = {org:String}",
            "e.namespace = {ns:String}",
            "e.embedding_model = {model:String}",
            "e.embedding_dim > 0",
            "e.tombstoned_at IS NULL",
            "e.status IN ('active', 'duplicate', 'conflicted')",
            "(e.agent = {agent:String} OR e.scope = 'shared')",
            "e.run_id = {run_id:String}",
        ]
    )
    params = {
        "org": "o",
        "ns": "n",
        "model": "m",
        "agent": "a",
        "run_id": "r",
        "stems": ["task", "worktre", "path"],
        "limit": 30,
        "per_key": 30,
        "min_strength": 0.5,
        "qv": [0.0] * 8,
        "pool_keys": ["user_task_worktree", "user_task_status"],
    }
    for label, sql in (
        ("full FINAL", key_match_sql(where)),
        ("component FINAL", key_match_component_sql(where)),
    ):
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"key-match {label} statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_lexical_union_statement_passes_the_analyzer(ch_client) -> None:
    """The lexical union leg (`hasAnyTokens` over the three
    text columns, `ORDER BY lex_score DESC, distance ASC, memory_key`) keeps
    its vector and its plain WHERE."""
    from server.memory.app.retrieval.side_legs import _lexical_recall_fallback

    ch = StubCH()
    q = _q(entity_filter=[], query_text="grace hash join bucket spilling bug", top_k=5)
    await _lexical_recall_fallback(ch, q, limit=50, max_distance=0.9)
    assert len(ch.query_all_calls) == 1, "one statement: the lexical union read"
    sql, params = ch.query_all_calls[0]
    assert "PREWHERE" not in sql and "hasAnyTokens(" in sql
    assert "ORDER BY lex_score DESC, distance ASC, e.memory_key ASC" in sql
    try:
        ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
    except Exception as exc:  # pragma: no cover - the assertion message is the point
        raise AssertionError(
            f"lexical union statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
        ) from exc


async def test_sensitivity_sweep_and_integrity_statements_pass_the_analyzer(ch_client) -> None:
    """The sweep's scan (scoped and unscoped), the high-sensitivity invariant
    count, the three row-level invariants as the probe embeds them, the dim-0
    row fetch, the drift listing and the entity-vector tombstone read."""
    from server.memory.app import sensitivity_sweep as sw
    from server.memory.store import integrity as ir
    from server.memory.store import sensitivity_sweep_sql as sweep_sql
    from server.memory.store.sweeps import entity_vectors_live_sql

    ch = _RecordingCH()
    await sw.scan(org="o", namespace="n", since_days=2, limit=10, page_size=10, pages=1, ch=ch)  # type: ignore[arg-type]
    await sw.scan(limit=10, page_size=10, pages=1, ch=ch)  # type: ignore[arg-type]
    # The stub returns no keys, so the page's FINAL read is added by hand: a later page's.
    page = {f"{edge}_{col}": "x" for edge in ("after", "upto") for col in sweep_sql.SORT_KEY}
    scan = sweep_sql.scan_sql(org="o", namespace="n", since_days=2, after=sweep_sql.SORT_KEY)
    params = {"shape": sw.coarse_shape_re(set()), "lim": 10, "org": "o", "ns": "n", "days": 2}
    ch.calls.append((scan, params | page))
    await sw.high_with_embedding_count(org="o", namespace="n", ch=ch)  # type: ignore[arg-type]
    await sw.high_with_embedding_count(ch=ch)  # type: ignore[arg-type]
    await ir.integrity_counts(ch=ch)  # type: ignore[arg-type]
    await ir.dim0_with_vector_rows(limit=5, org="o", namespace="n", ch=ch)  # type: ignore[arg-type]
    await ir.dim0_with_vector_rows(limit=5, ch=ch)  # type: ignore[arg-type]
    await ir.drift_sample(limit=5, org="o", ch=ch)  # type: ignore[arg-type]
    ch.calls.append((entity_vectors_live_sql(), {"org": "o", "ns": "n", "keys": ["k"]}))
    # The missing-embedding reconcile excludes `high` rows; both windows.
    from server.memory import store as memory_repo

    await memory_repo.find_memories_without_embedding_rows(batch_size=5, since_seconds=600, ch=ch)  # type: ignore[arg-type]
    await memory_repo.find_memories_without_embedding_rows(batch_size=5, org_filter="o", ch=ch)  # type: ignore[arg-type]
    assert len(ch.calls) == 12
    assert all("sensitivity != 'high'" in sql for sql, _ in ch.calls[-2:])
    for i, (sql, params) in enumerate(ch.calls):
        assert "PREWHERE" not in sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_consumer_aggregates_and_the_audit_view_pass_the_analyzer(ch_client) -> None:
    """The two `admin.consumers` statements (agent_events by
    consumer with the `args['consumer']` fallback; code_search_outcome
    by source) and the SELECT the schema installs on the audit view."""
    from server.insights.store import admin as admin_repo

    ch = StubCH()
    await admin_repo.consumers(86_400, 200, org="clickhouse", ch=ch)  # type: ignore[arg-type]
    await admin_repo.consumers(7 * 86_400, 200, ch=ch)  # type: ignore[arg-type]
    await admin_repo.consumer_outcomes(86_400, 200, org="clickhouse", ch=ch)  # type: ignore[arg-type]
    assert len(ch.query_all_calls) == 3
    for i, (sql, params) in enumerate(ch.query_all_calls):
        assert "PREWHERE" not in sql.upper()
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"consumer statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_the_acted_join_statement_passes_the_analyzer(ch_client) -> None:
    """The one statement the usage fold derives `acted` from —
    memory_exposures.returned_keys ⋈ memory_versions(run_id) with a self-join
    for duplicate_of / superseded_by — analyzer-checked with its real
    parameter types; CI's empty tables are enough."""
    from datetime import datetime

    from server.memory.workers.usage_fold import ACTED_STATEMENT

    params = {
        "since": datetime(2026, 9, 14, 0, 0, 0),
        "since_exp": datetime(2026, 9, 13, 0, 0, 0),
        "lim": 5000,
    }
    try:
        ch_client.query(f"EXPLAIN QUERY TREE {ACTED_STATEMENT}", parameters=params)
    except Exception as exc:  # pragma: no cover - the assertion message is the point
        raise AssertionError(
            f"acted statement rejected by the analyzer: {str(exc)[:300]}\n{ACTED_STATEMENT}"
        ) from exc
    # It also runs (no rows on CI) — the analyzer and the executor agree.
    ch_client.query(ACTED_STATEMENT, parameters=params)


def _server_knows(ch_client, setting: str) -> bool:
    rows = ch_client.query(
        "SELECT count() FROM system.settings WHERE name = {n:String}", parameters={"n": setting}
    ).result_rows
    return bool(rows and rows[0][0])


async def test_reinforcement_side_table_statements_pass_the_analyzer_and_round_trip(
    ch_client,
) -> None:
    """The keyed post-fetch read passes EXPLAIN QUERY TREE; a row built by the
    builder inserts into the CI table and reads back through that statement."""
    from datetime import UTC, datetime

    from server.memory.app import reinforcement as R

    sql = R.reinforcement_lookup_sql()
    params = {"org": "ci-reinforcement", "ns": "ns", "keys": ["k1", "k2"]}
    ch_client.query("EXPLAIN QUERY TREE " + sql, parameters=params)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    row = R.build_reinforcement_row(
        org="ci-reinforcement",
        namespace="ns",
        agent="bot",
        user_id="-",
        memory_key="k1",
        reinforcement_count=7,
        last_reinforced_at=now,
    )
    ch_client.insert(R.TABLE, [list(row.values())], column_names=list(row))
    got = ch_client.query(sql, parameters=params)
    rows = [dict(zip(got.column_names, r, strict=False)) for r in got.result_rows]
    assert rows and rows[0]["memory_key"] == "k1" and rows[0]["reinforcement_count"] == 7
    ch_client.command("ALTER TABLE loom.memory_reinforcement DELETE WHERE org = 'ci-reinforcement'")


def test_patch_part_probe_and_reemit_reads_pass_the_analyzer(ch_client) -> None:
    from server.core.vectors import patch_parts as P
    from server.memory.store import embeddings as M

    ch_client.query(
        "EXPLAIN QUERY TREE " + P.PATCH_PARTS_SQL,
        parameters={"db": "loom", "t": "memory_embeddings"},
    )
    got = ch_client.query(P.PATCH_PARTS_SQL, parameters={"db": "loom", "t": "memory_embeddings"})
    assert got.result_rows[0][0] == 0  # the CI table has no patch parts
    ch_client.query(
        "EXPLAIN QUERY TREE SELECT * FROM loom.memory_embeddings FINAL PREWHERE "
        + M.EMB_KEY_PREWHERE
        + " WHERE tombstoned_at IS NULL",
        parameters={"org": "o", "ns": "n", "a": "a", "u": "u", "s": "s", "k": "k"},
    )
    ch_client.query(
        "EXPLAIN QUERY TREE SELECT * FROM loom.memory_embeddings FINAL PREWHERE "
        + M._EMB_SHARED_PREWHERE
        + " WHERE tombstoned_at IS NULL",
        parameters={"org": "o", "ns": "n", "k": "k"},
    )


async def test_main_fact_leg_with_the_codes_flag_passes_the_analyzer(ch_client) -> None:
    """The main leg's SETTINGS with `vector_search_use_quantized_codes = 1`
    and its SELECT columns. Skipped on a server that does not know the
    setting (26.6-): the name itself would fail the statement there, which is
    exactly why the flag rides the replica probe."""
    from server.core.vectors import patch_parts as P
    from server.core.vectors.quantized_setting import set_quantized_codes_supported
    from server.memory.app.retrieval import vector_search

    class _CH(StubCH):
        async def query_all(self, sql, params=None):
            if "system.parts" in sql:
                return [{"patch_parts": 0}]
            return await super().query_all(sql, params)

    P.reset()
    set_quantized_codes_supported(_server_knows(ch_client, "vector_search_use_quantized_codes"))
    try:
        ch = _CH()
        q = _q(embedding_model="text-embedding-3-small", execution_planes=["fact"])
        await vector_search(q, ch=ch)
        sql, params = next(
            (s, p) for s, p in ch.query_all_calls if "FROM loom.memory_embeddings AS e FINAL" in s
        )
        assert "e.reinforcement_count AS reinforcement_base" in sql
        if set_quantized_codes_supported.__module__ and _server_knows(
            ch_client, "vector_search_use_quantized_codes"
        ):
            assert "vector_search_use_quantized_codes = 1" in sql
        else:
            assert "vector_search_use_quantized_codes" not in sql
        ch_client.query("EXPLAIN QUERY TREE " + sql, parameters=params)
    finally:
        set_quantized_codes_supported(None)
        P.reset()


async def test_deep_evidence_keyed_reads_pass_the_analyzer(ch_client) -> None:
    """The deep pass reads links by key and
    evidence by id, each `LIMIT 1 BY` the table key with the tombstone filter
    outside, no FINAL. The second read is issued with the first read's ids
    typed as UUIDs."""
    from server.memory.app.deep_evidence import fetch_raw_evidence

    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "memory_key": "k1",
                "support_type": "extracted_from",
                "char_start": 0,
                "char_end": 12,
                "evidence_id": "00000000-0000-0000-0000-000000000001",
            }
        ]
    )
    ch.query_all_returns.append([])
    await fetch_raw_evidence(
        org="o",
        namespace="n",
        memory_keys=["k1", "k2"],
        max_chars_per_memory=500,
        max_chars_total=2000,
        ch=ch,
    )
    assert len(ch.query_all_calls) == 2, "links by key, then evidence by id"
    for i, (sql, params) in enumerate(ch.query_all_calls):
        assert "FINAL" not in sql and "PREWHERE" not in sql
        assert "LIMIT 1 BY" in sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"deep-evidence statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_digest_leg_statement_passes_the_analyzer_without_limit_by(ch_client) -> None:
    """The digest leg orders by the bare distance and carries
    no `LIMIT 1 BY session_id`; the per-session dedup is Python's. Checked
    with an explicit agent (LIMIT n) and the wildcard (over-fetched)."""
    from server.memory.app.retrieval import fetch_session_summaries

    for agent in ("bot", "*"):
        ch = StubCH()
        await fetch_session_summaries(
            ch,
            org="o",
            namespace="n",
            agent=agent,
            user_id="u",
            query_vec=[0.1] * 3072,
            embedding_model="text-embedding-3-large",
        )
        assert len(ch.query_all_calls) == 1
        sql, params = ch.query_all_calls[0]
        code = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
        assert "LIMIT 1 BY" not in code and "PREWHERE" not in code
        assert "ORDER BY distance ASC\n" in code
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"digest leg statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_chunk_provenance_and_rollup_statements_pass_the_analyzer(ch_client) -> None:
    """The export's link read, the census and the user-scope tombstone count
    over the provenance link tables, and the two refreshable roll-up views'
    SELECTs. Plain WHERE everywhere."""
    from server.erasure.store import census as erasure_census
    from server.erasure.store import steps_graph as graph_steps
    from server.memory.app import chunk_links as cl

    statements: list[tuple[str, dict[str, Any]]] = []
    params = {"org": "o", "ns": "n", "sessions": ["s1", "s2"]}
    statements.append((cl.KEYS_SQL, params))

    class _KeysCH(_RecordingCH):
        """The user-scope step's read of the user's keys finds one."""

        async def query_all(self, sql, params=None):
            self.calls.append((sql, params or {}))
            return [{"namespace": "n", "memory_key": "k"}] if "FROM loom.memory " in sql else []

    ch = _KeysCH()
    await erasure_census.residue_census("o", "user_id", "alice", ch=ch)  # type: ignore[arg-type]
    await graph_steps.tombstone_source_links_by_user("o", "alice", ch=ch)  # type: ignore[arg-type]
    await graph_steps.tombstone_source_links_by_namespace("o", ["a", "b"], ch=ch)  # type: ignore[arg-type]
    links = [(sql, p) for sql, p in ch.calls if "loom.memory_chunk_sources" in sql]
    assert len(links) >= 3, [sql[:80] for sql, _ in links]
    statements += links
    statements += [(sql, p) for sql, p in ch.calls if "FROM loom.memory " in sql]
    for view in ("agent_cost_rollup_refresh", "memory_exposure_breadth_hourly_refresh"):
        statements.append((view_select(view), {}))
    # cost.summary: the roll-up's states UNION ALL the live tail, merged.
    from datetime import date

    from server.insights.api.models_cost import CostSummaryInput
    from server.insights.app import cost as cost_h
    from server.insights.store import cost as cost_store

    class _CostCH(_RecordingCH):
        async def query_all(self, sql, params=None):
            self.calls.append((sql, params or {}))
            return []

    cost_ch = _CostCH()
    cost_h.get_client = lambda: cost_ch  # type: ignore[assignment]
    try:
        for kw in ({}, {"group_by": ["namespace", "day"], "agent": "a", "operation": "memory.ask"}):
            await cost_h.handle_cost_summary(
                CostSummaryInput(
                    org="o",
                    namespace="n",
                    from_date=date(2026, 9, 1),
                    to_date=date(2026, 9, 14),
                    **kw,
                )
            )
    finally:
        from server.core.ch import get_client as _real_get_client

        cost_h.get_client = _real_get_client  # type: ignore[assignment]
    # The default readout also issues the cache-totals
    # statement (no UNION), so two readouts are four statements.
    assert len(cost_ch.calls) == 4
    assert sum("UNION ALL" in sql for sql, _ in cost_ch.calls) == 2
    assert sum(cost_store.CACHE_SELECT in sql for sql, _ in cost_ch.calls) == 2
    statements += cost_ch.calls
    assert len(statements) >= 1 + 3 + 2 + 4
    for i, (sql, p) in enumerate(statements):
        assert "PREWHERE" not in sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=p)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_traffic_class_statements_pass_the_analyzer(ch_client) -> None:
    """The registry point read that classes a request
    (`loom.namespaces FINAL`, kind + tags) and the `cost.summary` events scan
    grouped and filtered by `traffic_class` pass `EXPLAIN QUERY TREE`; every
    statement is plain WHERE."""
    from datetime import date

    from server.identity.app.traffic_class_registry import REGISTRY_CLASS_SQL
    from server.insights.api.models_cost import CostSummaryInput
    from server.insights.app import cost as cost_h

    statements: list[tuple[str, dict[str, Any]]] = [
        (REGISTRY_CLASS_SQL, {"org": "clickhouse", "ns": "kcurve-0"})
    ]

    ch = StubCH()
    cost_h.get_client = lambda: ch  # type: ignore[assignment]
    try:
        await cost_h.handle_cost_summary(
            CostSummaryInput(
                org="clickhouse",
                namespace="kcurve-0",
                from_date=date(2026, 9, 1),
                to_date=date(2026, 9, 14),
                group_by=["traffic_class", "operation"],
                traffic_class="bench",
            )
        )
    finally:
        from server.core.ch import get_client as _real

        cost_h.get_client = _real  # type: ignore[assignment]
    statements.extend(ch.query_all_calls)
    assert len(statements) == 2
    for i, (sql, params) in enumerate(statements):
        assert "PREWHERE" not in sql.upper()
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"traffic-class statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_budget_violation_reader_and_graph_reader_statements_pass_the_analyzer(
    ch_client,
) -> None:
    """(b) the `memory.budget_violations` read over loom.events
    is analyzer-checked with its real parameter types, and a row written the
    way the transport writes it is read back through the same SELECT. (c) the
    graph readers that honour `invalid_at IS NULL` pass the analyzer."""
    from server.insights.store.admin import budget_violations_sql
    from server.memory.app.retrieval import fetch_graph_neighbors
    from server.observability import events_stream as es

    # ---- (b) reader --------------------------------------------------------
    for extra in ("", " AND org = {org:String}"):
        sql = budget_violations_sql(extra)
        params = {"since": 600, "lim": 50, "org": "clickhouse"}
        ch_client.query("EXPLAIN QUERY TREE " + sql, parameters=params)
    ns = f"an3870-{__import__('uuid').uuid4().hex[:6]}"
    ev = es.to_event(
        es.BUDGET_VIOLATION_KIND,
        {
            "event_type": "budget.violated",
            "org": "clickhouse",
            "namespace": ns,
            "agent": "a",
            "actor": "control-plane",
            "request_id": "r",
            "operation": "memory.set",
            "elapsed_ms": 900,
            "budget_ms": 600,
            "overage_ratio": 1.5,
            "suppressed_since_last": 4,
        },
    )
    ch_client.insert("loom.events", [list(ev.values())], column_names=list(ev.keys()))
    rows = ch_client.query(
        budget_violations_sql(" AND org = {org:String}"),
        parameters={"since": 600, "lim": 50, "org": "clickhouse"},
    ).named_results()
    mine = [r for r in rows if r["namespace"] == ns]
    assert mine and mine[0]["operation"] == "memory.set" and int(mine[0]["violations"]) == 5
    assert float(mine[0]["max_overage_ratio"]) == 1.5

    # ---- (c) the readers that honour invalid_at -----------------------------
    from tests.unit.test_memory_search_service import StubCH

    stub = StubCH()
    stub.query_all_returns.extend([[], []])
    await fetch_graph_neighbors(
        stub,
        org="clickhouse",
        namespace=ns,
        seed_entities=["clickhouse"],
        hub_skip_degree=0,
        query_date="2026-09-01",
    )
    for sql, prm in stub.query_all_calls:
        ch_client.query("EXPLAIN QUERY TREE " + sql, parameters=prm)

    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    stub = StubCH()
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(facts_handlers, "get_client", lambda: stub)
    try:
        await facts_handlers.handle_facts_traverse(
            FactsTraverseInput(org="clickhouse", namespace=ns, seed_entity="user")
        )
    finally:
        mp.undo()
    assert stub.query_all_calls
    for sql, prm in stub.query_all_calls:
        assert "invalid_at IS NULL" in sql
        ch_client.query("EXPLAIN QUERY TREE " + sql, parameters=prm)

    from server.memory.api.models_facts import FactsPathInput

    stub = StubCH()
    mp = _pytest.MonkeyPatch()
    mp.setattr(facts_handlers, "get_client", lambda: stub)
    try:
        await facts_handlers.handle_facts_path(
            FactsPathInput(org="clickhouse", namespace=ns, entity_a="user", entity_b="clickhouse")
        )
    finally:
        mp.undo()
    assert stub.query_all_calls
    for sql, prm in stub.query_all_calls:
        assert "invalid_at IS NULL" in sql
        ch_client.query("EXPLAIN QUERY TREE " + sql, parameters=prm)


@pytest.mark.asyncio
async def test_facts_source_fanout_statements_pass_the_analyzer(ch_client) -> None:
    """facts.list's include_sources read of `entity_triple_memories` keeps
    `FINAL` + `groupArray(10)`, which is cheaper in wall on the all-key table.
    Plain WHERE; the aggregate is aliased to a new name."""
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app import facts as facts_handlers

    class _CH(StubCH):
        async def query_all(self, sql, params=None):
            self.query_all_calls.append((sql, params or {}))
            if "FROM loom.entity_triples" in sql and "entity_triple_memories" not in sql:
                from datetime import datetime as _dt

                return [
                    {
                        "subject": "alice",
                        "relation": "works_at",
                        "object": "acme",
                        "co_occur": 1,
                        "last_seen": _dt(2026, 9, 1),
                        "provenance": "",
                    }
                ]
            return []

    mp = pytest.MonkeyPatch()
    ch = _CH()
    mp.setattr(facts_handlers, "get_client", lambda: ch)
    try:
        await facts_handlers.handle_facts_list(
            FactsListInput(org="o", namespace="n", include_sources=True)
        )
    finally:
        mp.undo()
    fanout = [(sql, p) for sql, p in ch.query_all_calls if "entity_triple_memories" in sql]
    assert len(fanout) == 1, [sql[:60] for sql, _ in ch.query_all_calls]
    assert "FROM loom.entity_triple_memories FINAL" in fanout[0][0]  # facts.list keeps the merge
    assert "groupArray(10)(memory_key) AS source_keys" in fanout[0][0]
    for i, (sql, p) in enumerate(fanout):
        assert "PREWHERE" not in sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=p)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"fan-out statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


def test_grep_wrong_abstain_statements_pass_the_analyzer(ch_client) -> None:
    """The ledger read (scoped, and the behind-schema fallback),
    the one-scan recount with aligned prefixes the gauge and
    bench/code_grep_replay.py share, and the bench's per-call ledger read with
    and without the newer columns, analyzer-checked with the real
    parameter types."""
    from bench import code_grep_replay as bench
    from server.observability import grep_replay_probe as gp

    captured: list[tuple[str, dict[str, Any]]] = []

    class _Rows:
        result_rows: list[Any] = []
        column_names: list[str] = []

    class _Sync:
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Rows:
            captured.append((sql, dict(parameters or {})))
            return _Rows()

    assert gp.wrong_abstains(_Sync(), hours=24) == []
    assert gp.recount(_Sync(), "o", "n", [("Alias", ""), ("Alias", "src/")]) == {
        ("Alias", ""): 0,
        ("Alias", "src/"): 0,
    }
    assert len(captured) == 2 and "multiSearchAllPositions" in captured[1][0]
    captured.append(
        (
            bench.LEDGER_ROWS_SQL,
            {"hours": 24, "successes": 1, "org": "o", "ns": "n", "consumer": ""},
        )
    )
    for i, (sql, params) in enumerate(captured):
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"wrong-abstain statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_identifier_read_statements_pass_the_analyzer(ch_client) -> None:
    """The cosine-ordered fact identifier read is a keyed FINAL re-read whose
    identity pass is its subquery (`optimize_move_to_prewhere_if_final`); the
    recency and length orders are one plain statement. Every shape the
    service builds — default, agent-bound with a user (the key-prefix
    variant), case-insensitive, a long number matched on digits, the
    recency order and the length order — goes through `EXPLAIN QUERY TREE`."""
    from server.memory import store as memory_repo

    calls: list[tuple[str, dict[str, Any]]] = []
    variants: list[dict[str, Any]] = [
        {},
        {"agent": "bot", "user_id": "u1"},
        {"case_insensitive": True},
        {"identifiers": ["20202201042", "SampleUser"]},
        {"order": "recency"},
        {"query_embedding": None},
    ]
    for v in variants:
        ch = StubCH()
        ch.query_all_returns.append([])
        kw: dict[str, Any] = dict(
            org="o",
            namespace="n",
            identifiers=["117947"],
            limit=8,
            query_embedding=[0.1] * 3072,
            scope="private",
            agent="*",
            ch=ch,
        )
        kw.update(v)
        await memory_repo.find_by_identifier(**kw)
        vector = bool(kw.get("query_embedding")) and v.get("order") != "recency"
        assert len(ch.query_all_calls) == 1, v
        sql = ch.query_all_calls[0][0]
        if vector:
            assert "IN (" in sql and "SELECT DISTINCT org" in sql
            assert "optimize_move_to_prewhere_if_final = 1" in sql
        else:
            assert "SELECT DISTINCT" not in sql
        calls += ch.query_all_calls
    # the chunk read rides along
    ch = StubCH()
    await memory_repo.find_chunks_by_identifier(
        org="o",
        namespace="n",
        identifiers=["117947"],
        limit=4,
        query_embedding=[0.1] * 3072,
        scope="private",
        agent="bot",
        ch=ch,
        markers=["root cause"],
    )
    calls += ch.query_all_calls
    assert len(calls) == 6 + 1
    for i, (sql, params) in enumerate(calls):
        assert "PREWHERE" not in sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"identifier statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_threads_and_category_read_statements_pass_the_analyzer(
    ch_client, monkeypatch
) -> None:
    """Conversation.threads with an agent filter (the aggregate
    alias that shadowed the WHERE predicate, code 184) and the two plain reads
    behind namespace.list_categories (the correlated ORDER BY subquery, code
    48) — captured through the stub with their real parameter types."""
    from server.identity.api.models_namespace_catalog import NamespaceListCategoriesInput
    from server.identity.app import namespace_catalog as ns_handlers
    from server.knowledge.api.models_conversations import ConversationThreadsInput
    from server.knowledge.app.conversations import handle_conversation_threads

    ch = _StubCHWithOne()
    ch.query_all_returns.append([{"category_id": "c1", "name": "n", "description": "d"}])
    monkeypatch.setattr(ns_handlers, "get_client", lambda: ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    await ns_handlers.handle_namespace_list_categories(
        NamespaceListCategoriesInput(org="o", namespace="ns")
    )
    await handle_conversation_threads(ConversationThreadsInput(org="o", namespace="ns", agent="a"))
    assert len(ch.query_all_calls) == 3, [s[:40] for s, _ in ch.query_all_calls]
    for sql, params in ch.query_all_calls:
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"read statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


@pytest.mark.asyncio
async def test_reliance_split_statements_pass_the_analyzer(ch_client) -> None:
    """The fold's identity read with the `used_cited`
    sum joined beside it, the two `include_usage` point-reads, the agent-label
    and retention gauge probes and the consumer tagging aggregate — each through
    `EXPLAIN QUERY TREE` with its real parameter types."""
    from server.insights.store import admin as admin_repo
    from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
    from server.memory.app.search import payload as mod
    from server.memory.store.usage import AGENT_LABEL_EVENT_TYPES
    from server.memory.workers.usage_fold import IDENTITY_STATEMENT
    from server.observability.otel_probes import (
        _AGENT_LABELS_SQL,
        _RETENTION_SQL,
        RETENTION_STALE_DAYS,
    )

    ids = ["6b47c9b7-0000-0000-0000-000000000001"]
    statements: list[tuple[str, dict[str, Any]]] = [
        (IDENTITY_STATEMENT, {"orgs": ["o"], "nss": ["n"], "ids": ids}),
        (_AGENT_LABELS_SQL, {"kinds": list(AGENT_LABEL_EVENT_TYPES)}),
        (_RETENTION_SQL, {"stale": RETENTION_STALE_DAYS}),
    ]
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "namespace": "n",
                "memory_key": "k1",
                "mids": ids,
                "pinned": 0,
                "evidence_count": 1,
                "reinforcement_base": 1,
            }
        ]
    )
    saved = mod.get_client
    mod.get_client = lambda: ch  # type: ignore[assignment]
    try:
        hit = _MemorySearchHit(
            memory_key="k1",
            memory_type="semantic",
            content_excerpt="x",
            distance=0.1,
            similarity=0.9,
        )
        await mod._fill_hit_usage(
            [hit], MemorySearchInput(org="o", namespace="n", query="q", include_usage=True), ["n"]
        )
    finally:
        mod.get_client = saved  # type: ignore[assignment]
    assert len(ch.query_all_calls) == 3, "rows, counts, folded reinforcement"
    statements += ch.query_all_calls
    tagging = StubCH()
    await admin_repo.consumer_tagging(3600, 10, org="o", ch=tagging)  # type: ignore[arg-type]
    statements += tagging.query_all_calls
    assert len(statements) == 7
    for sql, params in statements:
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"consumer tagging statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


def test_storage_housekeeping_statements_pass_the_analyzer(ch_client) -> None:
    """The storage gauge statements (system.parts over the closed table list;
    system.view_refreshes for the loom roll-ups; the SLI rollup's sentinel and
    the ledger rows it missed) with the parameters the shared probe client binds."""
    from server.observability.otel_probe_client import database_parameters
    from server.observability.otel_probes import (
        _SLI_LAG_SQL,
        _SLI_MISSED_SQL,
        _STORAGE_PARTS_SQL,
        _VIEW_REFRESH_SQL,
        STORAGE_TABLES,
    )

    for sql, params in (
        (_STORAGE_PARTS_SQL, {**database_parameters(), "tables": list(STORAGE_TABLES)}),
        (_VIEW_REFRESH_SQL, database_parameters()),
        (_SLI_LAG_SQL, {}),
        (_SLI_MISSED_SQL, {}),
    ):
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"storage statement rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_lexical_stats_refresh_statements_pass_the_analyzer(ch_client) -> None:
    """The two refreshable views' SELECTs (data branch UNION ALL
    sentinel branch, each with the self-referencing mark subquery), plus the
    two reader statements of _fetch_bm25_corpus_stats they feed. Plain WHERE everywhere; the CI
    database holds both roll-ups with their ts_hwm column."""
    from server.memory.app import search_bm25

    statements: list[tuple[str, dict[str, Any]]] = [
        (view_select(view), {}) for view in ("memory_term_freq_refresh", "memory_doc_stats_refresh")
    ]

    class _Reader:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
            self.calls.append((sql, params or {}))
            if "uniqExactMerge" in sql:
                return [{"term": "zebra", "df": 2}]
            return [{"n": 10, "avgdl": 20.0}]

    reader = _Reader()
    search_bm25._BM25_CORPUS_CACHE.clear()
    idfs, avgdl = await search_bm25._fetch_bm25_corpus_stats(
        reader,
        org="o",
        namespace="n",
        tokens=["zebra", "lantern"],  # type: ignore[arg-type]
    )
    assert len(idfs) == 2 and avgdl == 20.0 and len(reader.calls) == 2
    statements += reader.calls
    for i, (sql, p) in enumerate(statements):
        assert "PREWHERE" not in sql
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=p)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc


async def test_uncounted_table_erasure_statements_pass_the_analyzer(ch_client, monkeypatch) -> None:
    """The steps for the tables the census did not count —
    every count SELECT in both scopes and, for every mutation they submit,
    the WHERE re-read as `SELECT count() FROM t WHERE …` (EXPLAIN takes no
    ALTER). The census over the widened registry is run again too, so a
    plain MergeTree row never reads FINAL."""
    from server.erasure.store import census as erasure_census
    from server.erasure.store import mutations as erasure_mutations
    from server.erasure.store import steps_graph as graph_steps
    from server.erasure.store import steps_ledgers as ledger_steps

    class _One(_RecordingCH):
        async def query_one(self, sql, params=None):
            self.calls.append((sql, params or {}))
            # `engine` makes the run-claims step see its KeeperMap table and submit its delete.
            return {"n": 1, "engine": "KeeperMap"}

    mutations: list[tuple[str, str, dict[str, Any]]] = []

    async def _rec(table, *, mutation="DELETE", where, params, stamp=None, ch=None, wait_s=None):
        mutations.append((table, where, params))
        return None

    monkeypatch.setattr(erasure_mutations, "run_mutation", _rec)
    monkeypatch.setattr(erasure_mutations, "run_lightweight_delete", _rec)
    monkeypatch.setattr(erasure_mutations, "run_keeper_delete", _rec)
    ch = _One()
    ns = ["a", "b"]
    for fn in (
        ledger_steps.tombstone_conversations_by_namespace,
        ledger_steps.tombstone_context_blobs_by_namespace,
        ledger_steps.delete_memory_exposures_by_namespace,
        ledger_steps.delete_search_trace_log_by_namespace,
        ledger_steps.delete_code_search_trace_by_namespace,
        ledger_steps.delete_events_by_namespace,
        ledger_steps.delete_memory_drops_by_namespace,
        ledger_steps.delete_learnings_queue_by_namespace,
        ledger_steps.delete_agent_runs_by_namespace,
        ledger_steps.tombstone_curation_by_namespace,
        ledger_steps.delete_consolidation_runs_by_namespace,
        ledger_steps.delete_projection_failures_by_namespace,
        graph_steps.delete_entity_graph_by_namespace,
    ):
        await fn("o", ns, ch=ch)  # type: ignore[arg-type]
    for fn in (
        ledger_steps.tombstone_conversations_by_user,
        ledger_steps.tombstone_context_blobs_by_user,
        ledger_steps.delete_memory_exposures_by_user,
        ledger_steps.delete_code_search_trace_by_user,
        ledger_steps.delete_events_by_user,
        ledger_steps.delete_agent_runs_by_user,
        ledger_steps.delete_learnings_queue_by_user,
        ledger_steps.delete_consolidation_runs_by_user,
    ):
        await fn("o", "alice", ch=ch)  # type: ignore[arg-type]
    await erasure_census.residue_census("o", "namespace", ns, ch=ch)  # type: ignore[arg-type]
    await erasure_census.residue_census("o", "user_id", "alice", ch=ch)  # type: ignore[arg-type]
    assert len(mutations) >= 29 and len(ch.calls) >= 29 + 50 + 31
    for i, (sql, params) in enumerate(ch.calls):
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"count statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc
    for table, where, params in mutations:
        sql = f"SELECT count() FROM {table} WHERE {where}"
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"mutation predicate on {table} rejected by the analyzer: {str(exc)[:300]}\n{where}"
            ) from exc


async def test_cache_key_axis_and_credential_ledger_statements_pass_the_analyzer(ch_client) -> None:
    """The user-scope deletes of the response cache, the
    precedents and the communities (parent-key predicates through
    `window_keys` and the edge links), `memory.redact`'s by-key deletes and
    the user census over the three tables are analyzer-checked with their
    real parameter types; the mutations themselves — a scalar subquery inside
    an ALTER DELETE, `mapApply` over the events `args` map — are run for real
    against the migrated CI tables (empty; ClickHouse still types every
    expression); a cache row written with `window_keys` is read back by the
    `has()` the redact path uses."""
    import server.memory.store as memory_repo
    from server.erasure.store import census as erasure_census
    from server.erasure.store import predicates as erasure_predicates
    from server.erasure.store import steps_graph as graph_steps
    from server.erasure.store import steps_memory as memory_steps

    ch = _RecordingCH()
    await graph_steps.delete_llm_response_cache_by_user("o", "alice", ch=ch)  # type: ignore[arg-type]
    await graph_steps.delete_ask_precedents_by_user("o", "alice", ch=ch)  # type: ignore[arg-type]
    await graph_steps.delete_entity_communities_by_user("o", "alice", ch=ch)  # type: ignore[arg-type]
    await memory_repo.delete_llm_response_cache_by_key("o", "k1", ch=ch)  # type: ignore[arg-type]
    await memory_repo.delete_ask_precedents_by_key("o", "k1", ch=ch)  # type: ignore[arg-type]
    await erasure_census.residue_census("o", "user_id", "alice", ch=ch)  # type: ignore[arg-type]
    counts = [c for c in ch.calls if c[0].lstrip().startswith("SELECT count()")]
    assert len(counts) >= 5 + 23
    assert any("hasAny(window_keys," in sql for sql, _ in counts)
    assert any("(namespace, label) IN" in sql for sql, _ in counts)
    for i, (sql, params) in enumerate(counts):
        try:
            ch_client.query(f"EXPLAIN QUERY TREE {sql}", parameters=params)
        except Exception as exc:  # pragma: no cover - the assertion message is the point
            raise AssertionError(
                f"statement #{i} rejected by the analyzer: {str(exc)[:300]}\n{sql}"
            ) from exc
    # the mutations, for real, on the empty CI tables
    params = {"org": "analyzer-census", "uid": "nobody", "key": "k1"}
    for table, pred in (
        ("loom.llm_response_cache", erasure_predicates.USER_WINDOW_KEYS),
        ("loom.memory_ask_precedents", erasure_predicates.USER_PRECEDENT_KEYS),
        ("loom.entity_communities", erasure_predicates.USER_COMMUNITY_ROWS),
        ("loom.llm_response_cache", "has(window_keys, {key:String})"),
        ("loom.memory_ask_precedents", "has(arrayConcat(window_keys, cited_keys), {key:String})"),
    ):
        ch_client.command(
            f"ALTER TABLE {table} DELETE WHERE org = {{org:String}} AND {pred} "
            "SETTINGS mutations_sync = 2",
            parameters=params,
        )
    ch_client.command(
        f"ALTER TABLE loom.agent_events {memory_steps.EVENTS_SCRUB_MUTATION} "
        "WHERE org = {org:String} AND user_id = {uid:String} SETTINGS mutations_sync = 2",
        parameters=params,
    )
    # a cache row with its key axis round-trips through the has() the redact path uses
    from server.llm.cache import cache_key

    key = cache_key("sys", f"user {id(ch)}", 600)
    ch_client.insert(
        "loom.llm_response_cache",
        [
            [
                "analyzer-model",
                key,
                "analyzer-census",
                "n",
                "ask_synthesize",
                "answer [k1]",
                12,
                3,
                ["k1", "k2"],
            ]
        ],
        column_names=[
            "model",
            "key",
            "org",
            "namespace",
            "stage",
            "text",
            "tokens_in",
            "tokens_out",
            "window_keys",
        ],
    )
    res = ch_client.query(
        "SELECT window_keys FROM loom.llm_response_cache WHERE model = 'analyzer-model' "
        "AND key = {key:String} AND has(window_keys, {k:String}) LIMIT 1",
        parameters={"key": key, "k": "k2"},
    )
    assert res.result_rows and list(res.result_rows[0][0]) == ["k1", "k2"]
    # audit_log carries token_id
    cols = {row[0] for row in ch_client.query("DESCRIBE loom.audit_log").result_rows}
    assert "token_id" in cols
