"""code.search definition leg: the names a query spells out as code are read from the
symbol table, and the chunks that define the first one are served directly after the
first unit of the ranking, never above it. A plain word is never looked up, an owner is
kept as written, an ambiguous name is dropped, and the fusion, every stage that orders
the ranking and the confidence of the other units are those of the same search without
the leg."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import search as cs
from server.code.app.search.definitions import (
    _MAX_NAMES,
    _SYMBOLS_PER_NAME,
    _held_definitions,
    _place_definitions,
    named_definitions,
)
from server.code.domain.idents import spelled_names
from server.code.domain.search_scope import build_scope
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.core.datetimes import naive_utc_now
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl, view

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")


def _names(query: str, limit: int = 32) -> list[str]:
    return [".".join(name) for name in spelled_names(query, limit)]


# ---- the names a query spells out ---------------------------------------------------


def test_a_called_backticked_qualified_or_code_shaped_name_is_spelled_out() -> None:
    assert _names("timesince() is off by one month") == ["timesince"]
    assert _names("how does `Field` validate") == ["Field"]
    assert _names("why is bulk_update slow") == ["bulk_update"]
    assert _names("who calls executeStep") == ["executeStep"]
    assert _names("SQLCompiler and S3Queue and _resolve") == ["SQLCompiler", "S3Queue", "_resolve"]
    assert _names("PipelineExecutor::executeStep hangs") == ["PipelineExecutor.executeStep"]
    # a qualified spelling counts whatever its member looks like
    assert _names("Query.combine drops an alias") == ["Query.combine"]
    assert _names("see pkg.db.models") == ["pkg.db.models"]
    assert _names("parser->_actions is empty") == ["parser._actions"]


def test_a_receiver_is_no_owner_and_any_other_owner_stays_as_written() -> None:
    assert _names("self.check_filterable(value) raises") == ["check_filterable"]
    assert _names("cls.from_db() then this.fooBar() then super.save_base()") == [
        "from_db",
        "fooBar",
        "save_base",
    ]
    assert _names("`self.save`, Self::build()") == ["save", "build"]
    # the member behind a receiver is a name only as a bare word would be
    assert _names("self.cache is stale and self.query.add_q() fails") == ["query.add_q"]
    # and a receiver is no name by itself, called or backticked
    assert _names("super() or cls() or `self`") == []
    # a library module, a variable or a class is an owner, not a receiver
    assert _names("json.dumps(payload) and qs.bulk_update(objs)") == [
        "json.dumps",
        "qs.bulk_update",
    ]
    assert _names("os.path.join() or ModelAdmin.lookup_allowed()") == [
        "os.path.join",
        "ModelAdmin.lookup_allowed",
    ]
    assert _names("this->executeStep() and self->flush_all()") == ["executeStep", "flush_all"]


def test_a_member_of_something_that_is_not_a_name_is_no_name() -> None:
    # the owner is a call, a subscript or a template: the text does not spell it
    assert _names("qs.filter(a=1).values_list('x')") == ["qs.filter"]
    assert _names("get_parser()._actions and parsers[0]._actions") == ["get_parser"]
    assert _names("requests.get(url).raise_for_status()") == ["requests.get"]
    assert _names("rows[0]->flush_all() or Foo<T>::do_work()") == []
    assert _names("super().save_base() and cls().from_db()") == []
    assert _names("call .save_base() on it") == []


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


def test_a_slash_between_two_names_separates_alternatives_and_a_path_names_nothing() -> None:
    assert _names("select_related/prefetch_related is slow") == [
        "select_related",
        "prefetch_related",
    ]
    assert _names("bulk_create()/bulk_update() ignore it") == ["bulk_create", "bulk_update"]
    assert _names("`QuerySet`/`Manager` and obj.save_all()/obj.drop_all().") == [
        "QuerySet",
        "Manager",
        "obj.save_all",
        "obj.drop_all",
    ]
    # plain words stay plain on either side of a slash
    assert _names("and/or input/output GET/POST") == []
    # a path: a second slash, a file extension after the slash, or a slash at the edge
    assert _names("under pkg/db/query_utils or pkg/query_utils.py:12") == []
    assert _names("in the test_utils/ directory, not /query_utils") == []
    assert _names("http://localhost:8000/admin_site") == []
    # which a path elsewhere in the query does not make of an alternative
    assert _names("pkg/db/query_utils then save_all/drop_all") == ["save_all", "drop_all"]


def test_names_come_in_reading_order_once_each_and_stop_at_the_limit() -> None:
    query = "bulk_update() then save_base then self.bulk_update again and `bulk_update`"
    assert _names(query) == ["bulk_update", "save_base"]
    assert _names("a_b c_d e_f g_h", limit=2) == ["a_b", "c_d"]


# ---- the definitions read for them --------------------------------------------------


def _sym(name: str, qualified: str, path: str) -> dict[str, Any]:
    return {"name": name, "qualified_name": qualified, "path": path}


_SYMBOLS = [
    _sym("bulk_update", "pkg.query::QuerySet::bulk_update", "pkg/query.py"),
    _sym("bulk_update", "pkg.state::StateApps::bulk_update", "pkg/state.py"),
    _sym("bulk_update", "tests.q::Helper::bulk_update", "tests/q.py"),
    _sym("combine", "pkg.query::Query::combine", "pkg/query.py"),
    _sym("combine", "pkg.tree::Q::combine", "pkg/tree.py"),
    _sym("dumps", "pkg.signing::dumps", "pkg/signing.py"),
    _sym("test_combine", "tests.q::T::test_combine", "tests/q.py"),
    *[_sym("save", f"pkg.m{i}::C{i}::save", f"pkg/m{i}.py") for i in range(4)],
]


def _name(spelling: str) -> tuple[str, ...]:
    return tuple(spelling.split("."))


def test_a_name_keeps_its_definitions_outside_tests_or_its_test_ones() -> None:
    assert named_definitions([_name("bulk_update")], _SYMBOLS, asks_tests=True) == [
        [
            ("pkg/query.py", "pkg.query::QuerySet::bulk_update"),
            ("pkg/state.py", "pkg.state::StateApps::bulk_update"),
        ]
    ]
    assert named_definitions([_name("test_combine")], _SYMBOLS, asks_tests=True) == [
        [("tests/q.py", "tests.q::T::test_combine")]
    ]


def test_a_definition_on_a_demoted_path_is_no_candidate_unless_the_query_asks_for_tests() -> None:
    names = [_name("test_combine"), _name("combine")]
    # the search ranks a test down for this query, so the name defined only there is
    # passed over for the next one
    assert named_definitions(names, _SYMBOLS, asks_tests=False) == [
        [("pkg/query.py", "pkg.query::Query::combine"), ("pkg/tree.py", "pkg.tree::Q::combine")]
    ]
    assert [name[0][0] for name in named_definitions(names, _SYMBOLS, asks_tests=True)] == [
        "tests/q.py",
        "pkg/query.py",
    ]
    # test doubles do not make a name ambiguous: one vendored definition and three
    # test classes of the same name leave the one
    rows = [
        _sym("Admin", "pkg.contrib.admin::Admin", "pkg/contrib/admin.py"),
        *[_sym("Admin", f"tests.t{i}::Admin", f"tests/t{i}.py") for i in range(3)],
    ]
    assert named_definitions([_name("Admin")], rows, asks_tests=False) == [
        [("pkg/contrib/admin.py", "pkg.contrib.admin::Admin")]
    ]
    assert named_definitions([_name("Admin")], rows, asks_tests=True) == []


def test_a_name_defined_in_too_many_places_is_dropped() -> None:
    assert named_definitions([_name("save")], _SYMBOLS, asks_tests=False) == []
    # as is one whose read stopped at its bound, whatever paths it returned
    many = [_sym("get", f"tests.t{i}::get", f"tests/t{i}.py") for i in range(_SYMBOLS_PER_NAME)]
    assert (
        named_definitions(
            [_name("get")], [*many, _sym("get", "pkg.a::get", "pkg/a.py")], asks_tests=False
        )
        == []
    )


def test_a_qualified_spelling_names_only_its_own_definition() -> None:
    assert named_definitions([_name("Query.combine")], _SYMBOLS, asks_tests=False) == [
        [("pkg/query.py", "pkg.query::Query::combine")]
    ]
    # a module path qualifies too: its dots separate like `::`
    assert named_definitions([_name("query.QuerySet.bulk_update")], _SYMBOLS, asks_tests=False) == [
        [("pkg/query.py", "pkg.query::QuerySet::bulk_update")]
    ]
    assert named_definitions([_name("signing.dumps")], _SYMBOLS, asks_tests=False) == [
        [("pkg/signing.py", "pkg.signing::dumps")]
    ]
    # the owner is matched before the bound: one `save` among many is still named
    crowd = [_sym("save", f"pkg.m{i}::C{i}::save", f"pkg/m{i}.py") for i in range(40)]
    model = _sym("save", "pkg.base::Model::save", "pkg/base.py")
    assert named_definitions([_name("Model.save")], [*crowd, model], asks_tests=False) == [
        [("pkg/base.py", "pkg.base::Model::save")]
    ]
    assert named_definitions([_name("save")], [*crowd, model], asks_tests=False) == []


def test_an_owner_the_symbol_table_does_not_record_names_nothing() -> None:
    # a module of another library: the repository's `dumps` is not `json.dumps`
    assert (
        named_definitions(
            spelled_names("json.dumps(payload) raises", 32), _SYMBOLS, asks_tests=False
        )
        == []
    )
    # a variable, and a class that only inherits the member
    assert named_definitions([_name("qs.bulk_update")], _SYMBOLS, asks_tests=False) == []
    assert named_definitions([_name("SubQuery.combine")], _SYMBOLS, asks_tests=False) == []
    # the same members named alone, or behind a receiver, are read
    assert named_definitions(
        spelled_names("dumps(payload) raises", 32), _SYMBOLS, asks_tests=False
    ) == [[("pkg/signing.py", "pkg.signing::dumps")]]
    assert named_definitions(
        spelled_names("self.combine() fails", 32), _SYMBOLS, asks_tests=False
    ) == [[("pkg/query.py", "pkg.query::Query::combine"), ("pkg/tree.py", "pkg.tree::Q::combine")]]


def test_definitions_follow_the_query_order_within_the_prefix_and_the_bound() -> None:
    names = [_name("combine"), _name("save"), _name("bulk_update")]
    assert [
        [d[1] for d in name] for name in named_definitions(names, _SYMBOLS, asks_tests=False)
    ] == [
        ["pkg.query::Query::combine", "pkg.tree::Q::combine"],
        ["pkg.query::QuerySet::bulk_update", "pkg.state::StateApps::bulk_update"],
    ]
    # under a path prefix the three-definition bound counts inside it
    assert named_definitions([_name("save")], _SYMBOLS, path_prefix="pkg/m1", asks_tests=False) == [
        [("pkg/m1.py", "pkg.m1::C1::save")]
    ]
    # the read bound is the namespace's: at it, a name is dropped whatever the prefix
    crowd = [_sym("put", f"lib.t{i}::put", f"lib/t{i}.py") for i in range(_SYMBOLS_PER_NAME - 1)]
    one = _sym("put", "pkg.a::put", "pkg/a.py")
    assert (
        named_definitions([_name("put")], [*crowd, one], path_prefix="pkg/", asks_tests=False) == []
    )
    assert named_definitions(
        [_name("put")], [*crowd[1:], one], path_prefix="pkg/", asks_tests=False
    ) == [[("pkg/a.py", "pkg.a::put")]]
    # every name the query spells is kept: a later one is served when the ones before
    # it have no chunk of their own
    wide = [_sym(f"f_{i}", f"pkg.w::f_{i}", "pkg/w.py") for i in range(_MAX_NAMES)]
    got = named_definitions([_name(f"f_{i}") for i in range(_MAX_NAMES)], wide, asks_tests=False)
    assert got == [[("pkg/w.py", f"pkg.w::f_{i}")] for i in range(_MAX_NAMES)]


# ---- the bounded read on a re-indexed table -----------------------------------------

needs_clickhouse = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _reindexed(live: list[dict[str, Any]], dead: list[dict[str, Any]]) -> CodeRepo:
    """A symbol table after re-indexing and its merges: the `live` definitions, and the
    `dead` ones as the name table keeps them, one row per key whose newest version is
    a tombstone."""

    def write(rows: list[dict[str, Any]], version: int, tombstoned_at: str) -> str:
        values = ", ".join(
            f"('o', 'n', '{r.get('tier', 'treesitter')}', '{r['qualified_name']}', "
            f"'{r['symbol_id']}', '{r['name']}', '{r['path']}', {tombstoned_at}, {version})"
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


async def _read(repo: CodeRepo, spelling: str) -> list[dict[str, Any]]:
    """The leg's symbol read for one spelling, bare or qualified."""
    rows: list[dict[str, Any]] = await repo.symbols_named(
        "o",
        "n",
        [spelling.split(".")[-1]],
        qualified=[spelling] if "." in spelling else [],
        per_name=_SYMBOLS_PER_NAME,
    )
    return rows


