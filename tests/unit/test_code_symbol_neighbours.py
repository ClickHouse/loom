"""The neighbours block on code.symbol's own definitions —
distinct qualified names as anchors, both tiers' ids joined, off by default,
fail-open."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from server.code.app.symbol import symbol as h

ROWS = [
    {
        "symbol_id": "a",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "path": "src/Foo.cpp",
        "kind": "function",
        "tier": "treesitter",
    },
    {
        "symbol_id": "a2",
        "qualified_name": "DB::Foo::bar",
        "name": "bar",
        "path": "src/Foo.cpp",
        "kind": "function",
        "tier": "scip",
    },
    {
        "symbol_id": "c",
        "qualified_name": "DB::baz",
        "name": "baz",
        "path": "src/Other.cpp",
        "kind": "function",
        "tier": "scip",
    },
]


def _run(*, neighbours=None, k=5, fail=False):
    seen: list[Any] = []

    async def fake_attach(repo, org, ns, anchors, function_rows, kk):
        seen.append((anchors, function_rows, kk))
        if fail:
            raise RuntimeError("edges down")
        return (
            [{"qualified_name": a["qualified_name"], "callers": ["X"]} for a in anchors],
            False,
            77,
        )

    inp = SimpleNamespace(neighbours=neighbours, neighbour_k=k, name="bar")
    with patch.object(h, "_attach_enclosing_neighbours", new=fake_attach):
        out = asyncio.run(h._symbol_neighbours(object(), "o", "n", ROWS, inp))
    return out, seen


def test_definitions_are_the_anchors_distinct_by_name_rows_joined() -> None:
    (entries, tokens), seen = _run(neighbours=True, k=3)
    anchors, rows, k = seen[0]
    assert [a["qualified_name"] for a in anchors] == ["DB::Foo::bar", "DB::baz"]
    assert anchors[0]["symbol_id"] == "a" and rows is ROWS and k == 3
    assert [e["qualified_name"] for e in entries] == ["DB::Foo::bar", "DB::baz"] and tokens == 77


def test_off_unless_the_request_sets_them() -> None:
    assert _run() == (([], 0), [])
    assert _run(neighbours=False) == (([], 0), [])


def test_fail_open() -> None:
    assert _run(neighbours=True, fail=True)[0] == ([], 0)
