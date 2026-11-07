"""The served reads on clickhouse-local against the production tables: where a read lands,
which rows of a key memory.get counts, and the window memory.health reads."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from server.core.datetimes import naive_utc_now
from server.identity.domain.read_scope import ReadFilters
from server.memory.store import served
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")

M1 = "00000000-0000-0000-0000-0000000000a1"
M2 = "00000000-0000-0000-0000-0000000000a2"
OWN_U1 = ReadFilters(scope="own_user", user_id="u1", session_id=None)


def _memory(mid: str, user: str, updated: str) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_id, value, version, updated_at) VALUES "
        f"('o', 'n', 'w', '{user}', 's', 'k', '{mid}', 'v', 1, '{updated}')"
    )


@pytest.fixture
def ch(tmp_path: Path) -> KeptCH:
    """u2's copy of key `k` is the newest."""
    db = KeptCH(
        tmp_path,
        ddl("memory"),
        ddl("memory_served_daily"),
        ddl("memory_usage_totals"),
    )
    db.create(
        _memory(M1, "u1", "2026-01-01 00:00:00"),
        _memory(M2, "u2", "2026-02-01 00:00:00"),
    )
    return db


def _record(ch: KeptCH, agent: str, scope: ReadFilters | None = None) -> None:
    kw = {"scope": scope} if scope else {}
    asyncio.run(served.record_served("o", {"n": ["k"]}, agent=agent, ch=ch, **kw))  # type: ignore[arg-type]


def _by_agent(ch: KeptCH, scope: ReadFilters | None = None) -> list[tuple[str, int]]:
    kw = {"scope": scope} if scope else {}
    rows = asyncio.run(served.served_by_agent("o", "n", "k", ch=ch, **kw))  # type: ignore[arg-type]
    return sorted((r["agent"], int(r["n"])) for r in rows)


def test_a_read_lands_on_the_newest_copy_inside_the_readers_scope(ch: KeptCH) -> None:
    _record(ch, "wide")  # a namespace-wide reader: the newest copy, u2's
    _record(ch, "mine", OWN_U1)  # u1's narrowed reader: u1's own copy
    rows = asyncio.run(
        ch.query_all("SELECT toString(memory_id) AS m, agent FROM loom.memory_served_daily")
    )
    assert sorted((r["m"], r["agent"]) for r in rows) == [(M1, "mine"), (M2, "wide")]


def test_the_key_counts_every_live_row_and_a_narrowed_reader_its_own(ch: KeptCH) -> None:
    _record(ch, "wide")
    _record(ch, "wide")
    _record(ch, "mine", OWN_U1)
    assert _by_agent(ch) == [("mine", 1), ("wide", 2)]
    # u1 sees its own agent's read, never what landed on u2's copy
    assert _by_agent(ch, OWN_U1) == [("mine", 1)]


def test_health_counts_served_rows_inside_the_silent_window_only(ch: KeptCH) -> None:
    from server.memory.app import health

    day = (naive_utc_now().date() - timedelta(days=40)).isoformat()
    ch.create(
        "INSERT INTO loom.memory_served_daily (org, namespace, memory_id, agent, day, served, "
        f"last_at) VALUES ('o', 'n', '{M1}', 'a', '{day}', 3, '{day} 12:00:00')"
    )

    def served_count(days: int) -> int:
        since = naive_utc_now().date() - timedelta(days=days)
        rows = asyncio.run(ch.query_all(health._LIVE, {"org": "o", "ns": "n", "since": since}))
        return {r["memory_id"]: int(r["served_count"]) for r in rows}[M1]

    assert served_count(30) == 0
    assert served_count(60) == 3


def test_health_counts_a_read_that_landed_on_a_copy_it_does_not_list(tmp_path: Path) -> None:
    """A read lands on the newest live copy of the key, which may be one the health rows
    leave out (a rejected copy written after the active one): the key is still read."""
    from server.memory.app import health

    db = KeptCH(tmp_path, ddl("memory"), ddl("memory_served_daily"), ddl("memory_usage_totals"))
    db.create(
        _memory(M1, "u1", "2026-01-01 00:00:00"),
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_id, value, version, updated_at, status) VALUES "
        f"('o', 'n', 'w', 'u1', 's2', 'k', '{M2}', 'v', 1, '2026-02-01 00:00:00', 'rejected')",
    )
    _record(db, "codex")  # lands on M2, the newest copy
    since = naive_utc_now().date() - timedelta(days=30)
    rows = asyncio.run(db.query_all(health._LIVE, {"org": "o", "ns": "n", "since": since}))
    assert [(r["memory_id"], int(r["served_count"])) for r in rows] == [(M1, 1)]
    assert health.summarise([dict(r) for r in rows], silent_after_days=30, top_k=1)["silent"] == 0
