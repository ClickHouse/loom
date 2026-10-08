"""Coverage evidence carries its tier, `covered_by` needs a row
that names the subject or a PR co-occurrence (else `weak_evidence`), the
stateful tree is a test tree, and a SQL-level name (`sumMap`) binds to the
class that implements it through the registration statement — for
code.coverage_claim (its exact_scoped terms) and for code.callers /
code.impact seeds (`resolved_by: registration`). The positive control keeps
its semantics: no `uncovered` and no `weak_evidence` without a passing
control."""

from __future__ import annotations

from typing import Any

import pytest

import server.code.app.coverage as h
from server.code.api.models_symbol import CodeCallersOutput
from server.code.api.models_testing import CodeCoverageClaimInput, CodeCoverageClaimOutput
from server.code.app.index import sql_names as sn
from server.code.app.index import symbol_forms as sf
from server.code.domain.index import coverage_claim as cc

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ST, SF, IT = cc.STATELESS, cc.STATEFUL, cc.INTEGRATION
ORG, NS = "clickhouse", "code-clickhouse"


# ---- pure helpers: tiers, verdicts, trees --------------------------------------


def test_the_stateful_tree_is_a_test_tree() -> None:
    assert cc.TEST_PREFIXES == (ST, SF, IT)
    assert cc.is_test_file(f"{SF}00001_count_hits.sql")
    assert cc.is_test_file(f"{SF}00170_lower_upper_utf8_hits.sh")
    assert not cc.is_test_file(f"{SF}00001_count_hits.reference")
    assert cc.is_test_file(f"{ST}02994_sanity_check_settings.sql"), "0_stateless unchanged"


def test_tier_of_method_and_strongest() -> None:
    assert cc.tier_of_method("content_reference", scoped=True) == cc.EXACT_SCOPED
    assert cc.tier_of_method("content_reference") == cc.FILE_REFERENCE
    assert cc.tier_of_method("grep", scoped=True) == cc.EXACT_SCOPED
    assert cc.tier_of_method("grep") == cc.SHORT_NAME_GREP
    assert cc.tier_of_method("pr_cooccurrence") == cc.PR_COOCCURRENCE
    assert cc.tier_of_method("path_convention") == cc.PATH_CONVENTION
    assert cc.tier_of_method("include_graph") == cc.EXACT_SCOPED
    assert (
        cc.strongest([cc.PATH_CONVENTION, cc.SHORT_NAME_GREP, cc.PR_COOCCURRENCE])
        == cc.PR_COOCCURRENCE
    )
    assert cc.strongest([]) == ""


def test_row_tier_reads_the_tier_or_derives_it_from_methods() -> None:
    assert cc.row_tier({"tier": cc.EXACT_SCOPED, "methods": ["grep"]}) == cc.EXACT_SCOPED
    assert cc.row_tier({"methods": ["grep", "pr_cooccurrence"]}) == cc.PR_COOCCURRENCE
    assert cc.row_tier({"method": "path_convention"}) == cc.PATH_CONVENTION
    assert cc.row_tier({"test_path": "t"}) == ""


def test_coverage_verdict_needs_a_strong_tier_and_a_passing_control() -> None:
    weak = [
        {"test_path": "a", "tier": cc.SHORT_NAME_GREP},
        {"test_path": "b", "tier": cc.PATH_CONVENTION},
    ]
    assert cc.coverage_verdict(weak, control_ok=True) == cc.WEAK_EVIDENCE
    assert (
        cc.coverage_verdict(
            [*weak, {"test_path": "c", "tier": cc.PR_COOCCURRENCE}], control_ok=True
        )
        == cc.COVERED_BY
    )
    assert (
        cc.coverage_verdict([{"test_path": "c", "tier": cc.EXACT_SCOPED}], control_ok=True)
        == cc.COVERED_BY
    )
    assert cc.coverage_verdict([], control_ok=True) == cc.UNCOVERED
    # the positive control: no negative and no weak claim without it
    assert cc.coverage_verdict(weak, control_ok=False) == cc.UNKNOWN
    assert cc.coverage_verdict([], control_ok=False) == cc.UNKNOWN
    # rows built before tiers existed still decide by their methods
    assert (
        cc.coverage_verdict([{"test_path": "d", "methods": ["pr_cooccurrence"]}], control_ok=True)
        == cc.COVERED_BY
    )
    assert (
        cc.coverage_verdict([{"test_path": "d", "methods": ["grep"]}], control_ok=True)
        == cc.WEAK_EVIDENCE
    )


def test_default_terms() -> None:
    assert h.default_terms("setting", "max_threads") == {"max_threads": cc.EXACT_SCOPED}
    assert h.default_terms("path", "src/x.cpp") == {}
    assert h.default_terms("symbol", "readRow") == {"readRow": cc.SHORT_NAME_GREP}
    assert h.default_terms("symbol", "DB::NpyRowInputFormat::readRow(size_t)") == {
        "readRow": cc.SHORT_NAME_GREP,
        "NpyRowInputFormat::readRow": cc.EXACT_SCOPED,
    }


# ---- sql names: the three registration shapes --------------------------------------


def _chunk(path: str, scope: str, content: str, start: int = 10) -> dict[str, Any]:
    return {
        "path": path,
        "scope": scope,
        "symbol_ids": [],
        "start_line": start,
        "end_line": start + content.count("\n"),
        "content": content,
    }


