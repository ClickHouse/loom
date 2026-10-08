"""Symbol-form tolerance. An over-qualified name — one with
`(anonymous namespace)::` segments, or a namespace the definition does not
carry — binds after the exact and suffix lookups miss, when exactly one
definition fits; the payload says `resolved_by`. A bare name never enters
the rule, and a dropped segment that names an indexed class declines."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.index import symbol_forms as sf

ANON = "DB::(anonymous namespace)::AggregateFunctionLargestTriangleThreeBuckets::createResultType"


def _row(qn: str, sid: str = "", kind: str = "function") -> dict[str, Any]:
    return {
        "symbol_id": sid or qn,
        "qualified_name": qn,
        "name": qn.split("::")[-1],
        "kind": kind,
        "path": "src/x.cpp",
        "tier": "treesitter",
    }


# ---- pure rules -------------------------------------------------------------


def test_segments_keep_the_anonymous_namespace_as_one_segment() -> None:
    assert sf.segments(ANON) == [
        "DB",
        sf.ANON,
        "AggregateFunctionLargestTriangleThreeBuckets",
        "createResultType",
    ]
    assert sf.segments("DB::Foo::bar(int, const String &)") == ["DB", "Foo", "bar"]
    assert sf.segments("DB::Foo<T>::bar") == ["DB", "Foo<T>", "bar"]
    assert sf.segments("bar") == ["bar"]


def test_strip_anonymous() -> None:
    assert (
        sf.strip_anonymous(ANON)
        == "DB::AggregateFunctionLargestTriangleThreeBuckets::createResultType"
    )
    assert sf.strip_anonymous("(anonymous namespace)::f") == "f"
    assert sf.strip_anonymous("DB::Foo::bar") == "", "no such segment: the form did not change"


def test_is_qualified_ignores_anonymous_segments() -> None:
    assert sf.is_qualified("DB::f")
    assert not sf.is_qualified("f")
    assert not sf.is_qualified("(anonymous namespace)::f"), "a bare name in disguise"


def test_the_compiler_tier_spelling_of_an_anonymous_namespace() -> None:
    """ClickGap's trace spells it the compiler way: `$anonymous_namespace_<path>`."""
    scip = "DB::$anonymous_namespace_src/Interpreters/InterpreterCreateQuery.cpp::throwIfTableFunctionCannotBeUsedToCreateTable"
    assert sf.segments(scip)[1].startswith("$anonymous_namespace")
    assert sf.strip_anonymous(scip) == "DB::throwIfTableFunctionCannotBeUsedToCreateTable"
    assert (
        sf.strip_anonymous(
            "$anonymous_namespace_src/Common/OptimizedRegularExpression.cpp::analyzeImpl"
        )
        == "analyzeImpl"
    )
    assert not sf.is_qualified(
        "$anonymous_namespace_src/Common/OptimizedRegularExpression.cpp::analyzeImpl"
    )


@pytest.mark.asyncio
async def test_a_stripped_bare_name_is_looked_up_as_a_bare_name() -> None:
    repo = _Repo({"analyzeImpl": [_row("analyzeImpl"), _row("DB::Other::analyzeImpl")]})
    rows, by = await sf.resolve_tolerant(
        repo,
        "o",
        "n",
        "$anonymous_namespace_src/Common/OptimizedRegularExpression.cpp::analyzeImpl",
    )
    # rule 1 handed the bare name to the ordinary lookup (every definition, the
    # ambiguity guard downstream decides); rule 2 never ran for a bare name
    assert by == sf.RESOLVED_ANON and len(rows) == 2 and repo.calls == [("analyzeImpl", 50)]


def test_tail_binds_a_dropped_namespace_when_one_definition_fits() -> None:
    rows = [
        _row("DB::parseTableStatsHint", "a"),
        _row("DB::parseTableStatsHint", "b"),
        _row("Other::parseTableStatsHint"),
    ]
    bound = sf.tail_bind("DB::QueryPlanOptimizations::parseTableStatsHint", rows, class_names=set())
    # both rows of the one qualified name that fits (two tiers of one definition)
    assert [r["symbol_id"] for r in bound] == ["a", "b"]
    assert sf.dropped_segments("DB::QueryPlanOptimizations::parseTableStatsHint", rows) == {
        "QueryPlanOptimizations",
    }


