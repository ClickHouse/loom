"""Scip-typescript symbols convert to the tree-sitter tier's
module-scoped names; kinds from the definition's shape; parameters, meta
members, externals and locals are skipped; type references are uses_type."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "scip_harness" / "scip_to_delta.py"
spec = importlib.util.spec_from_file_location("scip_to_delta", _HARNESS)
assert spec and spec.loader
c = importlib.util.module_from_spec(spec)
sys.modules["scip_to_delta"] = c
spec.loader.exec_module(c)

_PKG = "scip-typescript npm @langfuse/shared 1.0.0 "
_WEB = "scip-typescript npm web 4.37.0 "


class _Occ:
    def __init__(self, rng, enclosing=(), roles=1):
        self.range = list(rng)
        self.enclosing_range = list(enclosing)
        self.symbol_roles = roles


@pytest.fixture
def ctx():
    before = c._TS
    c._TS = c._TsContext(
        {"@langfuse/shared": "packages/shared", "web": "web", "langfuse": ""}, c._SourceLines(None)
    )
    yield c._TS
    c._TS = before


def test_descriptors_tokenize_every_shape() -> None:
    d = c._ts_descriptors("src/server/`scores.ts`/StringFilter#`<constructor>`().(filters)")
    assert d == [
        ("src", "/", ""),
        ("server", "/", ""),
        ("scores.ts", "/", ""),
        ("StringFilter", "#", ""),
        ("<constructor>", "()", ""),
        ("filters", "param", ""),
    ]
    assert c._ts_descriptors("`observations.ts`/observationId0:") == [
        ("observations.ts", "/", ""),
        ("observationId0", ":", ""),
    ]
    assert c._ts_descriptors("`x.ts`/f().[T]")[-1] == ("T", "tparam", "")


def test_symbols_map_to_the_treesitter_tiers_names(ctx) -> None:
    ctx.def_kinds[_PKG + "src/server/repositories/`scores.ts`/getScoreById."] = "function"
    ctx.def_kinds[_PKG + "src/domain/`scores.ts`/ScoreDomain#"] = "type"
    assert c._parse_symbol(_PKG + "src/server/repositories/`scores.ts`/getScoreById.") == (
        "packages.shared.src.server.repositories.scores::getScoreById",
        "function",
        "",
    )
    assert c._parse_symbol(_PKG + "src/server/repositories/`scores.ts`/LIMIT.") == (
        "packages.shared.src.server.repositories.scores::LIMIT",
        "const",
        "",
    )
    assert c._parse_symbol(_PKG + "src/domain/`scores.ts`/ScoreDomain#") == (
        "packages.shared.src.domain.scores::ScoreDomain",
        "type",
        "",
    )
    assert c._parse_symbol(_PKG + "src/q/`clickhouse-filter.ts`/StringFilter#apply().") == (
        "packages.shared.src.q.clickhouse-filter::StringFilter::apply",
        "function",
        "()",
    )
    assert c._parse_symbol(
        _PKG + "src/q/`clickhouse-filter.ts`/StringFilter#`<constructor>`()."
    ) == ("packages.shared.src.q.clickhouse-filter::StringFilter::constructor", "function", "()")
    assert c._parse_symbol(
        _WEB + "src/server/api/routers/`observations.ts`/observationsRouter."
    ) == ("web.src.server.api.routers.observations::observationsRouter", "const", "")
    assert c._parse_symbol("scip-typescript npm langfuse 4.37.0 `turbo.json`/x.") is None
    root = c._parse_symbol("scip-typescript npm langfuse 4.37.0 scripts/`run.mjs`/main().")
    assert root == ("scripts.run::main", "function", "()")


def test_skipped_shapes(ctx) -> None:
    for sym in (
        _PKG + "src/server/`scores.ts`/",  # the file itself
        _PKG + "src/server/`scores.ts`/StringFilter#push().(filter)",  # a parameter
        _PKG + "src/server/`scores.ts`/observationId0:",  # an object-type member
        _PKG + "src/server/`scores.ts`/f().[T]",  # a type parameter
        "scip-typescript npm zod 4.3.6 `index.d.cts`/object().",  # an external package
        "scip-typescript npm . . `external.d.cts`/",  # an untyped external
        "local 12",
    ):
        assert c._parse_symbol(sym) is None, sym


def test_definition_kinds_come_from_the_shape_and_the_line() -> None:
    lines = [
        "export interface Props {",
        "export type Score = string;",
        "export enum Color {",
        "export class Foo {",
        "export const f = async () => {",
        "const LIMIT = 10;",
        "export default function Page() {",
    ]
    hash_sym = _PKG + "`a.ts`/X#"
    for i, kind in ((0, "interface"), (1, "type"), (2, "enum"), (3, "class")):
        assert c._ts_def_kind(hash_sym, _Occ((i, 0, 1)), lines) == kind
    term = _PKG + "`a.ts`/x."
    assert c._ts_def_kind(term, _Occ((4, 13, 14), enclosing=(4, 17, 9, 1)), lines) == "function"
    assert c._ts_def_kind(term, _Occ((5, 6, 11)), lines) == "const"
    # A class property is a `field`, a module const a `const`
    assert c._ts_def_kind(_PKG + "`a.ts`/Filter#field.", _Occ((5, 6, 11)), lines) == "field"
    assert c._ts_def_kind(_PKG + "`a.ts`/f().", _Occ((6, 0, 1)), lines) == "function"
    assert c._ts_def_kind(hash_sym, _Occ((0, 0, 1)), None) == "class"  # no source: a class


def test_relationship_edges_cover_interfaces_and_the_workspace_map(tmp_path: Path) -> None:
    assert c._INHERIT_KINDS == frozenset({"class", "struct", "interface", "type", "enum"})
    (tmp_path / "package.json").write_text('{"name": "langfuse"}')
    (tmp_path / "packages" / "shared").mkdir(parents=True)
    (tmp_path / "packages" / "shared" / "package.json").write_text('{"name": "@langfuse/shared"}')
    (tmp_path / "node_modules" / "zod").mkdir(parents=True)
    (tmp_path / "node_modules" / "zod" / "package.json").write_text('{"name": "zod"}')
    assert c.ts_package_dirs(tmp_path) == {"langfuse": "", "@langfuse/shared": "packages/shared"}


@pytest.mark.asyncio
async def test_only_call_edges_are_dropped_on_declaration_lines() -> None:
    """The cross-tier declaration test exists for a CALL
    recorded on a class-body declaration line; an `inherits` edge sits on
    the class line and a `uses_type` from an interface field on the
    interface's — both must survive the apply."""
    from unittest.mock import AsyncMock, patch

    from server.code.app.index.scip_delta import (
        ScipDelta,
        ScipEdge,
        ScipFile,
        apply_delta,
    )

    path = "packages/shared/src/q/filter.ts"
    delta = ScipDelta(
        delta_version=1,
        commit="c" * 40,
        files=[
            ScipFile(
                path=path,
                edges=[
                    ScipEdge(
                        edge_kind="inherits",
                        from_qualified_name="m::StringFilter",
                        to_qualified_name="m::Filter",
                        line=46,
                    ),
                    ScipEdge(
                        edge_kind="uses_type",
                        from_qualified_name="m::Props",
                        to_qualified_name="m::Score",
                        line=3,
                    ),
                    ScipEdge(
                        edge_kind="calls",
                        from_qualified_name="m::StringFilter::apply",
                        to_qualified_name="m::helper",
                        line=47,
                    ),  # a declaration line: dropped
                    ScipEdge(
                        edge_kind="calls",
                        from_qualified_name="m::StringFilter::apply",
                        to_qualified_name="m::helper",
                        line=80,
                    ),  # inside the method: kept
                ],
            )
        ],
    )
    repo = AsyncMock()
    repo.live_symbols_for_path.return_value = []
    repo.live_edges_for_path.return_value = []
    # a class skeleton over 46–150 with one method body 72–149
    repo.chunk_spans_for_path.return_value = [
        ("class_skeleton", 46, 150),
        ("function", 72, 149),
        ("class_skeleton", 1, 10),
    ]

    class _Cfg:
        path_prefixes = ("packages/",)

    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = await apply_delta(repo, "org", "ns", delta)
    assert stats.edges_dropped_declarations == 1 and stats.edges_new == 3
    inserted = [
        r
        for call in repo.insert_rows.await_args_list
        if call.args[0] == "code_edges"
        for r in call.args[1]
    ]
    assert sorted((r["edge_kind"], r["line"]) for r in inserted) == [
        ("calls", 80),
        ("inherits", 46),
        ("uses_type", 3),
    ]