@needs_clickhouse
@pytest.mark.asyncio
async def test_the_read_bound_counts_live_definitions_not_name_table_rows() -> None:
    # 33 live definitions, five of them outside tests: the name says nothing about where
    live = [_key("helper", f"aa_tests.t{i:02d}::helper", f"tests/t{i:02d}.py") for i in range(28)]
    live += [_key("helper", f"pkg.m{i}::helper", f"pkg/m{i}.py") for i in range(5)]
    # a dead key that sorts first must not take one of the places the bound counts
    dead = [_key("helper", "aa_gone::helper", "pkg/gone.py")]
    for repo in (_reindexed(live, []), _reindexed(live, dead)):
        rows = await _read(repo, "helper")
        assert len(rows) == _SYMBOLS_PER_NAME
        assert all(r["path"] != "pkg/gone.py" for r in rows)
        assert named_definitions([_name("helper")], rows, asks_tests=False) == []


@needs_clickhouse
@pytest.mark.asyncio
async def test_dead_keys_of_a_name_do_not_hide_its_live_definition() -> None:
    # a signature change writes a new key and tombstones the old one
    combine = ("combine", "pkg.query::Query::combine", "pkg/query.py")
    dead = [_key(*combine, symbol_id=f"old-{i:02d}") for i in range(_SYMBOLS_PER_NAME)]
    repo = _reindexed([_key(*combine, symbol_id="zz-live")], dead)
    rows = await _read(repo, "combine")
    assert rows == [_sym(*combine)]
    assert named_definitions([_name("combine")], rows, asks_tests=False) == [
        [("pkg/query.py", combine[1])]
    ]


