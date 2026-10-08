"""`memory.digests`: a namespace's session digests are readable
on the wire so their fidelity to the turns they compress can be audited."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_temporal import MemoryDigestsInput, MemoryDigestsOutput
from server.memory.app import curation as cur
from server.operations.registry import ALL_OPERATIONS


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.params, self.sql = rows, {}, ""

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql, self.params = sql, dict(params or {})
        return self.rows[: int(self.params["lim"])]


def test_op_is_registered_for_viewers() -> None:
    op = next(o for o in ALL_OPERATIONS if o.name == "memory.digests")
    assert op.required_role.name == "VIEWER"
    assert op.input_model is MemoryDigestsInput and op.output_model is MemoryDigestsOutput
    assert MemoryDigestsInput(org="o", namespace="n", agent="a").session_id == "*"


@pytest.mark.asyncio
async def test_wildcard_lists_all_sessions_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "session_id": f"ns:session_{i}",
            "summary_text": f"digest {i}",
            "key_facts": ["a"],
            "derived_from": ["k1"],
            "valid_at": datetime(2023, 5, i + 1),
            "created_at": datetime(2026, 9, 12),
            "version": 1,
        }
        for i in range(3)
    ]
    ch = _FakeCH(rows)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_digests(
        MemoryDigestsInput(org="o", namespace="n", agent="a", limit=2)
    )
    assert "session_id = {s:String}" not in ch.sql and ch.params["lim"] == 3
    assert [r.session_id for r in out.rows] == ["ns:session_0", "ns:session_1"] and out.truncated
    assert out.rows[0].key_facts == ["a"] and out.rows[0].derived_from == ["k1"]


@pytest.mark.asyncio
async def test_concrete_session_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _FakeCH([])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_digests(
        MemoryDigestsInput(org="o", namespace="n", agent="a", session_id="ns:session_2")
    )
    assert "session_id = {s:String}" in ch.sql and ch.params["s"] == "ns:session_2"
    assert out.rows == [] and out.truncated is False
