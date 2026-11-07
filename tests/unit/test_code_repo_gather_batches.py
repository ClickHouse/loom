"""The keyed point reads a request makes in
batches (`symbol_stats`, `unit_risk_rows`, `chunks_by_keys`) read their
batches together, results in batch order, at most `limit` in flight."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store import index as cr
from server.code.store.index import sql as code_sql


def test_batches_read_together_in_order_under_the_limit() -> None:
    in_flight = 0
    peak = 0

    async def read(batch: list[Any]) -> list[Any]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return [b * 10 for b in batch]

    out = asyncio.run(code_sql._gather_batched(list(range(10)), 2, read, limit=3))
    assert out == [[0, 10], [20, 30], [40, 50], [60, 70], [80, 90]]
    assert peak == 3  # three batches in flight at once: serial reads peak at one


def test_a_single_batch_reads_inline_and_nothing_reads_nothing() -> None:
    calls: list[list[Any]] = []

    async def read(batch: list[Any]) -> list[Any]:
        calls.append(batch)
        return batch

    assert asyncio.run(code_sql._gather_batched(["a", "b"], 200, read)) == [["a", "b"]]
    assert asyncio.run(code_sql._gather_batched([], 200, read)) == []
    assert calls == [["a", "b"]]


def test_symbol_stats_merges_every_batch() -> None:
    class _CH:
        def __init__(self) -> None:
            self.ids: list[list[str]] = []

        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.ids.append(list(params["ids"]))
            await asyncio.sleep(0.01)
            return [{"symbol_id": s, "fan_in_nontest": len(s)} for s in params["ids"]]

    repo = cr.CodeRepo.__new__(cr.CodeRepo)
    repo._ch = _CH()  # type: ignore[attr-defined]
    ids = [f"s{i}" for i in range(450)] + ["s1", ""]
    out = asyncio.run(repo.symbol_stats("o", "n", ids))
    assert len(out) == 450 and out["s449"]["fan_in_nontest"] == 4
    assert [len(b) for b in repo._ch.ids] == [200, 200, 50]  # type: ignore[attr-defined]