def test_is_sql_name_candidate() -> None:
    assert sn.is_sql_name_candidate("sumMap")
    assert sn.is_sql_name_candidate("arrayFold::executeImpl")
    assert not sn.is_sql_name_candidate("std::vector")
    assert not sn.is_sql_name_candidate("DB::Foo::bar")
    assert not sn.is_sql_name_candidate("a::b::c")
    # a keyword is still an identifier; the grep decides whether it names a function
    assert sn.is_sql_name_candidate("return")
    assert sn.split_sql_form("toFixedString::isInjective") == ("toFixedString", "isInjective")
    assert sn.split_sql_form("indexHint") == ("indexHint", "")


def test_the_symbol_naming_convention_helpers() -> None:
    assert sn.registration_classes("sumMap") == [
        "FunctionSumMap",
        "AggregateFunctionSumMap",
        "TableFunctionSumMap",
        "NameSumMap",
    ]
    assert sn.combinator_stems("countIfOrNull") == ["countIf", "count"]
    assert sn.combinator_stems("anyLast_respect_nulls") == ["anyLast"]
    assert sn.combinator_stems("sumMap") == ["sum"], (
        "Map is a combinator; the stem is what registers"
    )
    assert sn.combinator_stems("If") == []


@pytest.mark.asyncio
async def test_the_convention_route_binds_a_rowed_class_before_any_grep() -> None:
    class _R(_Repo):
        async def symbols_by_name(self, org, ns, name, *, limit=50):
            self.calls.append(("symbols_by_name", name))
            if name == "FunctionArrayShingles":
                return [
                    {
                        "qualified_name": "DB::FunctionArrayShingles",
                        "kind": "class",
                        "symbol_id": "as-c",
                        "path": "src/Functions/arrayShingles.cpp",
                    }
                ]
            return []

        async def functions_for_paths(self, org, ns, paths, *, limit=400, kinds=()):
            return [
                {
                    "qualified_name": "DB::FunctionArrayShingles::executeImpl",
                    "kind": "function",
                    "symbol_id": "as-e",
                    "path": paths[0],
                },
                {
                    "qualified_name": "DB::FunctionArrayShingles::create",
                    "kind": "function",
                    "symbol_id": "as-cr",
                    "path": paths[0],
                },
            ]

    repo = _R()
    rows = await sn.Resolver(repo, ORG, NS).resolve("arrayShingles")
    assert [r["symbol_id"] for r in rows] == ["as-e", "as-cr"]
    assert not any(c[0] == "grep_chunks" for c in repo.calls), "keyed reads only"
    # a combinator: the stem's class binds
    rows = await sn.Resolver(repo, ORG, NS).resolve("arrayShinglesIf")
    assert [r["symbol_id"] for r in rows] == ["as-e", "as-cr"]


def test_registration_hits_keep_the_three_shapes_and_the_registries() -> None:
    chunks = [
        _chunk(
            "src/Functions/indexHint.h",
            "DB::FunctionIndexHint",
            'class FunctionIndexHint : public IFunction\n{\npublic:\n    static constexpr auto name = "indexHint";\n',
            24,
        ),
        _chunk(
            "src/Functions/array/arrayAUC.cpp",
            "DB::REGISTER_FUNCTION",
            'REGISTER_FUNCTION(ArrayAUC)\n{\n    factory.registerFunction("arrayAUCPR", [](ContextPtr){ return FunctionArrayAUC::create("arrayAUCPR", true); }, doc);\n}',
            554,
        ),
        _chunk(
            "src/AggregateFunctions/AggregateFunctionSumMap.cpp",
            "DB::AggregateFunctionSumMap::getNameImpl",
            'static String getNameImpl()\n{\n    if (overflow)\n        return "sumMapWithOverflow";\n    return "sumMap";\n}',
            500,
        ),
        # the same literal in a test or a doc string is not a registration
        _chunk(
            "src/Functions/array/arrayAUC.cpp",
            "",
            'The `arrayAUCPR` function; see "arrayAUCPR" in docs',
            600,
        ),
        # a name constant outside the registries is prose, not a SQL name
        _chunk(
            "src/Interpreters/Foo.cpp", "DB::Foo", 'static constexpr auto name = "indexHint";', 1
        ),
    ]
    assert [(x.shape, x.scope, x.line) for x in sn.registration_hits("indexHint", chunks)] == [
        ("name_constant", "DB::FunctionIndexHint", 27),
    ]
    assert [(x.shape, x.scope) for x in sn.registration_hits("arrayAUCPR", chunks)] == [
        ("registration_call", "DB::REGISTER_FUNCTION"),
    ]
    assert [(x.shape, x.scope, x.line) for x in sn.registration_hits("sumMap", chunks)] == [
        ("return_literal", "DB::AggregateFunctionSumMap::getNameImpl", 504),
    ]
    assert sn.registration_hits("sumMapWithOverflow", chunks)[0].line == 503


def test_class_of_scope_and_methods_of() -> None:
    rows = [
        {"qualified_name": "DB::FunctionIndexHint", "kind": "class", "symbol_id": "c"},
        {
            "qualified_name": "DB::FunctionIndexHint::executeImpl",
            "kind": "function",
            "symbol_id": "e",
        },
        {"qualified_name": "DB::FunctionIndexHint::getName", "kind": "function", "symbol_id": "g"},
        {
            "qualified_name": "DB::FunctionIndexHint::Inner::run",
            "kind": "function",
            "symbol_id": "i",
        },
        {"qualified_name": "DB::REGISTER_FUNCTION", "kind": "function", "symbol_id": "r"},
    ]
    assert sn.class_of_scope("DB::FunctionIndexHint", rows) == "DB::FunctionIndexHint"
    assert sn.class_of_scope("DB::FunctionIndexHint::getName", rows) == "DB::FunctionIndexHint"
    assert sn.class_of_scope("DB::REGISTER_FUNCTION", rows) == ""
    assert sn.class_of_scope("", rows) == ""
    # a template class the parser did not row, whose methods it did
    assert (
        sn.class_of_scope("DB::FunctionIndexHint::getNameImpl", rows[1:3])
        == "DB::FunctionIndexHint"
    )
    got = sn.methods_of("DB::FunctionIndexHint", rows)
    assert [r["symbol_id"] for r in got] == ["e", "g"], "direct members only, interface first"
    assert [
        r["symbol_id"] for r in sn.methods_of("DB::FunctionIndexHint", rows, method="getName")
    ] == ["g"]


