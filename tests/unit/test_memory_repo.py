"""Unit tests for server/memory/store/.

The point of these tests: prove the repository pattern is testable
WITHOUT a live ClickHouse. We mock the CH client protocol; the repo's
job is to emit the right SQL with the right params and hydrate the
response into typed MemoryRow objects.

These tests run in seconds, fail loudly on a SQL refactor that drops
a clause, and don't require LOOM_BASE_URL or a CH container — exactly
the testability gap the repository extraction is meant to close.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.store import (
    CONTAINS_PREDICATE,
    Identity,
    MemoryRow,
    count_by_namespace,
    get_by_id,
    get_by_key,
    identity_key,
    list_by_namespace,
)
from tests.unit.memory_rows import projection_row

# ----------------------------------------------------------------------
# Test double: a stub CH client recording each call
# ----------------------------------------------------------------------


class StubCH:
    """In-memory replacement for server.core.ch.CHClient — records every call,
    lets the test scaffold supply the response for each query."""

    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql: str, params: dict[str, Any] | None = None):
        self.execute_calls.append((sql, params or {}))

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ):
        self.insert_calls.append((table, rows, settings or {}))


def _sample_row() -> dict[str, Any]:
    """Representative loom.memory row in the shape the SQL returns."""
    return {
        "memory_id": "11111111-2222-3333-4444-555555555555",
        "org": "clickhouse",
        "namespace": "personal-alice",
        "agent": "claude-desktop",
        "user_id": "alice@example.com",
        "session_id": "-",
        "memory_key": "deploy_window",
        "memory_type": "preference",
        "value": json.dumps("Deploy Tue/Thu, never Friday"),
        "confidence": 1.0,
        "version": 1778930253022426752,
        "pinned": 1,
        "created_at": datetime(2026, 5, 16, 12, 0, 0),
        "expires_at": None,
        "status": "active",
        "run_id": "-",
        "source_trace": "",
    }


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_key_hits_correct_filters_and_hydrates() -> None:
    """get_by_key must SELECT FINAL, filter by every identity field +
    memory_key + tombstoned_at IS NULL, and return a MemoryRow."""
    ch = StubCH()
    ch.query_one_returns.append(_sample_row())

    identity = Identity(
        org="clickhouse",
        namespace="personal-alice",
        agent="claude-desktop",
        user_id="alice@example.com",
    )
    row = await get_by_key(identity, "deploy_window", ch=ch, scope=WHOLE_NAMESPACE)

    # Right SQL shape
    sql, params = ch.query_one_calls[0]
    assert "FROM loom.memory FINAL" in sql, "missing FINAL — stale-read bug"
    assert "tombstoned_at IS NULL" in sql
    assert "LIMIT 1" in sql
    # Every identity field threads through
    assert params["org"] == "clickhouse"
    assert params["ns"] == "personal-alice"
    assert params["agent"] == "claude-desktop"
    assert params["uid"] == "alice@example.com"
    assert params["sid"] == "-"
    assert params["key"] == "deploy_window"

    # Hydration: typed MemoryRow with value JSON-decoded
    assert isinstance(row, MemoryRow)
    assert row.memory_key == "deploy_window"
    assert row.value == "Deploy Tue/Thu, never Friday"
    assert row.pinned is True
    assert row.version == 1778930253022426752


@pytest.mark.asyncio
async def test_get_by_key_returns_none_when_missing() -> None:
    """get_by_key returns None (not raises) when the row doesn't exist.
    Handlers raise NotFoundError; repo just reports absence."""
    ch = StubCH()
    ch.query_one_returns.append(None)
    identity = Identity(org="clickhouse", namespace="ns", agent="bot")
    assert await get_by_key(identity, "missing", ch=ch, scope=WHOLE_NAMESPACE) is None


@pytest.mark.asyncio
async def test_get_by_key_can_overlay_usage_totals() -> None:
    """User-facing reads can derive counters from memory_usage_totals while
    default repo reads preserve persisted loom.memory columns for RMT writers."""

    ch = StubCH()
    ch.query_one_returns.append(_sample_row())
    ch.query_one_returns.append(
        {
            "evidence_count": 4,
            "recall_count": 7,
            "last_recalled_at": datetime(2026, 5, 20, 9, 0, 0),
        }
    )
    identity = Identity(
        org="clickhouse",
        namespace="personal-alice",
        agent="claude-desktop",
        user_id="alice@example.com",
    )

    row = await get_by_key(
        identity,
        "deploy_window",
        ch=ch,
        include_usage_totals=True,
        scope=WHOLE_NAMESPACE,
    )

    assert row is not None
    assert row.evidence_count == 4
    assert row.recall_count == 7
    assert row.last_recalled_at == datetime(2026, 5, 20, 9, 0, 0)
    usage_sql, usage_params = ch.query_one_calls[1]
    assert "FROM loom.memory_usage_totals" in usage_sql
    assert usage_params["mid"] == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_list_by_namespace_applies_filters_and_limit() -> None:
    """list_by_namespace honors memory_type filter, pinned_only filter,
    and the bounded LIMIT."""
    ch = StubCH()
    ch.query_all_returns.append([_sample_row(), _sample_row()])

    identity = Identity(org="clickhouse", namespace="ns", agent="bot")
    rows = await list_by_namespace(
        identity,
        memory_type="preference",
        pinned_only=True,
        limit=50,
        ch=ch,
        scope=WHOLE_NAMESPACE,
    )

    sql, params = ch.query_all_calls[0]
    assert "memory_type = {mt:String}" in sql
    assert "pinned = 1" in sql
    assert params["limit"] == 50
    assert params["mt"] == "preference"
    assert len(rows) == 2
    assert all(isinstance(r, MemoryRow) for r in rows)


@pytest.mark.asyncio
async def test_list_by_namespace_without_optional_filters() -> None:
    """When no type filter / pinned_only, the SQL omits those clauses."""
    ch = StubCH()
    ch.query_all_returns.append([])

    identity = Identity(org="clickhouse", namespace="ns", agent="bot")
    await list_by_namespace(identity, ch=ch, scope=WHOLE_NAMESPACE)

    sql, params = ch.query_all_calls[0]
    assert "memory_type = " not in sql
    assert "pinned = 1" not in sql
    assert "startsWith(memory_key" not in sql
    assert "mt" not in params
    assert "prefix" not in params


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True])
async def test_list_and_count_filter_by_contained_text(preview: bool) -> None:
    """`contains` keeps the rows whose value or key holds the text, any case: the list page and
    its total read the same predicate, so "N of TOTAL" counts what the page filters."""
    ch = StubCH()
    ch.query_all_returns.append([])
    ch.query_one_returns.append({"n": 146})
    identity = Identity(org="clickhouse", namespace="ns", agent="*")

    await list_by_namespace(
        identity, contains="Azure", preview=preview, ch=ch, scope=WHOLE_NAMESPACE
    )
    total = await count_by_namespace(identity, contains="Azure", ch=ch, scope=WHOLE_NAMESPACE)

    list_sql, list_params = ch.query_all_calls[0]
    count_sql, count_params = ch.query_one_calls[0]
    for sql, params in ((list_sql, list_params), (count_sql, count_params)):
        assert CONTAINS_PREDICATE in sql
        assert params["contains"] == "Azure"
    assert "positionCaseInsensitiveUTF8(value, {contains:String})" in CONTAINS_PREDICATE
    assert "positionCaseInsensitiveUTF8(memory_key, {contains:String})" in CONTAINS_PREDICATE
    assert total == 146


@pytest.mark.asyncio
@pytest.mark.parametrize("contains", [None, ""])
async def test_list_without_contains_reads_no_values(contains: str | None) -> None:
    """No text (None or empty) is no filter: the page never scans the value column."""
    ch = StubCH()
    ch.query_all_returns.append([])
    identity = Identity(org="clickhouse", namespace="ns", agent="*")

    await list_by_namespace(identity, contains=contains, preview=True, ch=ch, scope=WHOLE_NAMESPACE)

    sql, params = ch.query_all_calls[0]
    assert "positionCaseInsensitiveUTF8" not in sql
    assert "contains" not in params


@pytest.mark.asyncio
async def test_list_by_namespace_key_prefix_and_order_options() -> None:
    """memory.list uses key_prefix + memory_key_asc; recent-feed uses
    the default created_at_desc. Both shapes are supported by the same
    repository function — proves the abstraction is general enough."""
    ch = StubCH()
    ch.query_all_returns.append([])
    identity = Identity(org="clickhouse", namespace="ns", agent="bot")

    await list_by_namespace(
        identity,
        key_prefix="deploy_",
        order_by="memory_key_asc",
        ch=ch,
        scope=WHOLE_NAMESPACE,
    )
    sql, params = ch.query_all_calls[0]
    assert "startsWith(memory_key, {prefix:String})" in sql
    assert "ORDER BY memory_key" in sql
    assert "ORDER BY created_at" not in sql
    assert params["prefix"] == "deploy_"


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize(
    ("order_by", "lead"),
    [
        ("created_at_desc", "created_at DESC, "),
        ("created_at_asc", "created_at ASC, "),
        ("memory_key_asc", ""),
    ],
)
async def test_list_by_namespace_order_is_total(order_by: str, lead: str, preview: bool) -> None:
    """OFFSET pages skip or repeat rows unless the ORDER BY is total:
    created_at is whole-second (a bulk write shares one second) and
    memory_key repeats across identities under a `*` axis. Every order ends
    in the table's unique key past (org, namespace), which WHERE fixes."""
    ch = StubCH()
    ch.query_all_returns.append([])
    identity = Identity(org="o", namespace="ns", agent="*", user_id="*", session_id="*")

    await list_by_namespace(
        identity, order_by=order_by, preview=preview, ch=ch, scope=WHOLE_NAMESPACE
    )
    sql, _ = ch.query_all_calls[0]
    assert f"ORDER BY {lead}memory_key, agent, user_id, session_id\n" in sql


