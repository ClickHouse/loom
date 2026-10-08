"""Read-your-writes for memory.search.

An empty pool right after a write is what a lagging ClickHouse Cloud replica
looks like. The search pays one more pass with sequential consistency only
then; the scope stamps the setting on every statement it issues."""

from __future__ import annotations

import asyncio

import pytest

from server.core.ch import query_settings, sequential_reads, sequential_reads_active
from server.memory.app.ask.prompts import _AFFECT_SYSTEM, _COUNT_SYSTEM, _OPTIONS_SYSTEM
from server.memory.app.search.consistency import _sequential_retry_wanted


def test_scope_stamps_sequential_consistency_on_queries_inside_only() -> None:
    assert "select_sequential_consistency" not in query_settings()
    assert not sequential_reads_active()
    with sequential_reads():
        assert sequential_reads_active()
        assert query_settings()["select_sequential_consistency"] == 1
        # A caller's explicit value wins over the scope.
        assert (
            query_settings({"select_sequential_consistency": 0})["select_sequential_consistency"]
            == 0
        )
        # Other per-query settings are kept next to it.
        assert query_settings({"max_threads": 2})["max_threads"] == 2
    assert not sequential_reads_active()
    assert "select_sequential_consistency" not in query_settings()


def test_scope_survives_an_exception_and_tasks_inherit_it() -> None:
    with pytest.raises(RuntimeError), sequential_reads():
        raise RuntimeError("leg failed")
    assert not sequential_reads_active()

    async def probe() -> bool:
        return sequential_reads_active()

    async def main() -> tuple[bool, bool]:
        with sequential_reads():
            inside = await asyncio.wait_for(probe(), timeout=1)
        outside = await asyncio.wait_for(probe(), timeout=1)
        return inside, outside

    assert asyncio.run(main()) == (True, False)


def test_retry_only_on_an_empty_pool_once_with_budget_and_flag() -> None:
    base = dict(pool_empty=True, already_sequential=False, enabled=True, remaining_wall_s=2.0)
    assert _sequential_retry_wanted(**base)
    assert not _sequential_retry_wanted(**{**base, "pool_empty": False})
    assert not _sequential_retry_wanted(**{**base, "already_sequential": True})
    assert not _sequential_retry_wanted(**{**base, "enabled": False})
    assert not _sequential_retry_wanted(**{**base, "remaining_wall_s": 0.01})


def test_option_answers_begin_with_the_chosen_letter() -> None:
    # The bench reads the FIRST standalone letter; an answer that names the
    # runner-up before the choice ("Fear … runner-ups B/C fail … D") is
    # scored as the runner-up. Every options-bearing mode says letter first.
    for prompt in (_OPTIONS_SYSTEM, _COUNT_SYSTEM, _AFFECT_SYSTEM):
        assert "BEGINS with" in prompt
    for prompt in (_OPTIONS_SYSTEM, _AFFECT_SYSTEM):
        assert (
            "runner-up letter is never written before it" in prompt
            or "never name a runner-up letter before" in prompt
        )
    assert "'ANSWER: D) Fear" in _AFFECT_SYSTEM
    assert "'ANSWER: D) 3" in _COUNT_SYSTEM


def test_recent_write_registry_notes_memory_table_inserts_and_expires() -> None:
    from server.memory.app import recent_writes

    recent_writes._RECENT_WRITES.clear()
    assert recent_writes.namespaces_of_rows(
        "loom.memory", [{"org": "o", "namespace": "n1"}, {"org": "o", "namespace": "n1"}]
    ) == {("o", "n1")}
    assert recent_writes.namespaces_of_rows(
        "loom.memory_chunks", [{"org": "o", "namespace": "n2", "x": 1}]
    ) == {("o", "n2")}
    # Other tables and rows without both columns are ignored.
    assert (
        recent_writes.namespaces_of_rows("loom.agent_events", [{"org": "o", "namespace": "n"}])
        == set()
    )
    assert recent_writes.namespaces_of_rows("loom.memory", [{"org": "o"}]) == set()

    recent_writes.note_recent_write("o", "n1", now=100.0)
    assert recent_writes.written_recently("o", "n1", 20.0, now=110.0)
    assert not recent_writes.written_recently("o", "n1", 20.0, now=121.0)
    assert not recent_writes.written_recently("o", "other", 20.0, now=101.0)
    assert not recent_writes.written_recently("o", "n1", 0.0, now=101.0)  # window off
    recent_writes._RECENT_WRITES.clear()