def test_narrow_targets_singles_out_the_class_whose_name_holds_the_sql_name() -> None:
    edges = [
        {"to_symbol_id": "1", "to_qualified_name": "DB::AggregateFunctionMinMap::keepKey"},
        {"to_symbol_id": "2", "to_qualified_name": "DB::AggregateFunctionSumMap::keepKey"},
        {"to_symbol_id": "3", "to_qualified_name": "DB::AggregateFunctionSumMap::getNameImpl"},
    ]
    assert [e["to_symbol_id"] for e in sn.narrow_targets("sumMap", edges)] == ["2", "3"]
    assert sn.narrow_targets("arrayAUCPR", edges) == edges, (
        "none matches: the registrar's whole set"
    )


def test_sql_names_from_chunks_keep_the_class_s_names() -> None:
    cls = "DB::AggregateFunctionSumMap"
    consts = [
        _chunk(
            "src/AggregateFunctions/AggregateFunctionSumMap.cpp",
            f"{cls}::getNameImpl",
            'return "sumMapWithOverflow";\n return "sumMap";',
        ),
        _chunk(
            "src/AggregateFunctions/AggregateFunctionSumMap.cpp", f"{cls}::keepKey", 'return "x";'
        ),
        _chunk(
            "src/AggregateFunctions/AggregateFunctionSumMap.cpp",
            "DB::Other",
            'static constexpr auto name = "minMap";',
        ),
    ]
    regs = [
        _chunk(
            "src/AggregateFunctions/AggregateFunctionSumMap.cpp",
            "DB::registerAggregateFunctionSumMap",
            'factory.registerFunction("sumMapFiltered", {…});\n factory.registerFunction("minMappedArrays", {…});',
        ),
        _chunk(
            "src/AggregateFunctions/AggregateFunctionSumMap.cpp",
            "DB::notARegistrar",
            'factory.registerFunction("sumMapZ", {…});',
        ),
    ]
    assert sn.sql_names_from_chunks(cls, consts, regs) == [
        "sumMapWithOverflow",
        "sumMap",
        "sumMapFiltered",
    ]
    # a name constant in the class scope, under the registries only
    assert sn.sql_names_from_chunks(
        "DB::FunctionIndexHint",
        [
            _chunk(
                "src/Functions/indexHint.h",
                "DB::FunctionIndexHint",
                'static constexpr auto name = "indexHint";',
            ),
            _chunk(
                "src/Interpreters/x.cpp",
                "DB::FunctionIndexHint",
                'static constexpr auto name = "prose";',
            ),
        ],
        [],
    ) == ["indexHint"]


# ---- the resolver over a stub repository --------------------------------------------