@needs_clickhouse
@pytest.mark.asyncio
async def test_a_qualified_spelling_of_a_common_member_is_read_apart_from_the_rest() -> None:
    live = [_key("save", f"pkg.m{i:02d}::C{i:02d}::save", f"pkg/m{i:02d}.py") for i in range(40)]
    live.append(_key("save", "pkg.zz::Model::save", "pkg/zz.py"))
    repo = _reindexed(live, [])
    rows = await _read(repo, "Model.save")
    assert named_definitions([_name("Model.save")], rows, asks_tests=False) == [
        [("pkg/zz.py", "pkg.zz::Model::save")]
    ]
    # the bare name is still one that says nothing about where, with or without it
    assert named_definitions([_name("save")], rows, asks_tests=False) == []
    assert named_definitions([_name("save")], await _read(repo, "save"), asks_tests=False) == []
    # and an owner with as many definitions as the bound is dropped like a bare name
    live = [_key("save", f"pkg.m{i:02d}::Model::save", f"pkg/m{i:02d}.py") for i in range(40)]
    rows = await _read(_reindexed(live, []), "Model.save")
    assert named_definitions([_name("Model.save")], rows, asks_tests=False) == []


@needs_clickhouse
@pytest.mark.asyncio
async def test_a_definition_both_tiers_record_counts_once() -> None:
    # the compiler tier records the same two definitions under its own names and a header
    parser = [_key("combine", f"pkg.{m}::Q::combine", f"pkg/{m}.py") for m in ("query", "tree")]
    scip = [
        {**_key("combine", f"scip {m} Q#combine().", f"include/{m}.h"), "tier": "scip"}
        for m in ("query", "tree")
    ]
    rows = await _read(_reindexed([*parser, *scip], []), "combine")
    assert named_definitions([_name("combine")], rows, asks_tests=False) == [
        [("pkg/query.py", "pkg.query::Q::combine"), ("pkg/tree.py", "pkg.tree::Q::combine")]
    ]