def test_tail_prefers_the_longest_match_and_declines_a_tie() -> None:
    # `Bar` dropped — with Bar not a class, DB::Foo::run (2 segments) beats DB::run (1) and
    # Foo::run (1); DB::Baz::run is no subsequence at all
    rows2 = [_row("DB::Foo::run"), _row("DB::run"), _row("Foo::run"), _row("DB::Baz::run")]
    got = sf.tail_bind("DB::Foo::Bar::run", rows2, class_names=set())
    assert [r["qualified_name"] for r in got] == ["DB::Foo::run"]
    # a tie at the longest length declines
    rows3 = [_row("DB::Foo::run"), _row("DB::Bar::run")]
    assert sf.tail_bind("DB::Foo::Bar::run", rows3, class_names=set()) == []


def test_a_dropped_segment_that_names_a_class_declines() -> None:
    """`DB::Foo::Bar::run` with `Bar` an indexed class: Bar::run is not in the
    table, and Foo::run is not Bar's method — never bind the outer class's."""
    rows = [_row("DB::Foo::run")]
    assert sf.tail_bind("DB::Foo::Bar::run", rows, class_names={"Bar"}) == []
    assert [
        r["qualified_name"] for r in sf.tail_bind("DB::Foo::Bar::run", rows, class_names=set())
    ] == [
        "DB::Foo::run",
    ]


def test_tail_never_binds_a_bare_definition_or_a_bare_query() -> None:
    assert sf.tail_bind("DB::Foo::run", [_row("run")], class_names=set()) == []
    assert sf.tail_bind("run", [_row("DB::run")], class_names=set()) == []
    # the exact scope is not a tail match (the exact lookup owns it)
    assert sf.tail_bind("DB::Foo::run", [_row("DB::Foo::run")], class_names=set()) == []
    # an out-of-order scope is not a subsequence
    assert sf.tail_bind("DB::Foo::Bar::run", [_row("Bar::Foo::run")], class_names=set()) == []


def test_anonymous_segments_in_stored_rows_are_ignored_for_the_match() -> None:
    rows = [_row("DB::(anonymous namespace)::Foo::run")]
    got = sf.tail_bind("DB::Foo::Extra::run", rows, class_names=set())
    assert [r["qualified_name"] for r in got] == ["DB::(anonymous namespace)::Foo::run"]


def test_mark_and_resolved_by_of() -> None:
    rows = [_row("DB::f")]
    assert sf.mark(rows, "") is rows
    marked = sf.mark(rows, "tail")
    assert marked[0]["resolved_by"] == "tail" and "resolved_by" not in rows[0]
    assert sf.resolved_by_of(marked) == "tail" and sf.resolved_by_of(rows) == ""


# ---- the repository walk ----------------------------------------------------------


class _Repo:
    def __init__(self, table: dict[str, list[dict[str, Any]]]) -> None:
        self.table = table
        self.calls: list[tuple[str, int]] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        self.calls.append((name, limit))
        rows = self.table.get(name, [])
        return rows[:limit]

    async def grep_chunks(self, org, ns, pattern, **kw):
        # the registration rule reads chunks for a SQL-level name;
        # this fixture registers nothing
        return []


@pytest.mark.asyncio
async def test_resolve_tolerant_strips_the_anonymous_namespace_first() -> None:
    exact = "DB::AggregateFunctionLargestTriangleThreeBuckets::createResultType"
    repo = _Repo(
        {
            exact: [_row(exact)],
            "createResultType": [_row(exact)]
            + [_row(f"DB::A{i}::createResultType") for i in range(89)],
        }
    )
    rows, by = await sf.resolve_tolerant(repo, "o", "n", ANON)
    assert by == sf.RESOLVED_ANON and [r["qualified_name"] for r in rows] == [exact]
    assert repo.calls == [(exact, 50)], "one exact read; the 90-row bare read never ran"


@pytest.mark.asyncio
async def test_resolve_tolerant_binds_the_tail_and_checks_dropped_segments_for_classes() -> None:
    repo = _Repo(
        {
            "parseTableStatsHint": [
                _row("DB::parseTableStatsHint", "scip"),
                _row("DB::parseTableStatsHint", "ts"),
            ],
            "QueryPlanOptimizations": [],  # a namespace: nothing indexed under that name
        }
    )
    rows, by = await sf.resolve_tolerant(
        repo, "o", "n", "DB::QueryPlanOptimizations::parseTableStatsHint"
    )
    assert by == sf.RESOLVED_TAIL and [r["symbol_id"] for r in rows] == ["scip", "ts"]
    assert ("parseTableStatsHint", sf.TAIL_READ_LIMIT) in repo.calls
    assert ("QueryPlanOptimizations", 5) in repo.calls, "the dropped segment was checked"


@pytest.mark.asyncio
async def test_resolve_tolerant_declines_when_the_dropped_segment_is_a_class() -> None:
    repo = _Repo(
        {
            "run": [_row("DB::Foo::run")],
            "Bar": [_row("DB::Foo::Bar", kind="class")],
        }
    )
    rows, by = await sf.resolve_tolerant(repo, "o", "n", "DB::Foo::Bar::run")
    assert rows == [] and by == ""


