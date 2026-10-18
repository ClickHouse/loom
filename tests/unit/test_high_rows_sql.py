"""The statements that keep 'high' rows home, run on clickhouse-local against the real
tables: high_keys finds a key with any high version, overwritten since or under an id since
re-created (loom.memory_versions keeps every version; loom.memory merges them); a `contains`
search skips a high row's value; and the usage overlay reads the epoch non-recall events
write as no recall."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.store.reads import CONTAINS_PREDICATE
from server.memory.store.rows import MemoryRow
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_A, _B, _C, _D = (f"00000000-0000-0000-0000-00000000000{i}" for i in "abcd")


class _LocalCH:
    """Runs each query on clickhouse-local after the setup statements."""

    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)


def _version(mid: str, key: str, version: int, sensitivity: str) -> str:
    return (
        "INSERT INTO loom.memory_versions (org, namespace, agent, memory_id, memory_key, "
        f"version, sensitivity) VALUES ('o', 'n', 'a', '{mid}', '{key}', {version}, "
        f"'{sensitivity}')"
    )


@pytest.mark.asyncio
async def test_high_keys_finds_any_high_version_of_a_key() -> None:
    ch = _LocalCH(
        ddl("memory_versions"),
        # overwritten: one id, high then low
        _version(_A, "overwritten", 1, "high"),
        _version(_A, "overwritten", 2, "low"),
        # deleted and written again: a new id the second time
        _version(_B, "recreated", 1, "high"),
        _version(_C, "recreated", 2, ""),
        _version(_D, "plain", 1, ""),
        # a merge keeps every version: `version` is in the sort key
        "OPTIMIZE TABLE loom.memory_versions FINAL",
    )
    got = await memory_repo.high_keys(
        "o", "n", ["overwritten", "recreated", "plain", "absent"], ch=ch
    )
    assert got == {"overwritten", "recreated"}


@pytest.mark.asyncio
async def test_a_contains_search_matches_a_high_row_by_key_only() -> None:
    ch = _LocalCH(
        ddl("memory"),
        "INSERT INTO loom.memory (org, namespace, agent, memory_id, memory_key, value, "
        f"sensitivity) VALUES ('o', 'n', 'a', '{_A}', 'door', 'code 4412', 'high'), "
        f"('o', 'n', 'a', '{_B}', 'note', 'code 4412 is old', '')",
    )
    sql = f"SELECT memory_key FROM loom.memory WHERE {CONTAINS_PREDICATE} ORDER BY memory_key"
    by_value = await ch.query_all(sql, {"contains": "4412"})
    by_key = await ch.query_all(sql, {"contains": "door"})
    assert [r["memory_key"] for r in by_value] == ["note"]
    assert [r["memory_key"] for r in by_key] == ["door"]


@pytest.mark.asyncio
async def test_the_usage_overlay_reads_the_epoch_as_no_recall() -> None:
    ch = _LocalCH(
        ddl("memory_usage_totals"),
        "INSERT INTO loom.memory_usage_totals (memory_id, org, namespace, evidence_count, "
        f"recall_count, last_recalled_at) VALUES ('{_A}', 'o', 'n', 2, 0, 0)",
    )
    row = MemoryRow(
        memory_id=_A,
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 19),
        expires_at=None,
    )
    [got] = await memory_repo.overlay_usage_totals_many([row], ch=ch)
    assert got.evidence_count == 2 and got.last_recalled_at is None
