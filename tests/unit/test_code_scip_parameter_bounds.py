"""Compiler reconciliation reads keep long names and paths within HTTP form limits."""

from __future__ import annotations

import re
from typing import Any

import pytest
from clickhouse_connect.driver.binding import format_bind_value

from server.code.store.index import CodeRepo
from server.code.store.index.sql import _batched
from server.core.ch_params import FIELD_BYTES

_HTTP_FIELD_BYTES = 128 * 1024


class _BoundedCH:
    def __init__(self, rows: list[dict[str, Any]], field_bytes: int = _HTTP_FIELD_BYTES) -> None:
        self.rows = rows
        self.field_bytes = field_bytes
        self.calls: list[dict[str, Any]] = []
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert params["org"] == "o" and params["ns"] == "n"
        for name, value in params.items():
            size = len(format_bind_value(value).encode())
            assert size <= self.field_bytes, f"{name}: {size} bytes exceeds {self.field_bytes}"
        self.calls.append(params)
        self.sql.append(sql)
        paths = set(params.get("paths", [params.get("path")]))
        rows = [dict(row) for row in self.rows if row["path"] in paths]
        if "SELECT DISTINCT qualified_name, symbol_id" in sql:
            return [{k: row[k] for k in ("qualified_name", "symbol_id")} for row in rows]
        if "keys" in params:
            keys = set(params["keys"])
            return [row for row in rows if (row["qualified_name"], row["symbol_id"]) in keys]
        if "(qualified_name, symbol_id) = (" in sql:
            refs = re.findall(r"unhex\(\{(\w+):String\}\)", sql)
            parts = [bytes.fromhex(params[name]) for name in refs]
            # Canonical symbol ids fit one field. The preceding name parts keep
            # expression order, including inside nested concatenations.
            values = [b"".join(parts[:-1]).decode(), parts[-1].decode()]
            return [
                row for row in rows if (row["qualified_name"], row["symbol_id"]) == tuple(values)
            ]
        assert not rows  # These tests read names or intentionally empty path sets.
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("round_read", [False, True])
@pytest.mark.parametrize(
    "prefix", ["module." * 40, "型\\'" * 45], ids=["long-name", "quoted-multibyte"]
)
async def test_symbol_reconciliation_reads_every_long_name_with_default_http_limits(
    round_read: bool, prefix: str
) -> None:
    rows = [
        {"path": "main.py", "qualified_name": f"{prefix}function_{i}", "symbol_id": f"{i:032x}"}
        for i in range(1300)
    ]
    ch = _BoundedCH(rows)
    repo = CodeRepo(ch)  # type: ignore[arg-type]

    if round_read:
        actual = (await repo.live_symbols_for_paths("o", "n", ["main.py"], "scip"))["main.py"]
    else:
        actual = await repo.live_symbols_for_path("o", "n", "main.py", "scip")

    assert actual == rows
    assert all(call["tier"] == "scip" for call in ch.calls)
    assert len(ch.calls) > 2


@pytest.mark.asyncio
@pytest.mark.parametrize("round_read", [False, True])
@pytest.mark.parametrize(
    "name",
    ["型" * 50_000, "型\\'" * 40_000, "型" * 720_000],
    ids=["multibyte", "quoted-multibyte", "many-parts"],
)
async def test_an_oversized_single_symbol_key_is_read_in_full(round_read: bool, name: str) -> None:
    rows = [
        {"path": "main.py", "qualified_name": name, "symbol_id": "1" * 32},
        {"path": "main.py", "qualified_name": "small", "symbol_id": "2" * 32},
    ]
    ch = _BoundedCH(rows, field_bytes=FIELD_BYTES)
    repo = CodeRepo(ch)  # type: ignore[arg-type]

    if round_read:
        actual = (await repo.live_symbols_for_paths("o", "n", ["main.py"], "scip"))["main.py"]
    else:
        actual = await repo.live_symbols_for_path("o", "n", "main.py", "scip")

    assert actual == rows
    assert any("concat(" in sql and "unhex(" in sql for sql in ch.sql)
    assert all(name not in sql for sql in ch.sql)
    assert all(call["tier"] == "scip" for call in ch.calls)
    assert all(_max_arguments(sql) <= 64 for sql in ch.sql)
    assert all(len(sql.encode()) < 256 * 1024 for sql in ch.sql)


def _max_arguments(sql: str) -> int:
    counts: list[int] = []
    maximum = 0
    for char in sql:
        if char == "(":
            counts.append(1)
        elif char == "," and counts:
            counts[-1] += 1
        elif char == ")":
            maximum = max(maximum, counts.pop())
    assert not counts
    return maximum


@pytest.mark.asyncio
@pytest.mark.parametrize("round_read", [False, True])
async def test_no_symbol_keys_send_no_reconciliation_query(round_read: bool) -> None:
    ch = _BoundedCH([])
    repo = CodeRepo(ch)  # type: ignore[arg-type]

    if round_read:
        assert await repo.live_symbols_for_paths("o", "n", ["main.py"], "scip") == {"main.py": []}
    else:
        assert await repo.live_symbols_for_path("o", "n", "main.py", "scip") == []

    assert len(ch.calls) == 1
    assert "SELECT DISTINCT qualified_name, symbol_id" in ch.sql[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["symbols", "edges", "spans"])
async def test_compiler_round_reads_bound_long_utf8_paths(method: str) -> None:
    paths = sorted(("文/" * 750) + f"file_{i}.py" for i in range(100))
    ch = _BoundedCH([])
    repo = CodeRepo(ch)  # type: ignore[arg-type]

    if method == "symbols":
        actual = await repo.live_symbols_for_paths("o", "n", paths, "scip")
    elif method == "edges":
        actual = await repo.live_edges_for_paths("o", "n", paths, "scip")
    else:
        actual = await repo.chunk_spans_for_paths("o", "n", paths)

    assert not any(actual.values())
    assert [path for call in ch.calls for path in call.get("paths", [])] == sorted(paths)


def test_parameter_batches_preserve_count_limits_and_input_order() -> None:
    small = list(range(205))
    assert _batched(small, 100) == [small[:100], small[100:200], small[200:]]
    assert _batched([], 100) == []
    keys = [("名\\'" * 80, f"{i:032x}") for i in range(400)]
    parts = _batched(keys, 100)
    assert [key for part in parts for key in part] == keys
    assert all(len(part) <= 100 for part in parts)
    assert all(len(format_bind_value(part).encode()) <= _HTTP_FIELD_BYTES for part in parts)
