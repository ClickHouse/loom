"""An aggregate is never aliased to a column the same statement filters on.

`argMin(provenance, …) AS provenance` next to `provenance IN
{prov:Array(String)}` makes ClickHouse 26.x resolve the alias into the
aggregate and reject the statement with ILLEGAL_AGGREGATION (code 184), so
every filtered `facts.list` (and `conversation.threads`) call would fail.
This test pins the alias and the row key the handler reads back."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest


class _StubCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        return self._rows if len(self.calls) == 1 else []


@pytest.mark.asyncio
async def test_facts_list_provenance_filter_does_not_shadow_the_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app import facts as mod

    ch = _StubCH(
        [
            {
                "subject": "user",
                "relation": "likes",
                "object": "tea",
                "co_occur": 2,
                "last_seen": datetime(2026, 1, 1, tzinfo=UTC),
                "provenance_tier": "extracted",
            }
        ]
    )
    monkeypatch.setattr(mod, "get_client", lambda: ch)
    out = await mod.handle_facts_list(
        FactsListInput(org="o", namespace="n", provenance=["extracted"])
    )
    sql, params = ch.calls[0]
    assert "AS provenance_tier" in sql and "AS provenance\n" not in sql
    assert "provenance IN {prov:Array(String)}" in sql and params["prov"] == ["extracted"]
    assert out.rows[0].provenance == "extracted"