@pytest.mark.asyncio
async def test_resolve_tolerant_leaves_bare_names_and_hubs_alone() -> None:
    repo = _Repo({"run": [_row(f"DB::C{i}::run") for i in range(sf.TAIL_READ_LIMIT)]})
    assert await sf.resolve_tolerant(repo, "o", "n", "run") == ([], "")
    # a bare name never re-reads its own definitions; the registration
    # rule reads only the convention classes (`FunctionRun`, …) as keyed lookups
    assert repo.calls and all(n.endswith("Run") and n != "run" for n, _ in repo.calls)
    repo.calls.clear()
    # a hub name past the read cap: the candidate set is not whole, no binding
    assert await sf.resolve_tolerant(repo, "o", "n", "DB::Extra::run") == ([], "")


# ---- the handlers' seed resolution ----------------------------------------------


@pytest.mark.asyncio
async def test_resolve_symbols_marks_rows_and_the_callers_payload_discloses(monkeypatch) -> None:
    from server.code.app.symbol import graph, resolve

    class Repo(_Repo):
        async def symbols_by_ids(self, org, ns, ids):
            return []

        async def edges_for_symbols(self, *a, **k):
            return []

        async def functions_for_paths(self, *a, **k):
            return []

    exact = "DB::AggregateFunctionLargestTriangleThreeBuckets::createResultType"
    repo = Repo({exact: [{**_row(exact), "path": "src/AggregateFunctions/X.cpp"}]})
    rows = await resolve._resolve_symbols(repo, "o", "n", symbol_id="", name=ANON)
    assert [r["qualified_name"] for r in rows] == [exact] and rows[0][
        "resolved_by"
    ] == "anonymous_namespace"
    # an exact hit carries no marker
    plain = await resolve._resolve_symbols(repo, "o", "n", symbol_id="", name=exact)
    assert "resolved_by" not in plain[0]

    monkeypatch.setattr(graph, "CodeRepo", lambda: repo)
    monkeypatch.setattr(graph, "require_code_intel", lambda: None)
    monkeypatch.setattr(graph, "resolve_code_identity", lambda org, ns: ("o", "n"))
    monkeypatch.setattr(graph, "schedule_code_trace", lambda **k: None)

    async def none_rows(*a, **k):
        return []

    async def zero_pair(*a, **k):
        return (0, 0)

    async def empty_set(*a, **k):
        return set()

    async def false(*a, **k):
        return False

    async def none(*a, **k):
        return None

    monkeypatch.setattr(graph, "_dispatch_seeds", none_rows)
    monkeypatch.setattr(graph, "_declined_safe", zero_pair)
    monkeypatch.setattr(graph, "_scip_coverage", empty_set)
    monkeypatch.setattr(graph, "_dispatch_missing", false)
    monkeypatch.setattr(graph, "_candidate_block", none)
    monkeypatch.setattr(graph, "_ctor_edges_absent", false)
    from server.code.api.models_symbol import CodeCallersInput

    out = await graph.handle_code_callers(CodeCallersInput(name=ANON, include_ambiguous=False))
    wire = out.model_dump()
    assert wire["resolved_by"] == "anonymous_namespace" and wire["seed_symbols"] == [exact]
    exact_out = await graph.handle_code_callers(
        CodeCallersInput(name=exact, include_ambiguous=False)
    )
    assert "resolved_by" not in exact_out.model_dump(), "an exact hit's payload is what it was"


def test_output_models_drop_the_unset_disclosure() -> None:
    from server.code.api.models_symbol import CodeCallersOutput, CodeImpactOutput, CodeSymbolOutput

    assert "resolved_by" not in CodeCallersOutput(edges=[], seed_symbols=[]).model_dump()
    assert (
        CodeCallersOutput(edges=[], seed_symbols=[], resolved_by="tail").model_dump()["resolved_by"]
        == "tail"
    )
    imp = CodeImpactOutput(impacted_symbols=[], impacted_tests=[], co_change_files=[])
    assert "resolved_by" not in imp.model_dump()
    assert (
        CodeImpactOutput(
            impacted_symbols=[], impacted_tests=[], co_change_files=[], resolved_by="tail"
        ).model_dump()["resolved_by"]
        == "tail"
    )
    assert "resolved_by" not in CodeSymbolOutput(definitions=[]).model_dump()
    assert (
        CodeSymbolOutput(definitions=[], resolved_by="tail").model_dump()["resolved_by"] == "tail"
    )