def test_recent_write_registry_stays_bounded() -> None:
    from server.memory.app import recent_writes

    recent_writes._RECENT_WRITES.clear()
    for i in range(recent_writes._RECENT_WRITES_MAX + 10):
        recent_writes.note_recent_write("o", f"ns{i}", now=float(i))
    assert len(recent_writes._RECENT_WRITES) <= recent_writes._RECENT_WRITES_MAX
    # The newest survive the trim.
    assert ("o", f"ns{recent_writes._RECENT_WRITES_MAX + 9}") in recent_writes._RECENT_WRITES
    recent_writes._RECENT_WRITES.clear()


def test_fresh_namespace_read_gates_on_window_flag_and_registry() -> None:
    from server.memory.app import recent_writes
    from server.memory.app.search.consistency import _fresh_namespace_read

    recent_writes._RECENT_WRITES.clear()
    recent_writes.note_recent_write("o", "fresh")
    base = dict(already_sequential=False, enabled=True, window_s=20.0)
    assert _fresh_namespace_read("o", "fresh", **base)
    assert not _fresh_namespace_read("o", "cold", **base)
    assert not _fresh_namespace_read("o", "fresh", **{**base, "already_sequential": True})
    assert not _fresh_namespace_read("o", "fresh", **{**base, "enabled": False})
    assert not _fresh_namespace_read("o", "fresh", **{**base, "window_s": 0.0})
    recent_writes._RECENT_WRITES.clear()


def test_fresh_hit_reread_fires_only_for_a_just_ingested_hit() -> None:
    from datetime import datetime, timedelta

    from server.memory.app.search.consistency import (
        _fresh_hit_reread_wanted,
        _newest_hit_age_s,
    )

    class _H:
        def __init__(self, created_at: str) -> None:
            self.created_at = created_at

    now = datetime(2026, 9, 12, 12, 0, 0)
    fresh = _H((now - timedelta(seconds=3)).strftime("%Y-%m-%d %H:%M:%S"))
    old = _H((now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    unknown = _H("")
    epoch = _H("1970-01-01 00:00:00")
    assert _newest_hit_age_s([old, fresh], now=now) == 3.0
    assert _newest_hit_age_s([unknown, epoch], now=now) == float("inf")

    class _Now(_H):
        pass

    # The predicate uses wall-clock now; a hit stamped this second is fresh.
    from datetime import UTC

    just_now = _H(datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"))
    base = dict(already_sequential=False, enabled=True, window_s=20.0)
    assert _fresh_hit_reread_wanted([just_now], **base)
    assert not _fresh_hit_reread_wanted([old], **base)
    assert not _fresh_hit_reread_wanted([], **base)
    assert not _fresh_hit_reread_wanted([just_now], **{**base, "already_sequential": True})
    assert not _fresh_hit_reread_wanted([just_now], **{**base, "enabled": False})
    assert not _fresh_hit_reread_wanted([just_now], **{**base, "window_s": 0.0})


def test_a_spent_wall_budget_reads_as_spent() -> None:
    """`remaining_wall_s` is floored at 0.1 s for wait_for, so the re-read gates read
    `wall_left_s`, which goes to zero."""
    from server.memory.app.search.budget import SearchBudget

    budget = SearchBudget(max_wall_ms=0)
    assert budget.remaining_wall_s() == 0.1
    assert budget.wall_left_s() <= 0


def _empty_pass_ctx(budget_ms: int) -> object:
    from types import SimpleNamespace

    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.budget import SearchBudget

    return SimpleNamespace(
        input=MemorySearchInput(org="o", namespace="n", agent="a", query="q"),
        search_budget=SearchBudget(max_wall_ms=budget_ms),
        namespaces=["n"],
    )


def test_the_empty_pool_retry_skips_a_spent_budget(monkeypatch) -> None:
    import server.memory.app.search as pkg
    from server.memory.app.search.consistency import _consistency_reread

    calls: list[object] = []

    async def search(inp: object) -> object:
        calls.append(inp)
        raise AssertionError("no re-read once the budget is spent")

    monkeypatch.setattr(pkg, "handle_memory_search", search)
    monkeypatch.setattr("server.memory.store.read_your_writes_enabled", lambda: True)
    assert asyncio.run(_consistency_reread(_empty_pass_ctx(0), [])) is None  # type: ignore[arg-type]
    assert calls == []


def test_a_failed_retry_keeps_the_first_pass(monkeypatch) -> None:
    import server.memory.app.search as pkg
    from server.memory.app.search.consistency import _consistency_reread

    async def search(inp: object) -> object:
        raise RuntimeError("replica unavailable")

    monkeypatch.setattr(pkg, "handle_memory_search", search)
    monkeypatch.setattr("server.memory.store.read_your_writes_enabled", lambda: True)
    assert asyncio.run(_consistency_reread(_empty_pass_ctx(60_000), [])) is None  # type: ignore[arg-type]
