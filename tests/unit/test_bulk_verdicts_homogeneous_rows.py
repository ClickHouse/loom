"""A set_from_messages batch with a duplicate verdict stays homogeneous.

ch.insert takes its column list from row 0, so a verdict step that stamped
`status`/`duplicate_of` on the duplicate rows only would raise KeyError on
the rest. Rows carry the columns a verdict may stamp, the verdict step
defaults them defensively, and ch.insert names a heterogeneous batch instead
of failing deep in the driver."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app.bulk_verdicts import apply_bulk_verdicts


def _row(key: str) -> dict[str, Any]:
    return {"org": "o", "namespace": "n", "memory_key": key, "memory_id": f"id-{key}", "value": "v"}


def test_verdicts_never_change_the_key_set() -> None:
    rows = [_row("a"), _row("b"), _row("c"), _row("d")]
    emb = [dict(r, status="active") for r in rows]
    results = [SimpleNamespace(memory_key=r["memory_key"], version=1) for r in rows]
    verdicts = {
        1: SimpleNamespace(verdict="duplicate", duplicate_of_id="dup-1"),
        2: SimpleNamespace(verdict="conflict", peers=[SimpleNamespace(memory_id="p1")]),
        3: SimpleNamespace(verdict="merge", existing_memory_id="e1"),
    }
    plan = apply_bulk_verdicts(rows, emb, [0, 1, 2, 3], results, verdicts)
    keysets = {frozenset(r.keys()) for r in plan.memory_rows}
    assert len(keysets) == 1, keysets
    by = {r["memory_key"]: r for r in plan.memory_rows}
    assert by["a"]["status"] == "active" and by["a"]["duplicate_of"] == ""
    assert by["b"]["status"] == "duplicate" and by["b"]["duplicate_of"] == "dup-1"
    assert "d" not in by and results[3].version == 0
    assert plan.conflict_marks == [("id-c", ["p1"])]


@pytest.mark.asyncio
async def test_insert_names_a_heterogeneous_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import rmt_carry
    from server.core.ch import CHClient

    monkeypatch.setattr(rmt_carry, "carries", lambda _table: False)  # its own tests
    ch = CHClient.__new__(CHClient)
    with pytest.raises(ValueError) as ei:
        await ch.insert("loom.memory", [{"a": 1, "b": 2}, {"a": 1}])
    assert "row 1" in str(ei.value) and "missing ['b']" in str(ei.value)
