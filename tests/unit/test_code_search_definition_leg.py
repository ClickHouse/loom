"""code.search definition leg: the names a query spells out as code are read from the
symbol table, and the chunks that define the first few join the fusion as one more
reciprocal-rank list. A plain word is never looked up, an ambiguous name is dropped,
and confidence is calibrated without the leg."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import search as cs
from server.code.app.search.definitions import (
    _MAX_CANDIDATES,
    _MAX_DEFINITIONS,
    _SYMBOLS_PER_NAME,
    named_definitions,
)
from server.code.app.search.fusion import _extra_via_labels, _rrf_fuse
from server.code.domain.idents import SpelledName, spelled_names
from server.code.domain.search_scope import build_scope
from server.code.store.index import CodeRepo
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl, view

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")


def _names(query: str, limit: int = 32) -> list[tuple[str, bool]]:
    return [(".".join(n.parts), n.member_alone) for n in spelled_names(query, limit)]


# ---- the names a query spells out ---------------------------------------------------


def test_a_called_backticked_qualified_or_code_shaped_name_is_spelled_out() -> None:
    assert _names("timesince() is off by one month") == [("timesince", True)]
    assert _names("how does `Field` validate") == [("Field", True)]
    assert _names("why is bulk_update slow") == [("bulk_update", True)]
    assert _names("who calls executeStep") == [("executeStep", True)]
    assert _names("SQLCompiler and S3Queue and _resolve") == [
        ("SQLCompiler", True),
        ("S3Queue", True),
        ("_resolve", True),
    ]
    assert _names("PipelineExecutor::executeStep hangs") == [("PipelineExecutor.executeStep", True)]
    # a qualified spelling whose member is a plain word names a definition only as written
    assert _names("Query.combine drops an alias") == [("Query.combine", False)]
    assert _names("see pkg.db.models") == [("pkg.db.models", False)]
    # a call on an object or on an expression still names the method
    assert _names("self.check_filterable(value) raises") == [("self.check_filterable", True)]
    assert _names("qs.filter(a=1).values_list('x')") == [("qs.filter", True), ("values_list", True)]


def test_a_plain_word_is_never_a_name() -> None:
    assert _names("how does the cache handle a timeout during get") == []
    assert _names("Field validation runs before the Model is saved") == []
    # a plural marker is not a call, a path component is not a name, one letter is not one
    assert _names("which model(s) and class(es) define it") == []
    assert _names("in pkg/db/models/query.py or tests/test_bulk_update.py") == []
    assert _names("see https://example.com/blob/abc/query_utils.py#L3") == []
    assert _names("f(x) and e.g. this") == []
    # an acronym or its plural is not code-shaped
    assert _names("the HTTP URLs and their IDs") == []


def test_names_come_in_reading_order_once_each_and_stop_at_the_limit() -> None:
    query = "bulk_update() then save_base then bulk_update again and `bulk_update`"
    assert _names(query) == [("bulk_update", True), ("save_base", True)]
    assert _names("a_b c_d e_f g_h", limit=2) == [("a_b", True), ("c_d", True)]
    # a later mention can show the member stands by itself
    assert _names("Query.combine fails; Query.combine() too") == [("Query.combine", True)]


# ---- the definitions read for them --------------------------------------------------


def _sym(name: str, qualified: str, path: str) -> dict[str, Any]:
    return {"name": name, "qualified_name": qualified, "path": path}


_SYMBOLS = [
    _sym("bulk_update", "pkg.query::QuerySet::bulk_update", "pkg/query.py"),
    _sym("bulk_update", "pkg.state::StateApps::bulk_update", "pkg/state.py"),
    _sym("bulk_update", "tests.q::Helper::bulk_update", "tests/q.py"),
    _sym("combine", "pkg.query::Query::combine", "pkg/query.py"),
    _sym("combine", "pkg.tree::Q::combine", "pkg/tree.py"),
    _sym("test_combine", "tests.q::T::test_combine", "tests/q.py"),
    *[_sym("save", f"pkg.m{i}::C{i}::save", f"pkg/m{i}.py") for i in range(4)],
]


def _name(spelling: str, alone: bool = True) -> SpelledName:
    return SpelledName(tuple(spelling.split(".")), alone)


def test_a_name_keeps_its_definitions_outside_tests_or_its_test_ones() -> None:
    assert named_definitions([_name("bulk_update")], _SYMBOLS) == [
        ("pkg/query.py", "pkg.query::QuerySet::bulk_update"),
        ("pkg/state.py", "pkg.state::StateApps::bulk_update"),
    ]
    assert named_definitions([_name("test_combine")], _SYMBOLS) == [
        ("tests/q.py", "tests.q::T::test_combine")
    ]


def test_a_name_defined_in_too_many_places_is_dropped() -> None:
    assert named_definitions([_name("save")], _SYMBOLS) == []
    # as is one whose read stopped at its bound, whatever paths it returned
    many = [_sym("get", f"tests.t{i}::get", f"tests/t{i}.py") for i in range(_SYMBOLS_PER_NAME)]
    assert named_definitions([_name("get")], [*many, _sym("get", "pkg.a::get", "pkg/a.py")]) == []


def test_a_qualified_spelling_names_only_its_own_definition() -> None:
    assert named_definitions([_name("Query.combine", alone=False)], _SYMBOLS) == [
        ("pkg/query.py", "pkg.query::Query::combine")
    ]
    # a module path qualifies too: its dots separate like `::`
    assert named_definitions([_name("query.QuerySet.bulk_update")], _SYMBOLS) == [
        ("pkg/query.py", "pkg.query::QuerySet::bulk_update")
    ]
    # an owner that is not one (`self.combine()`) falls back to the member by itself
    assert named_definitions([_name("self.combine")], _SYMBOLS) == [
        ("pkg/query.py", "pkg.query::Query::combine"),
        ("pkg/tree.py", "pkg.tree::Q::combine"),
    ]
    # but not when the member is a plain word: `models.combine` names nothing
    assert named_definitions([_name("models.combine", alone=False)], _SYMBOLS) == []


def test_definitions_follow_the_query_order_within_the_prefix_and_the_bound() -> None:
    names = [_name("combine"), _name("bulk_update"), _name("combine")]
    assert [d[1] for d in named_definitions(names, _SYMBOLS)] == [
        "pkg.query::Query::combine",
        "pkg.tree::Q::combine",
        "pkg.query::QuerySet::bulk_update",
        "pkg.state::StateApps::bulk_update",
    ]
    # under a path prefix a name is counted, and served, inside it
    assert named_definitions([_name("save")], _SYMBOLS, path_prefix="pkg/m1") == [
        ("pkg/m1.py", "pkg.m1::C1::save")
    ]
    wide = [_sym(f"f_{i}", f"pkg.w::f_{i}", "pkg/w.py") for i in range(_MAX_CANDIDATES + 5)]
    got = named_definitions([_name(f"f_{i}") for i in range(_MAX_CANDIDATES + 5)], wide)
    assert [d[1] for d in got] == [f"pkg.w::f_{i}" for i in range(_MAX_CANDIDATES)]


# ---- the bounded read on a re-indexed table -----------------------------------------

needs_clickhouse = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _reindexed(live: list[dict[str, Any]], dead: list[dict[str, Any]]) -> CodeRepo:
    """A symbol table after re-indexing and its merges: the `live` definitions, and the
    `dead` ones as the name table keeps them, one row per key whose newest version is
    a tombstone."""

    def write(rows: list[dict[str, Any]], version: int, tombstoned_at: str) -> str:
        values = ", ".join(
            f"('o', 'n', 'treesitter', '{r['qualified_name']}', '{r['symbol_id']}', "
            f"'{r['name']}', '{r['path']}', {tombstoned_at}, {version})"
            for r in rows
        )
        return (
            "INSERT INTO loom.code_symbols (org, namespace, tier, qualified_name, symbol_id, "
            f"name, path, tombstoned_at, version) VALUES {values}"
        )

    return CodeRepo(  # type: ignore[arg-type]
        LocalCH(
            ddl("code_symbols"),
            ddl("code_symbol_names"),
            view("code_symbol_names_mv"),
            write([*live, *dead], 1, "NULL"),
            *([write(dead, 2, "now()")] if dead else []),
            "OPTIMIZE TABLE loom.code_symbol_names FINAL",
        )
    )


def _key(name: str, qualified: str, path: str, symbol_id: str = "") -> dict[str, Any]:
    return {**_sym(name, qualified, path), "symbol_id": symbol_id or f"id-{qualified}"}


@needs_clickhouse
@pytest.mark.asyncio
async def test_the_read_bound_counts_live_definitions_not_name_table_rows() -> None:
    # 33 live definitions, five of them outside tests: the name says nothing about where
    live = [_key("helper", f"aa_tests.t{i:02d}::helper", f"tests/t{i:02d}.py") for i in range(28)]
    live += [_key("helper", f"pkg.m{i}::helper", f"pkg/m{i}.py") for i in range(5)]
    # a dead key that sorts first must not take one of the places the bound counts
    dead = [_key("helper", "aa_gone::helper", "pkg/gone.py")]
    for repo in (_reindexed(live, []), _reindexed(live, dead)):
        rows = await repo.symbols_named("o", "n", ["helper"], per_name=_SYMBOLS_PER_NAME)
        assert len(rows) == _SYMBOLS_PER_NAME
        assert all(r["path"] != "pkg/gone.py" for r in rows)
        assert named_definitions([_name("helper")], rows) == []


@needs_clickhouse
@pytest.mark.asyncio
async def test_dead_keys_of_a_name_do_not_hide_its_live_definition() -> None:
    # a signature change writes a new key and tombstones the old one
    combine = ("combine", "pkg.query::Query::combine", "pkg/query.py")
    dead = [_key(*combine, symbol_id=f"old-{i:02d}") for i in range(_SYMBOLS_PER_NAME)]
    repo = _reindexed([_key(*combine, symbol_id="zz-live")], dead)
    rows = await repo.symbols_named("o", "n", ["combine"], per_name=_SYMBOLS_PER_NAME)
    assert rows == [_sym(*combine)]
    assert named_definitions([_name("combine")], rows) == [("pkg/query.py", combine[1])]


# ---- fusion -------------------------------------------------------------------------


def _row(cid: str, **extra: Any) -> dict[str, Any]:
    return {"chunk_id": cid, "path": f"src/{cid}.cpp", **extra}


def test_the_definition_list_is_an_uncapped_reciprocal_rank_list() -> None:
    lex, vec = [_row("a"), _row("b")], [_row("c"), _row("a")]
    assert _rrf_fuse(lex, vec, definition_rows=[]) == _rrf_fuse(lex, vec)
    fused = dict((r["chunk_id"], s) for r, s in _rrf_fuse(lex, vec, definition_rows=[_row("b")]))
    # b: lexical #2 and the first named definition, level with a: lexical #1 + vector #2
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61) == pytest.approx(fused["a"])
    assert fused["b"] > fused["c"]
    only = dict((r["chunk_id"], s) for r, s in _rrf_fuse(lex, vec, definition_rows=[_row("d")]))
    # one neither content leg found scores as a content leg's first hit
    assert only["d"] == pytest.approx(1 / 61) == pytest.approx(only["c"])
    # the capped lists' ceiling is still the two content legs' top
    capped = _rrf_fuse(lex, vec, [_row("h")], history_cap=0.25, definition_rows=[_row("b")])
    assert dict((r["chunk_id"], s) for r, s in capped)["h"] == pytest.approx(
        0.25 * (1 / 61 + 1 / 62)
    )


def test_a_unit_only_the_definition_leg_found_says_so() -> None:
    prov = {"a": ("lexical", 1, 0)}
    labels = _extra_via_labels(prov, [_row("d")], [], [], [], [_row("a"), _row("d")])
    assert labels == {"d": "definition"}


# ---- the leg inside search() --------------------------------------------------------

_UNIT = dict(
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)


class _Repo:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kw: dict[str, Any] = {}

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [
            {"chunk_id": "a", "lex_score": 4.0, "path": "src/a.cpp", "scope": "f", **_UNIT},
            {"chunk_id": "b", "lex_score": 1.0, "path": "src/b.cpp", "scope": "g", **_UNIT},
        ]

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [
            {"chunk_id": "c", "distance": 0.1, "path": "src/c.cpp", "scope": "h", **_UNIT},
            {"chunk_id": "a", "distance": 0.2, "path": "src/a.cpp", "scope": "f", **_UNIT},
        ]

    async def symbols_named(
        self, org: str, ns: str, names: list[str], *, per_name: int
    ) -> list[dict[str, Any]]:
        self.calls.append("symbols_named")
        self.kw["names"] = names
        return [
            _sym("parse_quota", "DB::parse_quota", "src/d.cpp"),
            _sym("parse_quota", "DB::Other::parse_quota", "src/other.cpp"),
            _sym("load_quota", "DB::load_quota", "src/b.cpp"),
        ]

    async def definition_chunks(
        self, org: str, ns: str, model: str, definitions: list[tuple[str, str]], **k: Any
    ) -> list[dict[str, Any]]:
        self.calls.append("definition_chunks")
        self.kw["definitions"], self.kw["scope"] = definitions, k["scope"]
        return [
            # the two IN lists also admit a name defined in another listed file
            {"chunk_id": "x", "path": "src/b.cpp", "scope": "DB::parse_quota", **_UNIT},
            {"chunk_id": "b", "path": "src/b.cpp", "scope": "DB::load_quota", **_UNIT},
            {"chunk_id": "d", "path": "src/d.cpp", "scope": "DB::parse_quota", **_UNIT},
        ]

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    async def scope_file_count(self, *a: Any, **k: Any) -> int:
        return 3


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _search(repo: _Repo, query: str, *, top_k: int = 5, **kw: Any) -> Any:
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_pr_leg", "off"),
    ):
        return asyncio.run(
            cs.search(repo, "o", "ns", query=query, top_k=top_k, embedder=_Embedder(), **kw)
        )


def test_a_query_of_plain_words_adds_no_reads() -> None:
    repo = _Repo()
    res = _search(repo, "how does quota enforcement work")
    assert repo.calls == [] and "named_definitions" not in res.score_explanation
    assert [u.unit_id for u in res.units] == ["a", "c", "b"]


def test_named_definitions_join_the_fusion_in_the_order_the_query_names_them() -> None:
    plain = _search(_Repo(), "how does quota enforcement work")
    repo = _Repo()
    res = _search(repo, "why does parse_quota() ignore what load_quota returns")
    assert repo.calls == ["symbols_named", "definition_chunks"]
    assert repo.kw["names"] == ["load_quota", "parse_quota"]
    assert repo.kw["definitions"] == [
        ("src/d.cpp", "DB::parse_quota"),
        ("src/other.cpp", "DB::Other::parse_quota"),
        ("src/b.cpp", "DB::load_quota"),
    ]
    assert repo.kw["scope"] is None
    # b: lexical #2 and a named definition, now above vector-only c; d, which only
    # the definition leg found, enters level with c, a content leg's first hit
    assert [u.unit_id for u in plain.units] == ["a", "c", "b"]
    assert [u.unit_id for u in res.units] == ["a", "b", "c", "d"]
    assert {u.unit_id: u.score for u in res.units}["d"] == pytest.approx(1 / 61, abs=1e-4)
    via = {u.unit_id: u.via for u in res.units}
    assert via["d"] == "definition" and via["b"] == "lexical"
    assert "named_definitions=2" in res.score_explanation
    assert res.retrieval_work["named_definitions"] == 2
    # confidence and abstention are read off the other legs
    assert res.retrieval_confidence == plain.retrieval_confidence
    assert (res.abstain, res.retrieval_risk) == (plain.abstain, plain.retrieval_risk)
    assert res.top_similarity == plain.top_similarity


def test_the_leg_serves_the_first_few_definitions() -> None:
    class _Many(_Repo):
        async def symbols_named(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return [_sym(f"fn_{i}", f"DB::fn_{i}", "src/m.cpp") for i in range(6)]

        async def definition_chunks(
            self, org: str, ns: str, model: str, definitions: list[tuple[str, str]], **k: Any
        ) -> list[dict[str, Any]]:
            return [
                {"chunk_id": f"m{i}", "path": "src/m.cpp", "scope": f"DB::fn_{i}", **_UNIT}
                for i in reversed(range(1, 6))  # fn_0 has no chunk of its own
            ]

    res = _search(_Many(), "fn_0 fn_1 fn_2 fn_3 fn_4 fn_5 all fail", top_k=10)
    served = [u.unit_id for u in res.units if u.via == "definition"]
    assert served == ["m1", "m2", "m3"] and len(served) == _MAX_DEFINITIONS


def test_a_vector_only_run_skips_the_leg_and_a_constraint_reaches_its_chunk_read() -> None:
    repo = _Repo()
    _search(repo, "why does parse_quota() fail", legs="vector")
    assert repo.calls == []
    repo = _Repo()
    scope = build_scope(path_globs=["src/**"])
    _search(repo, "why does parse_quota() fail", scope=scope)
    assert repo.kw["scope"] is scope


def test_a_failed_read_degrades_to_the_other_legs() -> None:
    class _Broken(_Repo):
        async def symbols_named(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("table code_symbol_names does not exist")

    res = _search(_Broken(), "why does parse_quota() fail")
    assert [u.unit_id for u in res.units] == ["a", "c", "b"]
    assert "named_definitions" not in res.retrieval_work