class _Repo:
    """A slice of the ClickHouse index: indexHint (name constant in the .h,
    registrar in the .cpp), sumMap (return literal + registrar with edges to
    three classes), quantileGK (a name struct handed to a template creator)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def grep_chunks(
        self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
    ):
        self.calls.append(("grep_chunks", (pattern, path_prefix)))
        if pattern == '"indexHint"':
            return [
                _chunk(
                    "src/Functions/indexHint.h",
                    "DB::FunctionIndexHint",
                    'static constexpr auto name = "indexHint";',
                    27,
                )
            ]
        if pattern == '"sumMap"':
            return [
                _chunk(
                    "src/AggregateFunctions/AggregateFunctionSumMap.cpp",
                    "DB::AggregateFunctionSumMap::getNameImpl",
                    'return "sumMap";',
                    506,
                )
            ]
        if pattern == '"arrayAUCPR"':
            return [
                _chunk(
                    "src/Functions/array/arrayAUC.cpp",
                    "DB::REGISTER_FUNCTION",
                    'factory.registerFunction("arrayAUCPR", [](ContextPtr){ return FunctionArrayAUC::create("arrayAUCPR", true); });',
                    582,
                )
            ]
        if pattern == '"quantileGK"':
            return [
                _chunk(
                    "src/AggregateFunctions/AggregateFunctionQuantile.h",
                    "DB::NameQuantileGK",
                    'struct NameQuantileGK { static constexpr auto name = "quantileGK"; };',
                    402,
                )
            ]
        if pattern == "NameQuantileGK":
            return [
                _chunk(
                    "src/AggregateFunctions/AggregateFunctionQuantileGK.cpp",
                    "DB::registerAggregateFunctionsQuantileGK",
                    "factory.registerFunction(NameQuantileGK::name, createAggregateFunctionQuantile<FuncQuantileGK>);",
                    520,
                )
            ]
        if pattern == "name" and path_prefix == "src/Functions/indexHint.":
            return [
                _chunk(
                    "src/Functions/indexHint.h",
                    "DB::FunctionIndexHint",
                    'static constexpr auto name = "indexHint";',
                    27,
                )
            ]
        if pattern == "register" and path_prefix == "src/Functions/indexHint.":
            return [
                _chunk(
                    "src/Functions/indexHint.cpp",
                    "DB::REGISTER_FUNCTION",
                    "factory.registerFunction<FunctionIndexHint>();",
                    9,
                )
            ]
        return []

    async def functions_for_paths(self, org, ns, paths, *, limit=400, kinds=()):
        self.calls.append(("functions_for_paths", tuple(paths)))
        p = paths[0]
        if p == "src/Functions/indexHint.h":
            return [
                {
                    "qualified_name": "DB::FunctionIndexHint",
                    "kind": "class",
                    "symbol_id": "ih-c",
                    "path": p,
                    "start_line": 24,
                    "end_line": 76,
                },
                {
                    "qualified_name": "DB::FunctionIndexHint::executeImpl",
                    "kind": "function",
                    "symbol_id": "ih-e",
                    "path": p,
                    "start_line": 60,
                    "end_line": 70,
                },
                {
                    "qualified_name": "DB::FunctionIndexHint::getName",
                    "kind": "function",
                    "symbol_id": "ih-g",
                    "path": p,
                    "start_line": 30,
                    "end_line": 31,
                },
            ]
        if p == "src/AggregateFunctions/AggregateFunctionSumMap.cpp":
            return [
                {
                    "qualified_name": "DB::AggregateFunctionSumMap::getNameImpl",
                    "kind": "function",
                    "symbol_id": "sm-n",
                    "path": p,
                    "start_line": 500,
                    "end_line": 510,
                },
                {
                    "qualified_name": "DB::AggregateFunctionSumMap::keepKey",
                    "kind": "function",
                    "symbol_id": "sm-k",
                    "path": p,
                    "start_line": 412,
                    "end_line": 420,
                },
                {
                    "qualified_name": "DB::registerAggregateFunctionSumMap",
                    "kind": "function",
                    "symbol_id": "sm-reg",
                    "path": p,
                    "start_line": 880,
                    "end_line": 1080,
                },
            ]
        if p == "src/Functions/array/arrayAUC.cpp":
            return [
                {
                    "qualified_name": "DB::REGISTER_FUNCTION",
                    "kind": "function",
                    "symbol_id": "auc-reg",
                    "path": p,
                    "start_line": 554,
                    "end_line": 584,
                }
            ]
        if p == "src/AggregateFunctions/AggregateFunctionQuantile.h":
            return [
                {
                    "qualified_name": "DB::NameQuantileGK",
                    "kind": "struct",
                    "symbol_id": "nq",
                    "path": p,
                    "start_line": 402,
                    "end_line": 402,
                }
            ]
        if p == "src/AggregateFunctions/AggregateFunctionQuantileGK.cpp":
            return [
                {
                    "qualified_name": "DB::registerAggregateFunctionsQuantileGK",
                    "kind": "function",
                    "symbol_id": "gk-reg",
                    "path": p,
                    "start_line": 518,
                    "end_line": 530,
                }
            ]
        return []

    async def edges_for_symbols(self, org, ns, ids, *, direction, limit=200, **kw):
        self.calls.append(("edges", (tuple(ids), direction)))
        if ids == ["auc-reg"]:
            return [
                {"to_symbol_id": "auc-e", "to_qualified_name": "DB::FunctionArrayAUC::executeImpl"},
                {"to_symbol_id": "auc-c", "to_qualified_name": "DB::FunctionArrayAUC::create"},
            ]
        if ids == ["gk-reg"]:
            return [
                {"to_symbol_id": "gk-a", "to_qualified_name": "DB::AggregateFunctionQuantile::add"},
                {
                    "to_symbol_id": "gk-c",
                    "to_qualified_name": "DB::createAggregateFunctionQuantile",
                },
            ]
        return []

    async def symbols_by_ids(self, org, ns, ids):
        self.calls.append(("symbols_by_ids", tuple(ids)))
        names = {
            "auc-e": "DB::FunctionArrayAUC::executeImpl",
            "auc-c": "DB::FunctionArrayAUC::create",
            "gk-a": "DB::AggregateFunctionQuantile::add",
            "gk-c": "DB::createAggregateFunctionQuantile",
        }
        return [
            {
                "symbol_id": i,
                "qualified_name": names[i],
                "kind": "function",
                "path": "src/x.cpp",
                "tier": "treesitter",
            }
            for i in ids
            if i in names
        ]

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        self.calls.append(("symbols_by_name", name))
        return []


@pytest.mark.asyncio
async def test_a_name_constant_in_the_class_binds_the_class_s_methods() -> None:
    repo = _Repo()
    rows = await sn.Resolver(repo, ORG, NS).resolve("indexHint")
    assert [r["symbol_id"] for r in rows] == ["ih-e", "ih-g"]
    assert ("grep_chunks", ('"indexHint"', "src/")) in repo.calls
    # the `sqlName::method` form keeps one method
    rows = await sn.Resolver(repo, ORG, NS).resolve("indexHint::getName")
    assert [r["symbol_id"] for r in rows] == ["ih-g"]


@pytest.mark.asyncio
async def test_a_return_literal_in_a_method_binds_the_enclosing_class() -> None:
    rows = await sn.Resolver(_Repo(), ORG, NS).resolve("sumMap")
    assert [r["symbol_id"] for r in rows] == ["sm-n", "sm-k"]


@pytest.mark.asyncio
async def test_a_template_class_with_only_its_constructor_rowed_falls_back_to_the_file_s_registrar() -> (
    None
):
    """The parser rows `AggregateFunctionSumMap.cpp` with one
       method under the class prefix (the constructor); the registrar's edges
    reach the rest."""

    class _R(_Repo):
        async def functions_for_paths(self, org, ns, paths, *, limit=400, kinds=()):
            p = paths[0]
            return [
                {
                    "qualified_name": "DB::AggregateFunctionSumMap::AggregateFunctionSumMap",
                    "kind": "function",
                    "symbol_id": "sm-ctor",
                    "path": p,
                    "start_line": 100,
                    "end_line": 110,
                },
                {
                    "qualified_name": "DB::registerAggregateFunctionSumMap",
                    "kind": "function",
                    "symbol_id": "sm-reg",
                    "path": p,
                    "start_line": 880,
                    "end_line": 1080,
                },
            ]

        async def edges_for_symbols(self, org, ns, ids, *, direction, limit=200, **kw):
            assert ids == ["sm-reg"] and direction == "callees"
            return [
                {
                    "to_symbol_id": "sm-k",
                    "to_qualified_name": "DB::AggregateFunctionSumMap::keepKey",
                },
                {
                    "to_symbol_id": "mm-k",
                    "to_qualified_name": "DB::AggregateFunctionMinMap::keepKey",
                },
            ]

        async def symbols_by_ids(self, org, ns, ids):
            return [
                {
                    "symbol_id": i,
                    "qualified_name": "DB::AggregateFunctionSumMap::keepKey",
                    "kind": "function",
                    "path": "src/AggregateFunctions/AggregateFunctionSumMap.cpp",
                    "tier": "treesitter",
                }
                for i in ids
            ]

    rows = await sn.Resolver(_R(), ORG, NS).resolve("sumMap")
    assert [r["symbol_id"] for r in rows] == ["sm-ctor", "sm-k"], (
        "the class row plus the registrar's, MinMap excluded"
    )


def test_a_file_level_definition_states_its_class() -> None:
    chunks = [
        _chunk(
            "src/Functions/polygonPerimeter.cpp",
            "",
            'template <>\nconst char * FunctionPolygonPerimeter<SphericalPoint>::name = "polygonPerimeterSpherical";\n',
            100,
        )
    ]
    hits = sn.registration_hits("polygonPerimeterSpherical", chunks)
    assert [(h.shape, h.scope, h.stated_class, h.line) for h in hits] == [
        ("name_constant", "", "FunctionPolygonPerimeter", 101)
    ]
    rows = [
        {
            "qualified_name": "DB::FunctionPolygonPerimeter::executeImpl",
            "kind": "function",
            "symbol_id": "pp-e",
        },
        {"qualified_name": "DB::REGISTER_FUNCTION", "kind": "function", "symbol_id": "r"},
    ]
    assert (
        sn._class_by_bare_name("FunctionPolygonPerimeter", rows) == "DB::FunctionPolygonPerimeter"
    )
    assert sn._class_by_bare_name("Nope", rows) == ""
    # inside a class body nothing is stated
    assert (
        sn.registration_hits(
            "indexHint",
            [
                _chunk(
                    "src/Functions/indexHint.h",
                    "DB::FunctionIndexHint",
                    'static constexpr auto name = "indexHint";',
                )
            ],
        )[0].stated_class
        == ""
    )


@pytest.mark.asyncio
async def test_a_return_literal_names_its_class_even_when_the_class_is_not_rowed() -> None:
    """`ContingencyData::getName` returns "contingency"; the parser rows neither
    the struct nor its methods, but the file's registrar reaches them."""

    class _R(_Repo):
        async def grep_chunks(
            self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
        ):
            if pattern == '"contingency"':
                return [
                    _chunk(
                        "src/AggregateFunctions/AggregateFunctionContingencyCoefficient.cpp",
                        "DB::ContingencyData::getName",
                        'return "contingency";',
                        18,
                    )
                ]
            return []

        async def functions_for_paths(self, org, ns, paths, *, limit=400, kinds=()):
            return [
                {
                    "qualified_name": "DB::registerAggregateFunctionsStatisticsSimple",
                    "kind": "function",
                    "symbol_id": "st-reg",
                    "path": paths[0],
                    "start_line": 30,
                    "end_line": 60,
                }
            ]

        async def edges_for_symbols(self, org, ns, ids, *, direction, limit=200, **kw):
            assert ids == ["st-reg"]
            return [
                {"to_symbol_id": "ct-a", "to_qualified_name": "DB::ContingencyData::add"},
                {"to_symbol_id": "cr-a", "to_qualified_name": "DB::CramersVData::add"},
            ]

        async def symbols_by_ids(self, org, ns, ids):
            return [
                {
                    "symbol_id": i,
                    "qualified_name": "DB::ContingencyData::add",
                    "kind": "function",
                    "path": "src/x.cpp",
                    "tier": "treesitter",
                }
                for i in ids
            ]

    rows = await sn.Resolver(_R(), ORG, NS).resolve("contingency")
    assert [r["symbol_id"] for r in rows] == ["ct-a"]