@pytest.mark.asyncio
async def test_list_by_namespace_rejects_unknown_order_by() -> None:
    """Defensive: an unknown `order_by` is a programming error, not a
    silent default. Returning the wrong order silently is a bug source."""
    ch = StubCH()
    identity = Identity(org="clickhouse", namespace="ns", agent="bot")
    with pytest.raises(ValueError, match="unknown order_by"):
        await list_by_namespace(identity, order_by="bogus", ch=ch, scope=WHOLE_NAMESPACE)


@pytest.mark.asyncio
async def test_get_many_by_keys_empty_input_skips_ch() -> None:
    """Bulk get with empty keys list — no CH round-trip, no rows returned."""
    from server.memory.store import get_many_by_keys

    ch = StubCH()
    identity = Identity(org="clickhouse", namespace="ns", agent="bot")
    out = await get_many_by_keys(identity, [], ch=ch, scope=WHOLE_NAMESPACE)
    assert out == []
    assert ch.query_all_calls == []


@pytest.mark.asyncio
async def test_get_many_by_keys_emits_array_filter() -> None:
    """Bulk get uses `memory_key IN {keys:Array(String)}` — one round-trip
    instead of N point lookups."""
    from server.memory.store import get_many_by_keys

    ch = StubCH()
    ch.query_all_returns.append([_sample_row(), _sample_row()])
    identity = Identity(org="clickhouse", namespace="ns", agent="bot")
    out = await get_many_by_keys(identity, ["k1", "k2", "k3"], ch=ch, scope=WHOLE_NAMESPACE)
    sql, params = ch.query_all_calls[0]
    assert "memory_key IN {keys:Array(String)}" in sql
    assert params["keys"] == ["k1", "k2", "k3"]
    assert len(out) == 2
    assert all(isinstance(r, MemoryRow) for r in out)