def test_a_wrapped_component_is_a_function_in_the_compiler_tier() -> None:
    """`export const X = forwardRef<P>(` has no enclosing
    range and no `=>` on its line; the wrapper names the kind."""
    lines = [
        "",
        "export const BaseButton = forwardRef<HTMLButtonElement, Props>(",
        "export const Icon = React.memo(function Icon(p) {",
        "export const t = memo(keys);",
    ]
    term = _PKG + "`a.tsx`/BaseButton."
    assert c._ts_def_kind(term, _Occ((1, 13, 23)), lines) == "function"
    assert c._ts_def_kind(_PKG + "`a.tsx`/Icon.", _Occ((2, 13, 17)), lines) == "function"
    assert c._ts_def_kind(_PKG + "`a.tsx`/t.", _Occ((3, 13, 14)), lines) == "function"


def test_a_wrapped_component_gets_the_formatter_statement_span() -> None:
    """No enclosing_range on `const X = forwardRef(`; the
    statement span runs to the column-0 closer so the calls inside the
    component attribute to it."""
    lines = [
        "import x from 'y';",
        "export const Grid = forwardRef<P>(",
        "  (props, ref) => {",
        "    return <Cell />;",
        "  }",
        ");",
        "",
        "export const one = (): T => ({ a: 1 });",
        "export const open = memo(",
        "  () => 1",
        "export function next() {}",
    ]
    assert c._ts_statement_span(lines, 2) == (2, 6)
    assert c._ts_statement_span(lines, 8) == (0, 0)  # one-liner
    assert c._ts_statement_span(lines, 9) == (0, 0)  # no closer before the next token


