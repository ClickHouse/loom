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


async def _ingest(repo, org, ns, source=_SRC, sha="a" * 40, *, path="src/S.cpp"):
    from server.code.app.index.ingest import ingest_file
    from server.embedding.fake import FakeNGramEmbedder
    from server.settings import settings

    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    return await ingest_file(
        repo,
        org,
        ns,
        path=path,
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


async def test_read_returns_exact_indexed_ranges(code_env, monkeypatch) -> None:
    from server.code.api.models_read import CodeReadInput
    from server.code.app import read
    from server.settings import settings

    repo, org, ns = code_env
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    await _ingest(repo, org, ns)

    async def mirror(_cfg):
        return "local-mirror"

    async def show_file(_directory, _revision, _path, *, max_bytes):
        return _SRC

    monkeypatch.setattr(read, "present_dir", mirror)
    monkeypatch.setattr(read.gitrepo, "show_file", show_file)
    response = await read.handle_code_read(
        CodeReadInput(
            org=org,
            namespace=ns,
            ranges=["src/S.cpp:5-6", "src/S.cpp:10", "src/missing.cpp:1", "src/S.cpp:40"],
        )
    )
    assert [r.status for r in response.results] == [
        "complete",
        "complete",
        "not_indexed",
        "out_of_range",
    ]
    source_lines = _SRC.decode().splitlines(keepends=True)
    for result in response.results[:2]:
        assert result.indexed_commit == "a" * 40
        assert result.windows[0].text == "".join(
            source_lines[result.start_line - 1 : result.end_line]
        )
    single = await read.handle_code_read(
        CodeReadInput(org=org, namespace=ns, path="src/S.cpp", start_line=5, end_line=6)
    )
    assert single.results == response.results[:1]


async def test_read_verifies_mirror_blob_after_incremental_ingest(code_env, monkeypatch) -> None:
    from server.code.api.models_read import CodeReadInput
    from server.code.app import read
    from server.settings import settings

    repo, org, ns = code_env
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    commit = "b" * 40
    current = _SRC.replace(b"squashImpl();", b"doWork();")
    await _ingest(repo, org, ns)
    await _ingest(repo, org, ns, source=current, sha=commit)
    calls = []

    async def mirror(_cfg):
        return "local-mirror"

    async def show_file(git_dir, revision, path, *, max_bytes):
        calls.append((git_dir, revision, path))
        return current

    monkeypatch.setattr(read, "present_dir", mirror)
    monkeypatch.setattr(read.gitrepo, "show_file", show_file)
    response = await read.handle_code_read(
        CodeReadInput(org=org, namespace=ns, ranges=["src/S.cpp:10", "src/S.cpp:5-10"])
    )
    lines = current.decode().splitlines(keepends=True)
    assert [r.status for r in response.results] == ["complete", "complete"]
    assert response.results[0].windows[0].text == lines[9]
    assert response.results[1].windows[0].text == "".join(lines[4:10])
    assert calls == [("local-mirror", commit, "src/S.cpp")]
    record = (await repo.live_files_for_path(org, ns, "src/S.cpp"))[0]
    await repo.insert_rows("code_files", [repo.moved_of("code_files", record, chunk_count=0)])
    calls.clear()
    excluded = await read.handle_code_read(
        CodeReadInput(org=org, namespace=ns, ranges=["src/S.cpp:10"])
    )
    assert excluded.results[0].status == "unavailable"
    assert not excluded.results[0].windows and not calls


async def test_grep_returns_distinct_retained_enclosing_bodies_by_default(
    ch_client, monkeypatch
) -> None:
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
    plain = await handle_code_grep(
        CodeGrepInput(org=org, namespace=ns, pattern="squashImpl", include_body=False)
    )
    rich = await handle_code_grep(CodeGrepInput(org=org, namespace=ns, pattern="squashImpl"))
    assert rich.matches == plain.matches and rich.matches
    assert not plain.definitions
    assert not rich.definitions_partial
    assert len(rich.definitions) == len({row["symbol_id"] for row in rich.definitions}) == 2
    assert all("squashImpl" in row["body"] and not row["body_elided"] for row in rich.definitions)
    nearby = await handle_code_grep(
        CodeGrepInput(
            org=org, namespace=ns, pattern="squashImpl", context_lines=1, include_body=False
        )
    )
    source_lines = _SRC.decode().splitlines(keepends=True)
    assert nearby.matches == plain.matches and nearby.contexts and not nearby.contexts_partial
    assert not nearby.definitions
    for window in nearby.contexts:
        assert window["text"] == "".join(
            source_lines[window["start_line"] - 1 : window["end_line"]]
        )

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

    async def bodies(input, matches, budget, *, match_commits):
        hydrated.extend((row["path"], row["line"]) for row in matches)
        return await original(input, matches, budget, match_commits=match_commits)

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
    from server.code.domain.units import _card_of, estimate_unit_tokens
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

    query = CodeSymbolInput(
        org=org,
        namespace=ns,
        name="DB::Squasher::squashImpl",
        include_history=False,
        include_lessons=False,
    )
    plain_response = await handle_code_symbol(query)
    plain = plain_response.definitions[0].examples
    examples = await handle_code_symbol(query.model_copy(update={"example_context_lines": 2}))
    sites = examples.definitions[0].examples
    assert plain and [{k: v for k, v in site.items() if k != "context"} for site in sites] == plain
    site = next(site for site in sites if site["caller"] == "DB::Squasher::flush")
    context = site["context"]
    assert context["commit"] == "a" * 40
    assert context["start_line"] <= site["line"] <= context["end_line"]
    assert context["text"].splitlines()[site["line"] - context["start_line"]].strip() == (
        "void flush() { squashImpl(); }"
    )
    plain_definition = plain_response.definitions[0]
    budget = max(
        estimate_unit_tokens(plain_definition), estimate_unit_tokens(_card_of(plain_definition))
    )
    limited = await handle_code_symbol(
        query.model_copy(update={"example_context_lines": 2, "token_budget": budget})
    )
    assert limited.definitions[0].body == plain_definition.body
    assert limited.definitions[0].examples == plain


async def test_symbol_reads_test_examples_with_the_definition(code_env, monkeypatch) -> None:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import handle_code_symbol
    from server.embedding.fake import fake_model_name
    from server.settings import settings

    repo, org, ns = code_env
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "embedding_model", fake_model_name(settings.embedding_dim))
    await _ingest(repo, org, ns)
    call = "void test_work() { DB::doWork(); }"
    path = "test/test_work.cpp"
    await _ingest(repo, org, ns, source=(call + "\n").encode(), path=path)
    result = await handle_code_symbol(
        CodeSymbolInput(
            org=org,
            namespace=ns,
            name="DB::doWork",
            test_examples_only=True,
            example_context_lines=2,
            include_history=False,
            include_lessons=False,
        )
    )
    (definition,) = result.definitions
    assert definition.body.strip() == "void doWork() { }"
    (example,) = definition.examples
    assert example["path"] == path
    assert example["context"]["text"].strip() == call