@pytest.mark.asyncio
async def test_get_by_id_uses_uuid_param_type() -> None:
    """get_by_id must declare the param as :UUID so CH parses correctly —
    a regression would silently use string-comparison and return nothing."""
    ch = StubCH()
    ch.query_one_returns.append(None)
    await get_by_id(
        "11111111-2222-3333-4444-555555555555",
        org="o",
        namespace="n",
        ch=ch,
        scope=WHOLE_NAMESPACE,
    )
    sql, params = ch.query_one_calls[0]
    assert "{mid:UUID}" in sql
    assert params["mid"] == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_get_by_id_scopes_by_org_and_namespace() -> None:
    """A leaked memory_id can't surface another tenant's row, and a restored copy of
    the id in another namespace is not this namespace's row."""
    ch = StubCH()
    ch.query_one_returns.append(None)
    await get_by_id(
        "11111111-2222-3333-4444-555555555555",
        org="clickhouse",
        namespace="ns",
        ch=ch,
        scope=WHOLE_NAMESPACE,
    )
    sql, params = ch.query_one_calls[0]
    assert "org = {org:String} AND namespace = {in_ns:String}" in sql
    assert (params["org"], params["in_ns"]) == ("clickhouse", "ns")


def test_identity_key_reads_a_model_a_dict_and_a_driver_record_alike() -> None:
    cols = dict(org="o", namespace="n", agent="a", user_id="u", session_id="s", memory_key="k")

    class Record:  # indexed by column name only, as a Postgres driver record is
        def __getitem__(self, name: str) -> Any:
            return cols[name]

    row = projection_row(**cols)
    expected = ("o", "n", "a", "u", "s", "k")
    assert identity_key(row) == expected
    assert identity_key(MemoryRow.from_snapshot(row)) == expected
    assert identity_key(Record()) == expected


def test_a_copy_is_drawn_above_the_head_it_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A head written on a replica whose clock is ahead would overtake a copy drawn here
    (a lock answering locked while the head stays unlocked): the copy goes above it."""
    from types import SimpleNamespace

    from server.memory.store import rows

    monkeypatch.setattr(rows, "next_version", lambda: 100)
    assert rows.version_over(SimpleNamespace(version=150)) == 152
    assert rows.version_over(SimpleNamespace(version=40)) == 100