# ---- the place in the ranking ------------------------------------------------------


def _row(cid: str, **extra: Any) -> dict[str, Any]:
    return {"chunk_id": cid, "path": f"src/{cid}.cpp", **extra}


def _unit(cid: str, score: float) -> CodeUnit:
    return CodeUnit(unit_id=cid, path=f"src/{cid}.cpp", score=score, via="lexical")


def _placed(ranked: list[CodeUnit], rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
    held = _held_definitions(rows, ranked)
    return [(u.unit_id, u.score) for u in _place_definitions(ranked, held, rows)]


def test_a_named_definition_is_served_after_the_first_unit_at_its_score() -> None:
    ranked = [_unit("a", 0.03), _unit("b", 0.02), _unit("c", 0.01), _unit("d", 0.001)]
    assert _place_definitions(ranked, [], []) is ranked
    # one the ranking holds far down rises to second place, level with the first unit
    assert _placed(ranked, [_row("d")]) == [("a", 0.03), ("d", 0.03), ("b", 0.02), ("c", 0.01)]
    # one no other leg found enters there too
    assert _placed(ranked, [_row("x")])[:3] == [("a", 0.03), ("x", 0.03), ("b", 0.02)]
    # a definition that is first stays first, and nothing else moves
    assert _placed(ranked, [_row("a")]) == [(u.unit_id, u.score) for u in ranked]
    # the definitions of one name: those the ranking holds in its order, then the others
    assert [cid for cid, _ in _placed(ranked, [_row("x"), _row("d"), _row("c")])] == [
        "a",
        "c",
        "d",
        "x",
        "b",
    ]


def test_a_unit_only_the_definition_leg_found_says_so() -> None:
    held = _held_definitions([_row("a"), _row("d")], [_unit("a", 0.03)])
    assert [(u.unit_id, u.via) for u in held] == [("d", "definition")]


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
        self, org: str, ns: str, names: list[str], *, qualified: list[str], per_name: int
    ) -> list[dict[str, Any]]:
        self.calls.append("symbols_named")
        self.kw["names"], self.kw["qualified"] = names, qualified
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