@pytest.mark.asyncio
async def test_sql_names_for_declines_a_free_function_and_a_plan_step_literal() -> None:
    class _R(_Repo):
        async def grep_chunks(
            self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
        ):
            if pattern == "register" and path_prefix.startswith("src/Processors/QueryPlan/"):
                return [
                    _chunk(
                        "src/Processors/QueryPlan/LimitStep.cpp",
                        "DB::registerLimitStep",
                        'registry.registerStep("Limit", &LimitStep::deserialize);',
                    )
                ]
            return []

    r = sn.Resolver(_R(), ORG, NS)
    # a free function's parent is the namespace: no class, no names
    assert (
        await r.sql_names_for(
            [
                {
                    "qualified_name": "DB::validatePuffinFooterFlags",
                    "kind": "function",
                    "path": "src/Formats/Puffin.cpp",
                }
            ]
        )
        == []
    )
    # a registered literal outside the SQL-visible registries is not a SQL name
    assert (
        await r.sql_names_for(
            [
                {
                    "qualified_name": "DB::LimitStep::getLimitForSorting",
                    "kind": "function",
                    "path": "src/Processors/QueryPlan/LimitStep.cpp",
                }
            ]
        )
        == []
    )
    assert (
        sn.sql_names_from_chunks(
            "DB::LimitStep",
            [],
            [
                _chunk(
                    "src/Processors/QueryPlan/LimitStep.cpp",
                    "DB::registerLimitStep",
                    'registerStep("Limit", x);',
                )
            ],
        )
        == []
    )
    assert sn.sql_names_from_chunks(
        "DB::StorageLog",
        [],
        [
            _chunk(
                "src/Storages/StorageLog.cpp",
                "DB::registerStorageLog",
                'factory.registerStorage("Log", x);',
            )
        ],
    ) == ["Log"]