@pytest.mark.asyncio
async def test_calls_inside_a_wrapped_component_are_not_declarations():
    """The chunker files `const Grid = forwardRef((…) => …)`
    as skeleton windows; the declaration test must also read the parser
    tier's function SYMBOL span, or every call inside the component is
    dropped as a declaration."""
    from unittest.mock import AsyncMock, patch

    from server.code.app.index.scip_delta import ScipDelta, ScipEdge, ScipFile, apply_delta

    delta = ScipDelta(
        delta_version=1,
        commit="c" * 40,
        files=[
            ScipFile(
                path="src/components/Grid/Grid.tsx",
                symbols=[],
                edges=[
                    ScipEdge(
                        edge_kind="calls",
                        from_qualified_name="src.components.Grid.Grid::Grid",
                        to_qualified_name="src.components.Grid.Cell::Cell",
                        line=808,
                    ),
                    ScipEdge(
                        edge_kind="calls",
                        from_qualified_name="src.components.Grid.Grid::Grid",
                        to_qualified_name="src.components.Grid.types::Props::onCopy",
                        line=47,
                    ),
                ],
            )
        ],
    )
    repo = AsyncMock()

    async def _live(org, ns, path, tier):
        if tier == "treesitter":
            return [
                {
                    "qualified_name": "src.components.Grid.Grid::Grid",
                    "kind": "function",
                    "start_line": 103,
                    "end_line": 897,
                    "symbol_id": "x",
                }
            ]
        return []

    repo.live_symbols_for_path.side_effect = _live
    repo.live_edges_for_path.return_value = []
    # the chunker's view: skeleton windows over the component, a const above it
    repo.chunk_spans_for_path.return_value = [
        ("class_skeleton", 47, 51),
        ("class_skeleton", 103, 311),
        ("class_skeleton", 312, 897),
    ]

    class _Cfg:
        path_prefixes = ("src/",)

    with patch("server.code.domain.index.repos.repo_for_namespace", return_value=_Cfg()):
        stats = await apply_delta(repo, "org", "ns", delta)
    # line 808 is inside the function symbol: a call; line 47 sits in a
    # skeleton window and no function: a declaration
    assert stats.edges_new == 1 and stats.edges_dropped_declarations == 1


def test_a_typeof_query_is_a_type_reference_not_a_call() -> None:
    """`ComponentProps<typeof AIAssistedInput>` is no call:
    read as one it puts false callers on the component; the operand of `typeof` is a
    uses_type."""
    lines = [
        "  nameAIAssistance: ComponentProps<typeof AIAssistedInput>['aiAssistance'];",
        "    typeof AIAssistedInput",
        "  return <AIAssistedInput />;",
    ]
    assert c._ts_typeof_ref(lines, (0, 42, 57)) is True
    assert c._ts_typeof_ref(lines, (1, 11, 26)) is True
    assert c._ts_typeof_ref(lines, (2, 10, 25)) is False
    assert c._ts_typeof_ref(None, (0, 42, 57)) is False