def test_the_first_named_definition_is_served_after_the_first_unit() -> None:
    plain = _search(_Repo(), "how does quota enforcement work")
    repo = _Repo()
    res = _search(repo, "why does parse_quota() ignore what Quota.load_quota returns")
    assert repo.calls == ["symbols_named", "definition_chunks"]
    assert (repo.kw["names"], repo.kw["qualified"]) == (
        ["load_quota", "parse_quota"],
        ["Quota.load_quota"],
    )
    assert repo.kw["definitions"] == [
        ("src/d.cpp", "DB::parse_quota"),
        ("src/other.cpp", "DB::Other::parse_quota"),
    ]
    assert repo.kw["scope"] is None
    # d, which only the definition leg found, is served after a at a's score, and the
    # other units keep the scores they have without the leg
    assert [u.unit_id for u in plain.units] == ["a", "c", "b"]
    assert [u.unit_id for u in res.units] == ["a", "d", "c", "b"]
    scores = {u.unit_id: u.score for u in res.units}
    assert scores["d"] == scores["a"]
    assert {u.unit_id: u.score for u in plain.units} == {k: scores[k] for k in "acb"}
    assert {u.unit_id: u.via for u in res.units}["d"] == "definition"
    assert "named_definitions=1" in res.score_explanation
    assert res.retrieval_work["named_definitions"] == 1
    # confidence and abstention are read off the other legs
    assert res.retrieval_confidence == plain.retrieval_confidence
    assert (res.abstain, res.retrieval_risk) == (plain.abstain, plain.retrieval_risk)
    assert res.top_similarity == plain.top_similarity
    # a definition a content leg found rises to the same place: b, lexical #2, passes
    # vector-only c; the name mentioned second is not read
    res = _search(_Repo(), "why does load_quota() ignore what parse_quota returns")
    assert [(u.unit_id, u.via) for u in res.units] == [
        ("a", "both"),
        ("b", "lexical"),
        ("c", "vector"),
    ]