@pytest.mark.asyncio
async def test_a_vocabulary_term_is_demoted_to_a_short_name_grep(plane) -> None:
    """`any` is a registered aggregate; it is also in thousands of test files."""

    async def grep_files(
        org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
    ):
        if pattern == "arrayJoin":
            return [{"path": f"{path_prefix}00000_control.sql", "chunks": 1, "first_line": 1}], 1
        if pattern == "any" and path_prefix == ST:
            return [{"path": f"{ST}00001_any.sql", "chunks": 1, "first_line": 1}], 4000
        return [], 0

    plane.grep_files = grep_files
    terms = {"addBatchSinglePlace": cc.SHORT_NAME_GREP, "any": cc.EXACT_SCOPED}
    ordered, total = await h.claim_tests(
        plane, ORG, NS, "symbol", "addBatchSinglePlace", [], limit=5, terms=terms
    )
    assert total == 4000
    assert [(r["test_path"], r["tier"]) for r in ordered] == [
        (f"{ST}00001_any.sql", cc.SHORT_NAME_GREP)
    ]
    assert terms["any"] == cc.SHORT_NAME_GREP, (
        "the payload's search_terms say what the hits counted as"
    )
    assert cc.coverage_verdict(ordered, control_ok=True) == cc.WEAK_EVIDENCE


@pytest.mark.asyncio
async def test_a_registration_call_binds_through_the_registrar_s_edges() -> None:
    repo = _Repo()
    rows = await sn.Resolver(repo, ORG, NS).resolve("arrayAUCPR")
    assert [r["symbol_id"] for r in rows] == ["auc-e", "auc-c"]
    assert ("edges", (("auc-reg",), "callees")) in repo.calls


@pytest.mark.asyncio
async def test_a_name_struct_binds_through_the_registrar_that_names_it() -> None:
    repo = _Repo()
    rows = await sn.Resolver(repo, ORG, NS).resolve("quantileGK")
    # the registrar also reaches its creator function; the class is what binds
    assert [r["symbol_id"] for r in rows] == ["gk-a"]
    assert ("grep_chunks", ("NameQuantileGK", "src/")) in repo.calls


@pytest.mark.asyncio
async def test_names_no_registration_holds_decline_cheaply() -> None:
    repo = _Repo()
    assert await sn.Resolver(repo, ORG, NS).resolve("std::vector") == []
    assert repo.calls == [], "a compiler namespace never reaches the index"
    assert await sn.Resolver(repo, ORG, NS).resolve("make_drop_context") == []
    reads = [c for c in repo.calls if c[0] != "symbols_by_name"]
    assert reads == [("grep_chunks", ('"make_drop_context"', "src/"))], (
        "one literal read, then silence"
    )
    assert [c[1] for c in repo.calls if c[0] == "symbols_by_name"] == [
        "FunctionMake_drop_context",
        "AggregateFunctionMake_drop_context",
        "TableFunctionMake_drop_context",
    ], "the convention classes are keyed reads before the grep"


@pytest.mark.asyncio
async def test_sql_names_for_a_class_reads_both_files_of_the_stem() -> None:
    repo = _Repo()
    rows = [
        {
            "qualified_name": "DB::FunctionIndexHint::executeImpl",
            "kind": "function",
            "path": "src/Functions/indexHint.h",
        }
    ]
    assert await sn.Resolver(repo, ORG, NS).sql_names_for(rows) == ["indexHint"]
    assert ("grep_chunks", ("name", "src/Functions/indexHint.")) in repo.calls
    assert ("grep_chunks", ("register", "src/Functions/indexHint.")) in repo.calls
    assert (
        await sn.Resolver(repo, ORG, NS).sql_names_for(
            [{"qualified_name": "f", "kind": "function", "path": "src/a.cpp"}]
        )
        == []
    )


@pytest.mark.asyncio
async def test_resolve_tolerant_runs_the_registration_rule_last() -> None:
    repo = _Repo()
    rows, by = await sf.resolve_tolerant(repo, ORG, NS, "sumMap")
    assert by == sf.RESOLVED_REGISTRATION == "registration"
    assert [r["symbol_id"] for r in rows] == ["sm-n", "sm-k"]
    rows, by = await sf.resolve_tolerant(repo, ORG, NS, "std::vector")
    assert (rows, by) == ([], "")
    assert sf.mark(rows, by) == []


# ---- the callers handler: registration seeds skip the ambiguity guard --------------


