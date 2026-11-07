"""A point read of loom.memory touches one small granule, filtered on its
key before the merge.

Two independent levers: the table is created with a small granule, and the
two hot point reads — `get_by_key` and the second phase of `get_by_id` — put
their sort-key columns in PREWHERE while every non-key filter (tombstone, expiry,
as-of window, created_at, memory_id) stays in WHERE, after the FINAL merge.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.store import Identity
from tests.unit.schema_ddl import ddl


def test_the_point_read_tables_carry_a_small_granule() -> None:
    assert "SETTINGS index_granularity = 128," in ddl("memory")
    assert "SETTINGS index_granularity = 1024," in ddl("memory_belief_sources")


class _CH:
    def __init__(self, rows: list[dict[str, Any] | None] | None = None) -> None:
        self.sqls: list[str] = []
        self.rows = list(rows or [])

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sqls.append(sql)
        return self.rows.pop(0) if self.rows else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.sqls.append(sql)
        return []


def _split(sql: str) -> tuple[str, str]:
    """(prewhere text, where text) of a FINAL point read."""
    assert "FROM loom.memory FINAL" in sql
    after = sql.split("FROM loom.memory FINAL", 1)[1]
    assert "PREWHERE" in after, sql
    pre = after.split("PREWHERE", 1)[1]
    where = ""
    if "WHERE" in pre.replace("PREWHERE", ""):
        pre, where = pre.split("\n        WHERE", 1) if "\n        WHERE" in pre else (pre, "")
    return pre, where


@pytest.mark.asyncio
async def test_get_by_key_puts_the_sort_key_in_prewhere_and_the_rest_in_where() -> None:
    ch = _CH()
    ident = Identity(org="o", namespace="n", agent="a", user_id="u", session_id="s")
    await memory_repo.get_by_key(ident, "k", ch=ch, include_expired=False, scope=WHOLE_NAMESPACE)
    sql = ch.sqls[0]
    pre, where = _split(sql)
    for col in ("org", "namespace", "memory_key", "agent", "user_id", "session_id"):
        assert f"{col} = {{" in pre, (col, pre)
    for non_key in ("tombstoned_at", "expires_at"):
        assert non_key not in pre, pre
        assert non_key in where, where
    assert sql.index("PREWHERE") < sql.index("tombstoned_at IS NULL")


@pytest.mark.asyncio
async def test_get_by_key_wildcard_agent_and_as_of_keep_non_key_filters_in_where() -> None:
    ch = _CH()
    ident = Identity(org="o", namespace="n", agent="*", user_id="-", session_id="-")
    await memory_repo.get_by_key(
        ident,
        "k",
        ch=ch,
        as_of=datetime(2026, 9, 13, tzinfo=UTC),
        include_tombstoned=True,
        scope=WHOLE_NAMESPACE,
    )
    sql = ch.sqls[0]
    pre, where = _split(sql)
    assert "agent = {" not in pre and "session_id = {" not in pre  # wildcard: namespace-wide
    assert "valid_at" in where or "valid_time" in where or "as_of" in where, where
    assert "tombstoned_at" not in sql.split("LIMIT", 1)[0].split("PREWHERE", 1)[1].split("WHERE")[0]
    assert "ORDER BY version DESC" in sql


@pytest.mark.asyncio
async def test_get_by_key_without_filters_has_no_empty_where() -> None:
    ch = _CH()
    ident = Identity(org="o", namespace="n", agent="a", user_id="u", session_id="s")
    await memory_repo.get_by_key(ident, "k", ch=ch, include_tombstoned=True, scope=WHOLE_NAMESPACE)
    sql = ch.sqls[0]
    body = sql.split("FROM loom.memory FINAL", 1)[1]
    assert "PREWHERE" in body
    assert not re.search(r"\bWHERE\s+(?:LIMIT|ORDER)", body), body
    assert not re.search(r"(?<!PRE)WHERE\s*\n\s*LIMIT", body), body


@pytest.mark.asyncio
async def test_get_by_id_second_phase_prewheres_only_the_key_columns_it_found() -> None:
    key = {"created_at": datetime(2026, 9, 13), "memory_key": "k"}
    ch = _CH(rows=[key, None])  # phase 1 finds the key; phase 2 reads nothing
    assert (
        await memory_repo.get_by_id(
            "00000000-0000-0000-0000-000000000001",
            org="o",
            namespace="n",
            ch=ch,
            scope=WHOLE_NAMESPACE,
        )
        is None
    )
    assert len(ch.sqls) == 2
    phase1, phase2 = ch.sqls
    assert "FINAL" not in phase1 and "memory_id = {mid:UUID}" in phase1
    pre, where = _split(phase2)
    assert "org = {" in pre and "namespace = {" in pre and "memory_key = {" in pre
    for non_key in ("created_at", "memory_id", "tombstoned_at"):
        assert non_key not in pre, pre
        assert non_key in where, where