def test_one_leg_finding_the_definition_does_not_put_it_first() -> None:
    class _Deep(_Repo):
        async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            rows = [
                {"chunk_id": f"l{i}", "lex_score": 1.0, "path": f"src/l{i}.cpp", "scope": "f"}
                for i in range(1, 61)
            ]
            rows[49] = {"chunk_id": "d", "lex_score": 1.0, "path": "src/d.cpp", "scope": "g"}
            return [{**row, **_UNIT} for row in rows]

    # the lexical leg runs alone and has the definition at rank 50: it is served
    # second, level with that leg's first hit
    res = _search(_Deep(), "why does parse_quota() fail", legs="lexical")
    assert [(u.unit_id, u.lexical_rank) for u in res.units[:3]] == [("l1", 1), ("d", 50), ("l2", 2)]
    assert res.units[1].score == res.units[0].score
    # the same when a vector leg was asked for and returned nothing
    with patch.object(_Deep, "vector_search_chunks", return_value=[]):
        res = _search(_Deep(), "why does parse_quota() fail")
    assert [u.unit_id for u in res.units[:3]] == ["l1", "d", "l2"]


def test_a_definition_agents_use_is_still_served_second() -> None:
    class _Used(_Repo):
        async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
            return {cid: (9.0, 0.0, naive_utc_now()) for cid in "bd"}

    # d, which no content leg found, is not read by the outcome boost: it would pass a
    # at a's own score
    res = _search(_Used(), "why does parse_quota() fail")
    assert [u.unit_id for u in res.units] == ["a", "d", "b", "c"]
    assert res.units[1].score == res.units[0].score
    # b, lexical #2, is boosted past c as without the leg, then served level with a
    plain = _search(_Used(), "how does quota enforcement work")
    assert [u.unit_id for u in plain.units] == ["a", "b", "c"]
    res = _search(_Used(), "why does load_quota() fail")
    assert [(u.unit_id, u.score) for u in res.units] == [
        ("a", plain.units[0].score),
        ("b", plain.units[0].score),
        ("c", plain.units[2].score),
    ]