@pytest.mark.asyncio
async def test_callers_of_a_sql_name_walk_the_class_and_say_so(monkeypatch) -> None:
    import server.code.app.symbol.graph as cs
    from server.code.api.models_symbol import CodeCallersInput

    seeds = [
        {
            "symbol_id": f"m{i}",
            "qualified_name": f"DB::AggregateFunctionSumMap::m{i}",
            "path": "src/a.cpp",
            "tier": "treesitter",
            "kind": "function",
            "start_line": i,
            "end_line": i,
            "resolved_by": "registration",
        }
        for i in range(8)
    ]

    async def _resolve(repo, org, ns, *, symbol_id, name):
        return seeds

    class _R:
        async def edges_for_symbols(self, *a, **k):
            return []

        async def call_guards_for_callers(self, *a, **k):
            return {}

        def __getattr__(self, name):
            async def _none(*a, **k):
                return [] if name != "class_of_method" else None

            return _none

    monkeypatch.setattr(cs, "_resolve_symbols", _resolve)
    monkeypatch.setattr(cs, "require_code_intel", lambda: None)
    monkeypatch.setattr(cs, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(cs, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(cs, "CodeRepo", lambda: _R())

    async def _no_dispatch(repo, org, ns, seeds):
        return []

    monkeypatch.setattr(cs, "_dispatch_seeds", _no_dispatch)
    monkeypatch.setattr(cs.settings, "code_callers_max_distinct_seeds", 5)
    out = await cs.handle_code_callers(CodeCallersInput(org=ORG, namespace=NS, name="sumMap"))
    assert isinstance(out, CodeCallersOutput)
    assert out.ambiguous is False and out.resolved_by == "registration"
    assert len(out.seed_symbols) == 8, "eight methods of one class are one class, not an ambiguity"


def test_output_models_disclose_and_drop_the_unset_field() -> None:
    d = CodeCoverageClaimOutput(
        subject="sumMap",
        subject_kind="symbol",
        verdict="covered_by",
        strongest_tier="exact_scoped",
        search_terms={"sumMap": "exact_scoped"},
        resolved_by="registration",
    ).model_dump()
    assert d["resolved_by"] == "registration" and d["strongest_tier"] == "exact_scoped"
    assert "resolved_by" not in CodeCoverageClaimOutput(subject="x").model_dump()


# ---- the coverage handler over a stub repository ---------------------------------------


class _CovRepo(_Repo):
    def __init__(self, *, control_hit: bool = True) -> None:
        super().__init__()
        self.control_hit = control_hit
        self.greps: list[tuple[str, str]] = []

    async def grep_files(
        self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
    ):
        self.greps.append((pattern, path_prefix))
        if pattern == "arrayJoin":
            return (
                [{"path": f"{path_prefix}00000_control.sql", "chunks": 1, "first_line": 1}]
                if self.control_hit
                else []
            ), int(self.control_hit)
        if pattern == "readRow" and path_prefix == IT:
            return [{"path": f"{IT}test_storage_kafka/test.py", "chunks": 2, "first_line": 154}], 1
        if pattern == "indexHint" and path_prefix == ST:
            return [{"path": f"{ST}01739_index_hint.sql", "chunks": 1, "first_line": 1}], 1
        if pattern == "executeImpl" and path_prefix == ST:
            return [{"path": f"{ST}00999_unrelated.sql", "chunks": 1, "first_line": 3}], 1
        if pattern == "sumMap" and path_prefix == SF:
            return [{"path": f"{SF}00040_sum_map_hits.sql", "chunks": 1, "first_line": 2}], 1
        if pattern == "sumMap" and path_prefix == ST:
            return [{"path": f"{ST}00502_sum_map.sql", "chunks": 1, "first_line": 1}], 1
        if pattern == "geohashesInBox" and path_prefix == ST:
            return [{"path": f"{ST}00972_geohashesInBox.sql", "chunks": 1, "first_line": 1}], 1
        return [], 0

    async def grep_chunks(
        self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
    ):
        if pattern == '"geohashesInBox"':
            return [
                _chunk(
                    "src/Functions/geohashesInBox.cpp",
                    "DB::FunctionGeohashesInBox",
                    'static constexpr auto name = "geohashesInBox";',
                    30,
                )
            ]
        return await super().grep_chunks(
            org,
            ns,
            pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            path_prefix=path_prefix,
            limit=limit,
            scope=scope,
        )

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        self.calls.append(("symbols_by_name", name))
        if name == "geohashesInBox":
            return [
                {
                    "path": "src/Functions/GeoHash.cpp",
                    "qualified_name": "DB::geohashesInBox",
                    "kind": "function",
                    "symbol_id": "gh",
                }
            ]
        if name == "DB::NpyRowInputFormat::readRow":
            return [
                {
                    "path": "src/Processors/Formats/Impl/NpyRowInputFormat.cpp",
                    "qualified_name": name,
                    "kind": "function",
                    "symbol_id": "npy",
                }
            ]
        if name == "DB::FunctionIndexHint::executeImpl":
            return [
                {
                    "path": "src/Functions/indexHint.h",
                    "qualified_name": name,
                    "kind": "function",
                    "symbol_id": "ih-e",
                }
            ]
        return []

    async def coverage_rows_for(self, org, ns, paths, *, limit=2000):
        return []

    async def coverage_files_for(self, org, ns, paths):
        return {}

    async def test_map_detail(self, org, ns, paths, *, limit=60):
        if "src/Processors/Formats/Impl/NpyRowInputFormat.cpp" in paths:
            return [
                {
                    "src_path": paths[0],
                    "test_path": f"{ST}02895_npy_format.sql",
                    "method": "path_convention",
                    "confidence": 0.5,
                    "evidence": "npy",
                }
            ]
        if "src/Functions/indexHint.h" in paths:
            return [
                {
                    "src_path": paths[0],
                    "test_path": f"{ST}00945_bloom_filter_index.sql",
                    "method": "content_reference",
                    "confidence": 0.6,
                    "evidence": "indexHint,arrayJoin",
                },
                {
                    "src_path": paths[0],
                    "test_path": f"{ST}02226_analyzer_or_like_combine.sql",
                    "method": "content_reference",
                    "confidence": 0.4,
                    "evidence": "arrayJoin",
                },
            ]
        return []


@pytest.fixture
def plane(monkeypatch):
    h.reset_control_cache()  # The control is kept per namespace
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    repo = _CovRepo()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    return repo


@pytest.mark.asyncio
async def test_a_short_method_grep_alone_is_weak_evidence(plane) -> None:
    """`NpyRowInputFormat::readRow` must not read covered_by via `readRow` hits
    in Kafka tests. The rows stay, the tier names them, the verdict does not."""
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="DB::NpyRowInputFormat::readRow")
    )
    assert out.verdict == cc.WEAK_EVIDENCE and out.strongest_tier == cc.SHORT_NAME_GREP
    assert out.search_terms == {
        "readRow": cc.SHORT_NAME_GREP,
        "NpyRowInputFormat::readRow": cc.EXACT_SCOPED,
    }
    tiers = {t["test_path"]: t["tier"] for t in out.tests}
    assert tiers[f"{IT}test_storage_kafka/test.py"] == cc.SHORT_NAME_GREP
    assert tiers[f"{ST}02895_npy_format.sql"] == cc.PATH_CONVENTION
    kafka = next(t for t in out.tests if t["test_path"].startswith(IT))
    assert kafka["evidence"] == "names readRow at line 154", "the evidence says what was grepped"
    # every literal was searched in all three trees
    assert {p for (pat, p) in plane.greps if pat == "readRow"} == {ST, SF, IT}
    assert out.resolved_by == "" and "resolved_by" not in out.model_dump()


