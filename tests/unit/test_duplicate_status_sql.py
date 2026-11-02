"""The served-duplicate status read, run on clickhouse-local against the real
`loom.memory`: by row, for the rows a search pooled."""

from __future__ import annotations

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.search_types import RowKey
from server.memory.store import dedup
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_ROWS = (
    "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
    "memory_id, version, status, duplicate_of, value) VALUES "
    "('o', 'n', 'a', '-', 's1', 'k1', generateUUIDv4(), 1, 'duplicate', 'c-old', 'v'), "
    "('o', 'n', 'a', '-', 's2', 'k1', generateUUIDv4(), 2, 'duplicate', 'c-new', 'v'), "
    "('o', 'n', 'a', '-', 's1', 'k2', generateUUIDv4(), 1, 'active', '', 'v'), "
    "('o', 'n', 'a', '-', 's2', 'k2', generateUUIDv4(), 1, 'duplicate', 'c-2', 'v'), "
    "('o', 'n', 'b', '-', 's1', 'k1', generateUUIDv4(), 1, 'active', '', 'v')"
)


@pytest.mark.asyncio
async def test_a_row_read_answers_for_that_row_alone() -> None:
    rows = [RowKey("n", "a", "-", "s2", "k2"), RowKey("n", "", "", "", "k1")]
    found = await dedup.status_by_rows(
        "o", rows, scope=WHOLE_NAMESPACE, ch=LocalCH(ddl("memory"), _ROWS)
    )
    # a key named without its row's identity matches no stored row
    assert [(s.row_key, s.status) for s in found] == [(rows[0], "duplicate")]
