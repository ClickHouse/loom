"""A function chunk's context text may name what it calls
and who calls it. Shape of the head, the stoplist, the df cap, the chunk
bucketing of call sites, the lexemes, and the ingest wiring — with the
default off leaving every row byte-identical."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

pytest.importorskip("tree_sitter", reason="code extras not installed")

from server.code.domain.extract import callee_outline as co
from server.code.domain.extract.chunker import Chunk, chunk_file
from server.code.domain.extract.context_text import (
    MAX_CALLEES,
    MAX_CALLERS,
    build_context_text,
    filter_callees,
)
from server.code.domain.extract.edges_treesitter import call_sites
from server.code.domain.extract.treesitter import parse_cpp

_SRC = b"""#include <memory>

namespace DB
{

class Squasher
{
public:
    void flush()
    {
        LOG_DEBUG(log, "flushing");
        squashImpl();
        std::sort(rows.begin(), rows.end());
        auto p = std::make_shared<Block>();
        assert(p);
        writeBlock(*p);
    }

    void squashImpl() { work(); Poco::Logger::get("x"); }

    std::vector<int> rows;
};

void freeFunction(int x)
{
    process(x);
    more(x);
    process(x + 1);
}

}
"""


def _parsed():
    return parse_cpp("src/Interpreters/Squasher.cpp", _SRC)


# ---- context text shape ----------------------------------------------------


def test_plain_text_is_unchanged_when_no_outline_is_given() -> None:
    plain = build_context_text(path="src/A/B.cpp", scope="DB::f", signature="(int)", content="x")
    assert plain == "src/A/B.cpp · src/A\nDB::f(int)\n---\nx"
    assert (
        build_context_text(
            path="src/A/B.cpp",
            scope="DB::f",
            signature="(int)",
            content="x",
            calls=[],
            callers=[],
        )
        == plain
    ), "empty outline lists are the plain builder, byte for byte"


def test_outline_lines_sit_before_the_separator_in_order() -> None:
    text = build_context_text(
        path="src/A/B.cpp",
        scope="DB::f",
        signature="(int)",
        description="Does f.",
        content="body",
        calls=["a", "b"],
        callers=["X::g", "Y::h"],
    )
    head, _, body = text.partition("\n---\n")
    assert head.splitlines() == [
        "src/A/B.cpp · src/A",
        "DB::f(int)",
        "Does f.",
        "calls: a, b",
        "Called by: X::g, Y::h",
    ]
    assert body == "body"


def test_outline_lines_are_capped() -> None:
    text = build_context_text(
        path="p.cpp",
        scope="s",
        content="c",
        calls=[f"c{i}" for i in range(40)],
        callers=[f"k{i}" for i in range(20)],
    )
    calls_line = next(ln for ln in text.splitlines() if ln.startswith("calls: "))
    callers_line = next(ln for ln in text.splitlines() if ln.startswith("Called by: "))
    assert len(calls_line[len("calls: ") :].split(", ")) == MAX_CALLEES == 24
    assert len(callers_line[len("Called by: ") :].split(", ")) == MAX_CALLERS == 8


# ---- stoplist / df cap -----------------------------------------------------


def test_filter_callees_drops_noise_keeps_order_and_dedups() -> None:
    names = [
        "LOG_DEBUG",
        "std::sort",
        "make_shared",
        "assert",
        "writeBlock",
        "squashImpl",
        "writeBlock",
        "DB::process",
        "std::move",
        "boost::algorithm::to_lower",
        "x",
        "Poco::Logger::get",
    ]
    # `Poco::Logger::get`'s bare name is container-verb noise: dropped like `get`
    assert filter_callees(names) == ["writeBlock", "squashImpl", "process"]


def test_filter_callees_strips_template_arguments_and_drops_the_cast() -> None:
    got = filter_callees(["as<ASTSelectQuery &>", "checkAndGet<Int64>", "ColumnUInt8::create"])
    assert got == ["checkAndGet", "ColumnUInt8::create"], got


def test_filter_callees_applies_a_corpus_stoplist_on_the_bare_name() -> None:
    assert filter_callees(["aa", "Foo::bb", "cc"], stoplist=frozenset({"bb"})) == ["aa", "cc"]
    assert filter_callees([f"n{i}" for i in range(50)], limit=3) == ["n0", "n1", "n2"]


def test_df_stoplist_names_the_callees_most_units_call() -> None:
    counts = Counter({"throwException": 900, "getContext": 300, "rare": 3})
    assert co.df_stoplist(counts, 1000, ratio=0.05) == {"throwException", "getContext"}
    assert co.df_stoplist(counts, 1000, ratio=0.0) == frozenset()
    assert co.df_stoplist(counts, 0, ratio=0.05) == frozenset()


# ---- call sites bucketed into chunks ---------------------------------------


def test_call_sites_are_bucketed_into_the_chunk_that_holds_them() -> None:
    pf = _parsed()
    sites = call_sites(pf)
    assert ("LOG_DEBUG" in {n for _, n in sites}) is True, (
        "the walker keeps macros; the filter drops them"
    )
    chunks = chunk_file(pf)
    by_scope = {c.scope: c for c in chunks if c.kind == "function"}
    flush = co.callees_for_chunk(sites, by_scope["DB::Squasher::flush"])
    assert flush == ["squashImpl", "writeBlock"], flush
    free = co.callees_for_chunk(sites, by_scope["DB::freeFunction"])
    assert free == ["process", "more"], "first-seen order, deduplicated"
    impl = co.callees_for_chunk(sites, by_scope["DB::Squasher::squashImpl"])
    assert impl == ["work"], "Poco::Logger::get is dropped on its bare name"


def test_callers_for_chunk_excludes_the_unit_itself_and_caps() -> None:
    c = Chunk(
        path="p",
        scope="DB::A::f",
        kind="function",
        start_line=1,
        end_line=2,
        content="c",
        symbol_ids=["s1"],
    )
    by_scope = {"DB::A::f": ["A::f", "B::g", *(f"K{i}::k" for i in range(12))]}
    got = co.callers_for_chunk(c, by_scope)
    assert "A::f" not in got and got[0] == "B::g" and len(got) == MAX_CALLERS
    assert (
        co.callers_for_chunk(
            Chunk(
                path="p", scope="DB::Z::z", kind="function", start_line=1, end_line=1, content="c"
            ),
            by_scope,
        )
        == []
    )


@pytest.mark.asyncio
async def test_callers_for_chunks_resolves_every_tier_then_reads_one_caller_hop() -> None:
    calls: list[dict[str, Any]] = []

    class _Repo:
        async def symbol_ids_for_names(self, org, ns, names):
            calls.append({"names": names, "ns": ns})
            return {"DB::A::f": ["s1", "scip1"]}

        async def edges_for_symbols(self, org, ns, ids, **kw):
            calls.append({"org": org, "ns": ns, "ids": ids, **kw})
            return [
                {"to_qualified_name": "DB::A::f", "from_qualified_name": "DB::X::y"},
                {
                    "to_qualified_name": "DB::A::f",
                    "from_qualified_name": "DB::(anonymous namespace)::z",
                },
                {"to_qualified_name": "DB::A::f", "from_qualified_name": "DB::A::b"},
                {"to_qualified_name": "DB::A::f", "from_qualified_name": "DB::A::f"},
                {"to_qualified_name": "DB::Q::q", "from_qualified_name": "DB::Q::caller"},
            ]

    c = Chunk(
        path="p",
        scope="DB::A::f",
        kind="function",
        start_line=1,
        end_line=1,
        content="c",
        symbol_ids=["s1"],
    )
    other = Chunk(
        path="p", scope="o", kind="other", start_line=2, end_line=2, content="c", symbol_ids=["s5"]
    )
    got = await co.callers_for_chunks(_Repo(), "o", "live-ns", [c, other])
    assert got == {"DB::A::f": ["A::b", "X::y"], "DB::Q::q": ["Q::caller"]}
    assert calls[0] == {"names": ["DB::A::f"], "ns": "live-ns"}, "function scopes only"
    assert calls[1] == {
        "org": "o",
        "ns": "live-ns",
        "ids": ["s1", "scip1"],
        "direction": "callers",
        "min_confidence": co.CALLER_MIN_CONFIDENCE,
        "per_match": co._CALLER_ROWS_PER_SYMBOL,
        "limit": co._CALLER_ROWS_LIMIT,
    }, "the parser id AND the compiler id, one read, callers direction"
    assert co.callers_for_chunk(c, got) == ["A::b", "X::y"]


# ---- lexemes ---------------------------------------------------------------


def test_extra_lexemes_reach_the_lexical_leg_ahead_of_the_body() -> None:
    c = Chunk(
        path="src/A.cpp",
        scope="DB::f",
        kind="function",
        start_line=1,
        end_line=1,
        content="int f() { return 1; }",
    )
    plain = c.lexemes
    c.extra_lexemes = "QueryPipelineBuilder::addTransform"
    assert c.lexemes != plain
    toks = c.lexemes.split()
    assert "querypipelinebuilder" in toks and "addtransform" in toks
    assert toks.index("addtransform") < toks.index("return"), "extras precede the body"
    c.extra_lexemes = ""
    assert c.lexemes == plain, "no extras: byte-identical to before"


# ---- ingest wiring ---------------------------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.inserted: dict[str, list[dict[str, Any]]] = {}
        self.edge_calls: list[tuple[str, str]] = []

    async def live_chunk_index_for_path(self, *a, **k):
        return []

    async def chunks_full_by_ids(self, *a, **k):
        return []

    async def insert_rows(self, table, rows):
        self.inserted.setdefault(table, []).extend(rows)

    async def live_symbols_for_path(self, *a, **k):
        return []

    async def name_candidates(self, *a, **k):
        return {}

    async def live_edges_for_path(self, *a, **k):
        return []

    async def live_call_misses_for_path(self, *a, **k):
        return []

    async def live_files_for_path(self, *a, **k):
        return []

    async def symbol_ids_for_names(self, org, ns, names):
        return {n: [f"scip-{n}"] for n in names}

    async def edges_for_symbols(self, org, ns, ids, **kw):
        self.edge_calls.append((org, ns))
        return [
            {"to_qualified_name": "DB::Squasher::flush", "from_qualified_name": "DB::Caller::run"}
        ]


async def _ingest(repo: _Repo, **kw: Any) -> list[dict[str, Any]]:
    from server.code.app.index.ingest import ingest_file

    await ingest_file(
        repo,
        "o",
        "n",
        path="src/Interpreters/Squasher.cpp",
        source=_SRC,
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=None,
        **kw,
    )
    return repo.inserted["code_chunks"]


@pytest.mark.asyncio
async def test_default_off_writes_the_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo()
    rows = await _ingest(repo)
    fn = [r for r in rows if r["kind"] == "function"]
    assert fn and all("calls:" not in r["context_text"] for r in fn)
    assert all("Called by:" not in r["context_text"] for r in fn)
    assert repo.edge_calls == [], "no caller read when the outline is off"
    flush = next(r for r in fn if r["scope"] == "DB::Squasher::flush")
    assert flush["context_text"] == build_context_text(
        path="src/Interpreters/Squasher.cpp",
        scope="DB::Squasher::flush",
        signature="()",
        content=flush["content"],
    )


@pytest.mark.asyncio
async def test_calls_mode_writes_the_outline_and_lexemes(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo()
    rows = await _ingest(repo, context_callees="calls")
    flush = next(r for r in rows if r["scope"] == "DB::Squasher::flush")
    head = flush["context_text"].split("\n---\n")[0].splitlines()
    assert head[-1] == "calls: squashImpl, writeBlock", head
    assert "Called by:" not in flush["context_text"]
    assert repo.edge_calls == []
    other = [r for r in rows if r["kind"] != "function"]
    assert other and all("calls:" not in r["context_text"] for r in other), "functions only"


@pytest.mark.asyncio
async def test_calls_callers_mode_reads_callers_from_the_named_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    rows = await _ingest(
        repo,
        context_callees="calls_callers",
        callers_namespace="live-ns",
        callee_stoplist=frozenset({"writeBlock"}),
    )
    flush = next(r for r in rows if r["scope"] == "DB::Squasher::flush")
    head = flush["context_text"].split("\n---\n")[0].splitlines()
    assert head[-2:] == ["calls: squashImpl", "Called by: Caller::run"], head
    assert repo.edge_calls == [("o", "live-ns")], "one caller read per file, other namespace"
    toks = flush["lexemes"].split()
    assert "caller" in toks and "run" in toks, "caller names enter the lexemes"
