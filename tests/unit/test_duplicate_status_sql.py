"""The served-duplicate status reads, run on clickhouse-local against the real
`loom.memory`: by row for a hit that names one, by key for a hit that names none."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.search_types import RowKey
from server.memory.store import dedup
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_ROWS = (
    "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
    "memory_id, version, status, duplicate_of, value) VALUES "
    "('o', 'n', 'a', '-', 's1', 'k1', generateUUIDv4(), 1, 'duplicate', 'c-old', 'v'), "
    "('o', 'n', 'a', '-', 's2', 'k1', generateUUIDv4(), 2, 'duplicate', 'c-new', 'v'), "
    "('o', 'n', 'a', '-', 's1', 'k2', generateUUIDv4(), 1, 'active', '', 'v'), "
    "('o', 'n', 'a', '-', 's2', 'k2', generateUUIDv4(), 1, 'duplicate', 'c-2', 'v')"
)


class _LocalCH:
    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run([ddl("memory"), _ROWS], sql, params)


@pytest.mark.asyncio
async def test_a_key_is_a_duplicate_only_when_none_of_its_rows_is_served() -> None:
    found = await dedup.status_by_keys(
        "o", [("n", "k1"), ("n", "k2"), ("n", "gone")], scope=WHOLE_NAMESPACE, ch=_LocalCH()
    )
    by_key = {s.memory_key: (s.status, s.canonical_id) for s in found}
    # the newest duplicate names the canonical; an active row of the key serves it
    assert by_key == {"k1": ("duplicate", "c-new"), "k2": ("active", "c-2")}


@pytest.mark.asyncio
async def test_a_row_read_answers_for_that_row_alone() -> None:
    rows = [RowKey("n", "a", "-", "s2", "k2"), RowKey("n", "", "", "", "k1")]
    found = await dedup.status_by_rows("o", rows, scope=WHOLE_NAMESPACE, ch=_LocalCH())
    # a key named without its row's identity matches no stored row
    assert [(s.row_key, s.status) for s in found] == [(rows[0], "duplicate")]
