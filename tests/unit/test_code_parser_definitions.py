"""`parser_definitions` reads a long name list a slice at a time, each slice one bound
parameter under the server's form-field cap, names and qualified names in reads of
their own, and returns each definition once."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.store import index as m
from server.core.ch_params import FIELD_BYTES


class _CH:
    def __init__(self) -> None:
        self.params: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.params.append(dict(params or {}))
        return [{"qualified_name": "m::fetch_rows", "symbol_id": "s1", "path": "m.py"}]


@pytest.mark.asyncio
async def test_a_long_name_list_is_read_in_slices_and_each_definition_comes_once() -> None:
    ch = _CH()
    repo = m.CodeRepo.__new__(m.CodeRepo)
    repo._ch = ch  # type: ignore[attr-defined]
    names = [f"read_rows_from_part_{i}" for i in range(9000)]
    rows = await repo.parser_definitions("o", "ns", names=names, qualified_names=["m::f"])
    assert rows == [{"qualified_name": "m::fetch_rows", "symbol_id": "s1", "path": "m.py"}]
    by_name = [p for p in ch.params if p["names"]]
    assert len(by_name) > 1 and all(p["qns"] == [] for p in by_name)
    assert sorted(n for p in by_name for n in p["names"]) == sorted(names)
    assert all(len(repr(p["names"])) < 2 * FIELD_BYTES for p in by_name)
    assert [p["qns"] for p in ch.params if not p["names"]] == [["m::f"]]
