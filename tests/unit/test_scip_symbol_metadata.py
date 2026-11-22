"""A stable compiler symbol identity still refreshes changed serving metadata."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.index import scip_delta as sd
from server.code.store.index import CodeRepo, build_row


def _symbol(**changes: Any) -> sd.ScipSymbol:
    return sd.ScipSymbol(
        **{
            "qualified_name": "project::run",
            "name": "run",
            "kind": "function",
            "signature": "(int value)",
            "line_start": 2,
            "line_end": 8,
            "doc": "Original notes",
            "decl_paths": ["run.h"],
            "scip_symbol": "producer old run",
            **changes,
        }
    )


def _live(symbol: sd.ScipSymbol) -> dict[str, Any]:
    return build_row(
        "code_symbols",
        org="org",
        namespace="code",
        tier="scip",
        path="run.cpp",
        qualified_name=symbol.qualified_name,
        symbol_id=sd.symbol_id(symbol.qualified_name, symbol.signature),
        name=symbol.name,
        kind=symbol.kind,
        signature=symbol.signature,
        start_line=symbol.line_start,
        end_line=symbol.line_end,
        doc_comment=symbol.doc[:2000],
        decl_paths=symbol.decl_paths,
        scip_symbol=symbol.scip_symbol,
        indexed_commit="a" * 40,
    )


async def _reconcile(live: dict[str, Any], symbol: sd.ScipSymbol) -> sd._RoundSink:
    sink = sd._RoundSink()
    await sd._apply_file(
        CodeRepo(),
        "org",
        "code",
        "b" * 40,
        sd.ScipFile(path="run.cpp", symbols=[symbol]),
        sd.ScipApplyStats(),
        existing=[live],
        existing_edges=[],
        spans=[],
        fn_symbol_spans=[],
        sink=sink,
    )
    return sink


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"doc": "Current notes"},
        {"name": "current_run"},
        {"decl_paths": ["current.h"]},
        {"signature": "(int  value)"},
    ],
)
async def test_metadata_refreshes_without_changing_identity_or_span(
    changes: dict[str, Any],
) -> None:
    before = _symbol()
    after = _symbol(**changes, scip_symbol="producer current run")
    live = _live(before)
    assert before.line_start == after.line_start and before.line_end == after.line_end
    assert before.qualified_name == after.qualified_name and before.signature != ""
    assert sd.symbol_id(before.qualified_name, before.signature) == sd.symbol_id(
        after.qualified_name, after.signature
    )
    result = await _reconcile(live, after)
    (row,) = result.symbols
    assert row["symbol_id"] == live["symbol_id"] and row["indexed_commit"] == "b" * 40
    assert row["name"] == after.name and row["signature"] == after.signature
    assert row["doc_comment"] == after.doc[:2000] and row["decl_paths"] == after.decl_paths
    assert row["scip_symbol"] == after.scip_symbol
    assert not (await _reconcile(row, after)).symbols


@pytest.mark.asyncio
async def test_provenance_changes_alone_do_not_rewrite_identical_metadata() -> None:
    live = _live(_symbol())
    assert not (await _reconcile(live, _symbol(scip_symbol="producer current run"))).symbols


@pytest.mark.asyncio
async def test_unstored_documentation_suffix_does_not_rewrite_the_row() -> None:
    live = _live(_symbol(doc="x" * 2000 + "before"))
    assert not (await _reconcile(live, _symbol(doc="x" * 2000 + "after"))).symbols