async def test_symbol_reads_a_test_that_passes_a_class_to_a_decorator(
    code_env, monkeypatch
) -> None:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import handle_code_symbol
    from server.embedding.fake import fake_model_name
    from server.settings import settings

    repo, org, ns = code_env
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "embedding_model", fake_model_name(settings.embedding_dim))
    await _ingest(repo, org, ns, source=b"class EntryPoint:\n    pass\n", path="src/cli.py")
    path = "tests/test_cli.py"
    source = b"from src.cli import EntryPoint\n\ndef test_entrypoint():\n    @register(cls=EntryPoint)\n    def cli():\n        pass\n"
    await _ingest(repo, org, ns, source=source, path=path)
    response = await handle_code_symbol(
        CodeSymbolInput(
            org=org,
            namespace=ns,
            name="src/cli.py:EntryPoint",
            test_examples_only=True,
            example_context_lines=2,
            include_history=False,
            include_lessons=False,
        )
    )
    (example,) = response.definitions[0].examples
    assert (example["path"], example["line"], example["evidence"]) == (path, 4, "name_reference")
    assert example["context"]["commit"] == "a" * 40
    assert "@register(cls=EntryPoint)" in example["context"]["text"]


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
    source = _SRC.replace(
        b"namespace DB {\n", b"namespace DB {\nclass Other { public: void insert(int v) {} };\n"
    )
    await _ingest(repo, org, ns, source=source)
    plain = await repo.symbols_by_name(org, ns, "Squasher::insert")
    scoped = await _resolve_symbols(repo, org, ns, symbol_id="", name="src/S.cpp:Squasher::insert")
    assert len(scoped) == 2
    assert {r["symbol_id"] for r in scoped} == {r["symbol_id"] for r in plain}
    assert all(r["resolved_by"] == "file_symbol" for r in scoped)
    assert not await _resolve_symbols(
        repo, org, ns, symbol_id="", name="src/Other.cpp:Squasher::insert"
    )
    assert not await near_misses(repo, org, ns, "src/Other.cpp:Squasher::insert")
    close = await near_misses(repo, org, ns, "src/S.cpp:Squasher::inset")
    assert close[0]["qualified_name"].endswith("Squasher::insert")
    assert all(row["path"] == "src/S.cpp" for row in close)
    unqualified = await near_misses(repo, org, ns, "src/S.cpp:inset")
    names = {row["qualified_name"] for row in unqualified}
    assert any(name.endswith("Squasher::insert") for name in names)
    assert any(name.endswith("Other::insert") for name in names)
    limited = await repo.symbols_by_name(org, ns, "insert", path="src/S.cpp", limit=1)
    assert len(limited) == 1 and limited[0]["path"] == "src/S.cpp"
