"""The taxonomy reads the communities it shows.

Reading every row of the namespace's communities under FINAL costs tens of
thousands of rows for a dozen communities, because FINAL merges every pass's
rows before `computed_at = latest` keeps one, and `entity`'s per-granule
dictionaries cost 170 bytes a row at 8192-row granules.
`fetch_communities` ranks ids by size on two small columns and reads
the members of the ids it keeps, without FINAL, over 256-row granules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.app import entity_communities as ec
from tests.unit.schema_ddl import ddl


class _CH:
    def __init__(self, answers: list[list[dict[str, Any]]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = answers

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.calls.append((sql, dict(params or {})))
        return self._answers.pop(0) if self._answers else []


_T = datetime(2026, 9, 19, 8, 0, 0)


@pytest.mark.asyncio
async def test_ids_are_ranked_first_and_only_their_members_are_read() -> None:
    ch = _CH(
        [
            [
                {"community_id": 7, "size": 40, "computed_at": _T},
                {"community_id": 3, "size": 12, "computed_at": _T},
            ],
            [
                {
                    "community_id": 3,
                    "label": "exporters",
                    "size": 12,
                    "top_entities": ["otel", "prom"],
                },
                {
                    "community_id": 7,
                    "label": "alerts",
                    "size": 40,
                    "top_entities": ["disk", "cpu", "mem"],
                },
            ],
        ]
    )
    out = await ec.fetch_communities(ch, org="o", namespace="n", top_n=2, members_per=3)
    assert len(ch.calls) == 2
    ids_sql, ids_params = ch.calls[0]
    members_sql, members_params = ch.calls[1]
    assert "FINAL" not in ids_sql and "FINAL" not in members_sql
    assert "label" not in ids_sql and "groupArray" not in ids_sql
    assert "ORDER BY size DESC, community_id ASC" in ids_sql and ids_params["lim"] == 2
    assert "community_id IN {ids:Array(UInt32)}" in members_sql
    assert "computed_at = {latest:DateTime}" in members_sql
    assert members_params["ids"] == [7, 3] and members_params["latest"] == _T
    assert members_params["mp"] == 3
    # the answer keeps the size order and each community its own members
    assert [c["community_id"] for c in out] == [7, 3]
    assert out[0]["label"] == "alerts" and out[0]["top_entities"] == ["disk", "cpu", "mem"]
    assert out[1]["size"] == 12 and out[1]["computed_at"] == _T


@pytest.mark.asyncio
async def test_a_namespace_the_pass_never_visited_is_one_statement() -> None:
    ch = _CH([[]])
    assert await ec.fetch_communities(ch, org="o", namespace="n") == []
    assert len(ch.calls) == 1


def test_the_table_reads_256_row_granules() -> None:
    assert ddl("entity_communities").endswith("\nSETTINGS index_granularity = 256;")
