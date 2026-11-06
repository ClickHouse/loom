"""The served reads on clickhouse-local against the production tables: which rows of a
key memory.get counts, and the window memory.health reads."""

from __future__ import annotations

import asyncio

import pytest

from server.identity.domain.read_scope import ReadFilters
from server.memory.store import usage
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")

M1 = "00000000-0000-0000-0000-0000000000a1"
M2 = "00000000-0000-0000-0000-0000000000a2"


def _memory(mid: str, user: str, session: str, key: str = "k") -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_id, value, version) VALUES "
        f"('o', 'n', 'w', '{user}', '{session}', '{key}', '{mid}', 'v', 1)"
    )


def _event(mid: str, kind: str, agent: str, days_ago: int = 0) -> str:
    return (
        "INSERT INTO loom.memory_usage_events (memory_id, org, namespace, event_type, "
        f"event_count, ts, agent) VALUES ('{mid}', 'o', 'n', '{kind}', 1, "
        f"now() - INTERVAL {days_ago} DAY, '{agent}')"
    )


def _ch(*rows: str) -> LocalCH:
    return LocalCH(ddl("memory"), ddl("memory_usage_events"), ddl("memory_usage_totals"), *rows)


def _memory_at(mid: str, user: str, session: str, updated: str) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_id, value, version, updated_at) VALUES "
        f"('o', 'n', 'w', '{user}', '{session}', 'k', '{mid}', 'v', 1, '{updated}')"
    )


OWN_U1 = ReadFilters(scope="own_user", user_id="u1", session_id=None)


def test_the_key_counts_every_live_row_and_a_narrowed_reader_its_own() -> None:
    ch = _ch(
        _memory(M1, "u1", "s1"),
        _memory(M2, "u2", "s2"),
        _event(M1, "recall", "a1"),
        _event(M2, "served", "a2"),
        _event(M2, "used", "a2"),  # not a read
    )
    every = asyncio.run(usage.served_by_agent("o", "n", "k", ch=ch))  # type: ignore[arg-type]
    assert sorted((r["agent"], int(r["n"])) for r in every) == [("a1", 1), ("a2", 1)]
    own = asyncio.run(usage.served_by_agent("o", "n", "k", scope=OWN_U1, ch=ch))  # type: ignore[arg-type]
    assert [(r["agent"], int(r["n"])) for r in own] == [("a1", 1)]


def test_a_narrowed_readers_event_lands_on_its_own_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """u2's copy of the key is the newest; u1's narrowed read was served u1's copy, so its
    event lands there, where u1's figures are read, and never on u2's."""
    ch = _ch(
        _memory_at(M1, "u1", "s1", "2026-01-01 00:00:00"),
        _memory_at(M2, "u2", "s2", "2026-02-01 00:00:00"),
    )
    landed: list[str] = []

    async def _event_on(**kw: object) -> None:
        landed.append(str(kw["memory_id"]))

    monkeypatch.setattr(usage, "record_usage_event", _event_on)
    asyncio.run(
        usage.record_usage_for_keys("o", {"n": ["k"]}, event_type="served", ch=ch)  # type: ignore[arg-type]
    )
    asyncio.run(
        usage.record_usage_for_keys(  # type: ignore[arg-type]
            "o", {"n": ["k"]}, event_type="served", scope=OWN_U1, ch=ch
        )
    )
    assert landed == [M2, M1]


def test_health_counts_served_events_inside_the_silent_window_only() -> None:
    from server.memory.app import health

    ch = _ch(_memory(M1, "u1", "s1"), _event(M1, "served", "a1", days_ago=40))

    def served(days: int) -> int:
        rows = asyncio.run(ch.query_all(health._LIVE, {"org": "o", "ns": "n", "days": days}))
        return int(rows[0]["served_count"])

    assert served(30) == 0
    assert served(60) == 1
