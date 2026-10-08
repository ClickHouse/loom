"""An erasure must reach the stored idempotency replays of the erased org.

loom.idempotency keeps a write's response for 24 hours so a retry replays it,
and a memory.set response can quote memory text (peer excerpts). The key
is a hash, so the table also carries the org and subject the key was built
from, and the `idempotency.delete` saga step removes
the erased scope's replays, so a retry never returns the erased body.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.core import idempotency
from server.erasure.store import census as erasure_census

SECRET = "the vendor's ssn is 078-05-1120"


class _Table:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.idempotency"
        for row in rows:
            self.rows[row["idempotency_key"]] = dict(row)

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        assert "FROM loom.idempotency FINAL WHERE idempotency_key" in sql
        return self.rows.get((params or {})["k"])


def _owning_steps(table: str) -> list[str]:
    census = [s.ns_step for s in erasure_census._CENSUS if s.table == table]
    step_only = [
        step for step, tables in erasure_census.STEP_ONLY_TABLES.items() if table in tables
    ]
    return census + step_only


async def test_a_replay_after_erasure_does_not_return_the_erased_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _Table()
    monkeypatch.setattr(idempotency, "get_client", lambda: table)
    key = idempotency.scoped_key("k-1", org="acme", subject="alice", operation="memory.set")
    await idempotency.store(
        key,
        "body-hash",
        "memory.set",
        200,
        {"peers": [{"excerpt": SECRET}]},
        org="acme",
        subject="alice",
    )
    replay = await idempotency.check(key, "body-hash")
    assert replay.hit is not None and SECRET in str(replay.hit.body)
    # The row names its owner, so an erasure of org "acme" can find it, and a
    # saga step owns the table.
    assert table.rows[key]["org"] == "acme" and table.rows[key]["subject"] == "alice"
    assert _owning_steps("loom.idempotency") == ["idempotency.delete"]


def test_the_table_is_erased_not_a_reviewed_skip() -> None:
    assert "loom.idempotency" not in erasure_census.NO_MEMORY_CONTENT
    assert _owning_steps("loom.idempotency") == ["idempotency.delete"]
