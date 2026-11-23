"""Integration tests for the code intelligence plane.

Exercise the ingest lifecycle and the read ops against the live ClickHouse
(via the `ch_client` env config), calling the service/handler layers
directly — the HTTP transport is covered by its own generated-route tests;
what needs proving here is the storage contract:

  * initial ingest -> chunks/symbols/edges live
  * unchanged re-ingest -> zero churn (content-addressed memoization)
  * modification -> only touched chunks churn; overloads stay distinct
  * scip delta apply -> tier coexistence, scip preferred on read
  * deletion -> everything tombstoned
  * OPTIMIZE FINAL -> overload rows survive engine merges

Skips cleanly when LOOM_CH_* points nowhere (house rule).
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytestmark = pytest.mark.asyncio

_SRC = b"""namespace DB {
class Squasher {
public:
    /// Flush buffered rows.
    void flush() { squashImpl(); }
    void squashImpl() { doWork(); }
    void insert(int v) { }
    void insert(double v) { }
};
void doWork() { }
}
"""


@pytest.fixture
async def code_env(ch_client):
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    ns = f"code-it-{os.urandom(4).hex()}"
    yield CodeRepo(), "test-org", ns


async def _ingest(repo, org, ns, source=_SRC, sha="a" * 40):
    from server.code.app.index.ingest import ingest_file
    from server.embedding.fake import FakeNGramEmbedder
    from server.settings import settings

    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    return await ingest_file(
        repo,
        org,
        ns,
        path="src/S.cpp",
        source=source,
        commit_sha=sha,
        embedding_model=embedder.model_name,
        embedder=embedder,
    )


async def test_ingest_lifecycle(code_env) -> None:
    repo, org, ns = code_env

    s1 = await _ingest(repo, org, ns)
    assert s1.chunks_new > 0 and s1.symbols_new >= 5 and s1.edges_new >= 2

    # overloads: two insert() symbols with distinct ids survive ingest
    rows = await repo.symbols_by_name(org, ns, "Squasher::insert")
    assert len(rows) == 2, [r["signature"] for r in rows]
    assert len({str(r["symbol_id"]) for r in rows}) == 2

    # unchanged re-ingest: zero churn
    s2 = await _ingest(repo, org, ns, sha="b" * 40)
    assert (s2.chunks_new, s2.chunks_tombstoned, s2.symbols_new, s2.edges_new) == (0, 0, 0, 0)
    assert s2.chunks_kept == s1.chunks_new

    # modify one method body: only its chunk churns; symbols stable
    s3 = await _ingest(
        repo, org, ns, source=_SRC.replace(b"doWork();", b"doWork(); log();"), sha="c" * 40
    )
    assert s3.chunks_new >= 1
    assert s3.chunks_kept >= 1
    assert s3.symbols_new == 0 and s3.symbols_tombstoned == 0


async def test_grep_can_return_distinct_retained_enclosing_bodies(ch_client, monkeypatch) -> None:
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app.grep import handle_code_grep
    from server.code.app.index.repo_registry import read_registry
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.settings import settings
    from tests.integration._resources import unique_org

    await get_client().connect()
    assert await read_registry(get_client())
    org, ns = unique_org("grep-bodies"), "source"
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    await _ingest(CodeRepo(), org, ns)
    plain = await handle_code_grep(CodeGrepInput(org=org, namespace=ns, pattern="squashImpl"))
    rich = await handle_code_grep(
        CodeGrepInput(
            org=org,
            namespace=ns,
            pattern="squashImpl",
            include_body=True,
        )
    )
    assert rich.matches == plain.matches and rich.matches
    assert not plain.definitions
    assert not rich.definitions_partial
    assert len(rich.definitions) == len({row["symbol_id"] for row in rich.definitions}) == 2
    assert all("squashImpl" in row["body"] and not row["body_elided"] for row in rich.definitions)

    # A vanished continuation pool reruns lines, but must hydrate only its page.
    from server.code.app import grep

    first = await handle_code_grep(
        CodeGrepInput(
            org=org,
            namespace=ns,
            pattern="squashImpl",
            limit=1,
            include_body=True,
        )
    )
    assert first.next_cursor
    original = grep.definitions_for
    hydrated = []

    async def bodies(input, matches, budget):
        hydrated.extend((row["path"], row["line"]) for row in matches)
        return await original(input, matches, budget)

    async def missing_pool(*args):
        return None

    monkeypatch.setattr(grep, "definitions_for", bodies)
    monkeypatch.setattr(grep, "_read_pool_settling", missing_pool)
    second = await handle_code_grep(
        CodeGrepInput(
            org=org,
            namespace=ns,
            pattern="squashImpl",
            limit=1,
            include_body=True,
            cursor=first.next_cursor,
        )
    )
    assert second.matches == rich.matches[1:2]
    assert hydrated == [(row["path"], row["line"]) for row in second.matches]
    assert not second.definitions_partial
    assert len(second.definitions) == 1


async def test_overloads_survive_engine_merge(code_env, ch_client) -> None:
    repo, org, ns = code_env
    await _ingest(repo, org, ns)
    ch_client.command("OPTIMIZE TABLE loom.code_symbols FINAL")
    rows = await repo.symbols_by_name(org, ns, "Squasher::insert")
    assert len(rows) == 2, "overload rows collapsed under RMT merge"


async def test_scip_tier_coexists_and_wins(code_env) -> None:
    from server.code.app.index.scip_delta import ScipDelta, apply_delta

    repo, org, ns = code_env
    await _ingest(repo, org, ns)

    delta = ScipDelta.model_validate(
        {
            "delta_version": 1,
            "commit": "d" * 40,
            "files": [
                {
                    "path": "src/S.cpp",
                    "symbols": [
                        {
                            "qualified_name": "DB::Squasher::flush",
                            "signature": "()",
                            "kind": "method",
                            "line_start": 5,
                            "line_end": 5,
                            "doc": "compiler tier",
                        }
                    ],
                    "edges": [
                        {
                            "edge_kind": "calls",
                            "from_qualified_name": "DB::Squasher::flush",
                            "from_signature": "()",
                            "to_qualified_name": "DB::Squasher::squashImpl",
                            "to_signature": "()",
                            "line": 5,
                        }
                    ],
                }
            ],
        }
    )
    stats = await apply_delta(repo, org, ns, delta)
    assert stats.symbols_new == 1 and stats.edges_new == 1

    rows = await repo.symbols_by_name(org, ns, "DB::Squasher::flush")
    tiers = {str(r["tier"]) for r in rows}
    # both tiers exist as rows; the read preference returns scip first
    assert "scip" in tiers
    assert str(rows[0]["tier"]) == "scip"

    # idempotent re-apply converges (no new rows)
    stats2 = await apply_delta(repo, org, ns, delta)
    assert stats2.symbols_new == 0 and stats2.edges_new == 0


async def test_scip_ingest_op_roundtrip(code_env, monkeypatch) -> None:
    from server.code.api.models_ingest import CodeScipIngestInput, CodeScipStatusInput
    from server.code.app.ingest import handle_code_scip_ingest, handle_code_scip_status
    from server.code.workers import scip_apply as code_scip_apply
    from server.settings import settings

    repo, org, ns = code_env
    monkeypatch.setattr(settings, "code_org", org)
    monkeypatch.setattr(settings, "code_namespace", ns)

    payload = json.dumps(
        {
            "delta_version": 1,
            "commit": "e" * 40,
            "files": [
                {
                    "path": "src/T.cpp",
                    "symbols": [
                        {
                            "qualified_name": "DB::tOnly",
                            "signature": "()",
                            "kind": "function",
                            "line_start": 1,
                            "line_end": 3,
                        }
                    ],
                    "edges": [],
                }
            ],
            "deleted_files": [],
        }
    )
    bid = uuid.uuid4().hex
    half = len(payload) // 2
    r0 = await handle_code_scip_ingest(
        CodeScipIngestInput(
            org=org,
            namespace=ns,
            batch_id=bid,
            seq=0,
            total_parts=2,
            commit_sha="e" * 40,
            payload=payload[:half],
        )
    )
    assert not r0.complete
    r1 = await handle_code_scip_ingest(
        CodeScipIngestInput(
            org=org,
            namespace=ns,
            batch_id=bid,
            seq=1,
            total_parts=2,
            commit_sha="e" * 40,
            payload=payload[half:],
        )
    )
    assert r1.complete

    result = await code_scip_apply.run_pass()
    assert result.batches_applied >= 1, result.errors

    st = await handle_code_scip_status(CodeScipStatusInput(org=org, namespace=ns, batch_id=bid))
    assert st.status == "applied"
    rows = await repo.symbols_by_name(org, ns, "tOnly")
    assert rows and str(rows[0]["tier"]) == "scip"


async def test_read_ops_and_abstain(code_env, monkeypatch) -> None:
    from server.code.api.models_search import CodeSearchInput
    from server.code.api.models_symbol import CodeCallersInput, CodeSymbolInput
    from server.code.app.search.handler import handle_code_search
    from server.code.app.symbol import handle_code_callers, handle_code_symbol
    from server.embedding.fake import fake_model_name
    from server.settings import settings

    repo, org, ns = code_env
    monkeypatch.setattr(settings, "code_org", org)
    monkeypatch.setattr(settings, "code_namespace", ns)
    monkeypatch.setattr(settings, "embedding_model", fake_model_name(settings.embedding_dim))
    await _ingest(repo, org, ns)

    # symbol-shaped -> exact route, zero embedding calls
    r = await handle_code_search(CodeSearchInput(org=org, namespace=ns, query="Squasher::flush"))
    assert r.route == "symbol_exact"
    assert r.retrieval_work["embedding_calls"] == 0
    assert r.results and r.results[0].symbol == "DB::Squasher::flush"

    # abstain fields present on the wire for hybrid queries
    r2 = await handle_code_search(
        CodeSearchInput(org=org, namespace=ns, query="terraform state locking dynamodb")
    )
    assert r2.abstain_code in {"", "no_match", "below_threshold", "low_calibrated_confidence"}
    assert r2.retrieval_risk in {"low", "medium", "high", "abstain"}
    assert r2.trace_run_id

    s = await handle_code_symbol(
        CodeSymbolInput(
            org=org,
            namespace=ns,
            name="DB::Squasher::flush",
            include_history=False,
            include_examples=False,
            include_lessons=False,
        )
    )
    assert s.definitions and s.definitions[0].body  # whole unit, not a window

    c = await handle_code_callers(
        CodeCallersInput(org=org, namespace=ns, name="DB::Squasher::squashImpl")
    )
    assert any(e.from_symbol == "DB::Squasher::flush" for e in c.edges)


async def test_deletion_tombstones_everything(code_env) -> None:
    from server.code.app.index.ingest import tombstone_file
    from server.embedding.fake import fake_model_name
    from server.settings import settings

    repo, org, ns = code_env
    model = fake_model_name(settings.embedding_dim)
    await _ingest(repo, org, ns)
    await tombstone_file(repo, org, ns, path="src/S.cpp", embedding_model=model)
    assert await repo.live_chunks_for_path(org, ns, "src/S.cpp", model) == []
    assert await repo.live_symbols_for_path(org, ns, "src/S.cpp", "treesitter") == []
    assert await repo.live_edges_for_path(org, ns, "src/S.cpp", "treesitter") == []


async def test_file_qualified_symbol_keeps_overloads_and_declines_other_files(code_env) -> None:
    from server.code.app.symbol.resolve import _resolve_symbols, near_misses

    repo, org, ns = code_env
    await _ingest(repo, org, ns)
    plain = await repo.symbols_by_name(org, ns, "Squasher::insert")
    scoped = await _resolve_symbols(repo, org, ns, symbol_id="", name="src/S.cpp:Squasher::insert")
    assert len(scoped) == 2
    assert {r["symbol_id"] for r in scoped} == {r["symbol_id"] for r in plain}
    assert all(r["resolved_by"] == "file_symbol" for r in scoped)
    assert not await _resolve_symbols(
        repo, org, ns, symbol_id="", name="src/Other.cpp:Squasher::insert"
    )
    assert not await near_misses(repo, org, ns, "src/Other.cpp:Squasher::insert")
    limited = await repo.symbols_by_name(org, ns, "insert", path="src/S.cpp", limit=1)
    assert len(limited) == 1 and limited[0]["path"] == "src/S.cpp"