def test_a_definition_near_the_askers_work_is_still_served_second() -> None:
    class _Near(_Repo):
        async def symbols_named(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return [_sym("parse_quota", "DB::parse_quota", "lib/d.cpp")]

        async def definition_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return [{"chunk_id": "d", "path": "lib/d.cpp", "scope": "DB::parse_quota", **_UNIT}]

    # the tie-break swaps a near-tie toward the asker's directories, and a definition
    # level with the first unit is an exact one
    res = _search(_Near(), "why does parse_quota() fail", user_recent_dirs=["lib"])
    assert [u.unit_id for u in res.units] == ["a", "d", "c", "b"]


def test_a_reranker_orders_the_other_units_and_the_definition_follows_its_first() -> None:
    async def reversed_head(endpoint: str, query: str, units: list[CodeUnit], **k: Any) -> Any:
        return list(reversed(units)), True

    # saturation off: it sorts the page by score again
    with (
        patch("server.code.app.search.rerank._rerank_units", reversed_head),
        patch.object(settings, "code_search_file_saturation", 0.0),
    ):
        plain = _search(_Repo(), "how does quota enforcement work", rerank_endpoint="builtin:x")
        res = _search(_Repo(), "why does parse_quota() fail", rerank_endpoint="builtin:x")
    assert [u.unit_id for u in plain.units] == ["b", "c", "a"]
    assert [u.unit_id for u in res.units] == ["b", "d", "c", "a"]
    assert res.units[1].score == res.units[0].score


def test_the_priors_score_the_other_units_as_they_do_without_the_leg() -> None:
    class _Unserved(_Repo):
        async def definition_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

    def run(repo: _Repo) -> Any:
        with patch.object(settings, "code_search_pr_leg", "off"):
            return asyncio.run(
                cs.search(
                    repo, "o", "ns", query="why does parse_quota() fail", embedder=_Embedder()
                )
            )

    without, served = run(_Unserved()), run(_Repo())
    assert [u.unit_id for u in served.units] == ["a", "d", "c", "b"]
    assert [(u.unit_id, u.score) for u in served.units if u.unit_id != "d"] == [
        (u.unit_id, u.score) for u in without.units
    ]


def test_a_name_behind_an_unrecorded_owner_adds_no_definition() -> None:
    plain = _search(_Repo(), "how does quota enforcement work")
    repo = _Repo()
    res = _search(repo, "why does json.parse_quota(payload) fail")
    assert repo.kw["definitions"] == []
    assert [(u.unit_id, u.via) for u in res.units] == [(u.unit_id, u.via) for u in plain.units]
    assert "named_definitions" not in res.retrieval_work


def test_the_leg_serves_the_definitions_of_the_first_name_that_has_a_chunk() -> None:
    class _Many(_Repo):
        async def symbols_named(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return [
                *[_sym(f"fn_{i}", f"DB::fn_{i}", "src/m.cpp") for i in range(6)],
                _sym("fn_1", "DB::Other::fn_1", "src/n.cpp"),
            ]

        async def definition_chunks(
            self, org: str, ns: str, model: str, definitions: list[tuple[str, str]], **k: Any
        ) -> list[dict[str, Any]]:
            self.kw["definitions"] = definitions
            return [
                {"chunk_id": "n1", "path": "src/n.cpp", "scope": "DB::Other::fn_1", **_UNIT},
                *[  # fn_0 has no chunk of its own
                    {"chunk_id": f"m{i}", "path": "src/m.cpp", "scope": f"DB::fn_{i}", **_UNIT}
                    for i in reversed(range(1, 6))
                ],
            ]

    repo = _Many()
    res = _search(repo, "fn_0 fn_1 fn_2 fn_3 fn_4 fn_5 all fail", top_k=10)
    assert len(repo.kw["definitions"]) == 7  # six names, one of them defined twice
    assert [u.unit_id for u in res.units if u.via == "definition"] == ["m1", "n1"]


def test_a_name_defined_only_in_tests_is_served_when_the_query_asks_for_tests() -> None:
    class _TestOnly(_Repo):
        async def symbols_named(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return [_sym("parse_quota", "tests.q::parse_quota", "tests/d.cpp")]

        async def definition_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            self.kw["definitions"] = a[3]
            return [
                {"chunk_id": "d", "path": "tests/d.cpp", "scope": "tests.q::parse_quota", **_UNIT}
            ]

    repo = _TestOnly()
    res = _search(repo, "why does parse_quota() fail")
    assert repo.kw["definitions"] == [] and "named_definitions" not in res.retrieval_work
    assert [u.unit_id for u in res.units] == ["a", "c", "b"]
    res = _search(_TestOnly(), "which test covers parse_quota()")
    assert [u.unit_id for u in res.units] == ["a", "d", "c", "b"]


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