@pytest.mark.asyncio
async def test_a_c_symbol_is_covered_through_the_sql_name_its_class_registers(plane) -> None:
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="DB::FunctionIndexHint::executeImpl")
    )
    assert out.verdict == cc.COVERED_BY and out.strongest_tier == cc.EXACT_SCOPED
    assert out.search_terms["indexHint"] == cc.EXACT_SCOPED
    assert out.search_terms["executeImpl"] == cc.SHORT_NAME_GREP
    by = {t["test_path"]: t for t in out.tests}
    # the content reference whose evidence names indexHint is scoped; the one naming only arrayJoin is not
    assert by[f"{ST}00945_bloom_filter_index.sql"]["tier"] == cc.EXACT_SCOPED
    assert by[f"{ST}02226_analyzer_or_like_combine.sql"]["tier"] == cc.FILE_REFERENCE
    assert by[f"{ST}01739_index_hint.sql"]["tier"] == cc.EXACT_SCOPED
    assert by[f"{ST}01739_index_hint.sql"]["evidence"] == "names indexHint at line 1"
    assert by[f"{ST}00999_unrelated.sql"]["tier"] == cc.SHORT_NAME_GREP
    assert out.tests[0]["tier"] == cc.EXACT_SCOPED, "strongest first"


@pytest.mark.asyncio
async def test_a_sql_name_subject_binds_through_the_registration_and_searches_the_stateful_tree(
    plane,
) -> None:
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="sumMap")
    )
    assert out.resolved_by == "registration"
    assert out.src_paths == ["src/AggregateFunctions/AggregateFunctionSumMap.cpp"]
    assert out.search_terms == {"sumMap": cc.EXACT_SCOPED}
    assert out.verdict == cc.COVERED_BY
    assert {t["test_path"] for t in out.tests} == {
        f"{SF}00040_sum_map_hits.sql",
        f"{ST}00502_sum_map.sql",
    }
    assert all(t["tier"] == cc.EXACT_SCOPED for t in out.tests)


@pytest.mark.asyncio
async def test_a_bare_name_that_is_both_a_free_function_and_a_registered_name_is_exact(
    plane,
) -> None:
    """`geohashesInBox` binds as a C++ function, and its own grep is the
    SQL name a test writes — exact, not a short-name grep (`arrayJoin`, the
    control symbol, has the same shape)."""
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="geohashesInBox")
    )
    assert out.resolved_by == "" and out.src_paths == ["src/Functions/GeoHash.cpp"]
    assert out.search_terms == {"geohashesInBox": cc.EXACT_SCOPED}
    assert out.verdict == cc.COVERED_BY and out.strongest_tier == cc.EXACT_SCOPED
    assert out.tests[0]["test_path"] == f"{ST}00972_geohashesInBox.sql"


@pytest.mark.asyncio
async def test_weak_evidence_is_never_stated_without_the_control(plane) -> None:
    plane.control_hit = False
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="DB::NpyRowInputFormat::readRow")
    )
    assert out.verdict == cc.UNKNOWN and out.control["ok"] is False
    assert out.tests, "the evidence is still returned"


def test_the_brief_s_tests_verdict_knows_weak_evidence() -> None:
    from server.code.app.index import review_brief as rb

    assert rb.tests_verdict([{"kind": "symbol", "verdict": "weak_evidence"}]) == "weak_evidence"
    assert (
        rb.tests_verdict(
            [
                {"kind": "symbol", "verdict": "weak_evidence"},
                {"kind": "symbol", "verdict": "covered_by"},
            ]
        )
        == "partly_covered"
    )
    assert (
        rb.tests_verdict(
            [
                {"kind": "symbol", "verdict": "weak_evidence"},
                {"kind": "symbol", "verdict": "uncovered"},
            ]
        )
        == "uncovered"
    )
    assert (
        rb.decisions_of({"tests": {"verdict": "weak_evidence"}}).get("uncovered_symbol") is False
        if hasattr(rb, "decisions_of")
        else True
    )
