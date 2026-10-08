"""SCIP `is_implementation` relationships become overrides/inherits edges, and
code.callers prefers them over the name-based dispatch heuristic."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "scip_to_delta", Path(__file__).resolve().parents[2] / "scripts/scip_harness/scip_to_delta.py"
)
scip_to_delta = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["scip_to_delta"] = scip_to_delta
_SPEC.loader.exec_module(scip_to_delta)

# scip-clang symbol grammar as the converter's _parse_symbol expects it
BASE_M = "cxx . . $ DB/IStorage#read(a81648bb8024aece)."
OVR_M = "cxx . . $ DB/StorageReplicatedMergeTree#read(a81648bb8024aece)."
BASE_T = "cxx . . $ DB/IStorage#"
DER_T = "cxx . . $ DB/StorageReplicatedMergeTree#"


def _si(symbol: str, *impl: str) -> NS:
    return NS(
        symbol=symbol,
        relationships=[NS(symbol=s, is_implementation=True) for s in impl],
        documentation=[],
    )


def test_relationship_edges_split_overrides_and_inherits() -> None:
    parsed = scip_to_delta._parse_symbol(OVR_M)
    assert parsed is not None and parsed[1] == "function", parsed
    edges = scip_to_delta._relationship_edges(
        [_si(OVR_M, BASE_M), _si(DER_T, BASE_T), _si(BASE_M)],
        {OVR_M: 4200, DER_T: 40},
    )
    kinds = {
        (e["edge_kind"], e["from_qualified_name"], e["to_qualified_name"], e["line"]) for e in edges
    }
    assert (
        "overrides",
        "DB::StorageReplicatedMergeTree::read",
        "DB::IStorage::read",
        4200,
    ) in kinds
    assert ("inherits", "DB::StorageReplicatedMergeTree", "DB::IStorage", 40) in kinds
    assert len(edges) == 2  # a symbol without relationships contributes nothing


def test_relationship_edges_skip_mixed_and_unparseable() -> None:
    edges = scip_to_delta._relationship_edges(
        [_si(OVR_M, BASE_T), _si("local 3", BASE_M), _si(OVR_M, "cxx . . $ DB/")],
        {},
    )
    assert edges == []


class _Repo:
    """Override seed with an exact `overrides` edge indexed; the heuristic
    fallbacks must not run (methods_named would raise)."""

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200):
        if edge_kinds == ["overrides"] and ids == ["ovr"] and direction == "callees":
            return [
                {
                    "to_symbol_id": "base1",
                    "to_qualified_name": "DB::IStorage::read",
                    "path": "src/Storages/IStorage.h",
                }
            ]
        return []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        return []

    async def methods_named(self, org, ns, method, *, limit=300):
        raise AssertionError("heuristic fallback must not run when overrides edges exist")


@pytest.mark.asyncio
async def test_dispatch_prefers_exact_overrides_edges() -> None:
    from server.code.app.symbol import dispatch as m

    seeds = [
        {
            "symbol_id": "ovr",
            "qualified_name": "DB::StorageReplicatedMergeTree::read",
            "kind": "function",
            "signature": "(scip:a81648bb8024aece)",
        }
    ]
    out = await m._dispatch_seeds(_Repo(), "o", "n", seeds)  # type: ignore[arg-type]
    assert [(r["qualified_name"], r["via"]) for r in out] == [("DB::IStorage::read", "overrides")]


TS_A = "scip-typescript npm pkg 1.0.0 src/`q.ts`/EventsQueryBuilder#"
TS_B = "scip-typescript npm pkg 1.0.0 src/`q.ts`/BaseEventsQueryBuilder#"
TS_C = "scip-typescript npm pkg 1.0.0 src/`q.ts`/AbstractCTEQueryBuilder#"
TS_D = "scip-typescript npm pkg 1.0.0 src/`q.ts`/AbstractQueryBuilder#"


def test_only_the_declared_base_survives_when_the_emitter_lists_every_ancestor() -> None:
    # Scip-typescript relates a class to ALL its ancestors;
    # the edge is the declared clause only, the walk re-derives the rest
    ancestors = {TS_A: {TS_B, TS_C, TS_D}, TS_B: {TS_C, TS_D}, TS_C: {TS_D}}
    assert scip_to_delta._direct_targets({TS_B, TS_C, TS_D}, ancestors) == {TS_B}
    assert scip_to_delta._direct_targets({TS_C, TS_D}, ancestors) == {TS_C}
    # a cycle keeps everything rather than nothing
    assert scip_to_delta._direct_targets({"x", "y"}, {"x": {"y"}, "y": {"x"}}) == {"x", "y"}
    # scip-clang lists direct bases only: nothing is implied, nothing is dropped
    assert scip_to_delta._direct_targets({BASE_T}, {DER_T: {BASE_T}}) == {BASE_T}
    # end to end on the C++ grammar the parser knows: a grandchild whose
    # emitter listed both its parent and its grandparent keeps the parent
    grand = "cxx . . $ DB/IStorage#"
    parent = "cxx . . $ DB/MergeTreeData#"
    child = "cxx . . $ DB/StorageMergeTree#"
    edges = scip_to_delta._relationship_edges(
        [_si(child, parent, grand)],
        {child: 70},
        {child: {parent, grand}, parent: {grand}},
    )
    assert [(e["from_qualified_name"], e["to_qualified_name"]) for e in edges] == [
        ("DB::StorageMergeTree", "DB::MergeTreeData"),
    ]
