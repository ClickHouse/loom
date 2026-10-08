"""The orphan reconciliation looks at what just arrived on
every pass, and at everything on a slow clock.

`find_memories_without_embedding_rows(since_seconds=…)` narrows the anti-join
to the memories of the window and the embedding keys of their namespaces;
`None` is the full join. The worker's
`_orphan_scan_window` hands out `None` on the first pass and whenever the
slow clock has elapsed, the recent window otherwise, and `None` always when
the clock is switched off.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.workers import embedding as w
from server.settings import settings


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        return []


@pytest.mark.asyncio
async def test_window_narrows_both_sides_of_the_anti_join() -> None:
    ch = _CH()
    out = await memory_repo.find_memories_without_embedding_rows(
        batch_size=100, since_seconds=900, ch=ch
    )
    assert out == []
    sql, params = ch.calls[0]
    assert "WITH recent AS" in sql and "SELECT DISTINCT org, namespace" in sql
    # the recent-namespace set is read without FINAL (a superset only widens the pruned side)
    recent = sql.split("WITH recent AS", 1)[1].split("SELECT m.org", 1)[0]
    assert (
        "FINAL" not in recent
        and "created_at > now() - toIntervalSecond({since_s:UInt32})" in recent
    )
    # right side pruned to those namespaces; left side to the window and those namespaces
    assert "WHERE tombstoned_at IS NULL AND (org, namespace) IN recent" in sql
    assert "(m.org, m.namespace) IN recent" in sql
    assert "m.created_at > now() - toIntervalSecond({since_s:UInt32})" in sql
    assert params["since_s"] == 900 and params["cand_lim"] == 200
    # the join itself is unchanged
    assert "LEFT ANTI JOIN" in sql and "FROM loom.memory AS m FINAL" in sql


@pytest.mark.asyncio
async def test_no_window_is_the_full_join() -> None:
    ch = _CH()
    await memory_repo.find_memories_without_embedding_rows(batch_size=50, ch=ch)
    sql, params = ch.calls[0]
    assert "WITH recent" not in sql and "IN recent" not in sql
    assert "since_s" not in params
    assert "FROM loom.memory_embeddings FINAL\n            WHERE tombstoned_at IS NULL\n" in sql


@pytest.mark.asyncio
async def test_zero_window_means_full_join_too() -> None:
    ch = _CH()
    await memory_repo.find_memories_without_embedding_rows(batch_size=50, since_seconds=0, ch=ch)
    assert "IN recent" not in ch.calls[0][0]


def test_clock_hands_out_full_then_recent_then_full_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embedding_worker_orphan_full_scan_seconds", 1800)
    monkeypatch.setattr(settings, "embedding_worker_orphan_recent_seconds", 900)
    monkeypatch.setattr(w, "_last_full_orphan_scan", 0.0)
    assert w._orphan_scan_window(now=1000.0) is None  # first pass: full
    assert w._orphan_scan_window(now=1005.0) == 900  # next passes: recent window
    assert w._orphan_scan_window(now=2799.0) == 900
    assert w._orphan_scan_window(now=2800.0) is None  # slow clock elapsed: full
    assert w._orphan_scan_window(now=2805.0) == 900


def test_clock_off_is_the_unwindowed_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embedding_worker_orphan_full_scan_seconds", 0)
    monkeypatch.setattr(w, "_last_full_orphan_scan", 0.0)
    assert w._orphan_scan_window(now=1.0) is None
    assert w._orphan_scan_window(now=2.0) is None
    monkeypatch.setattr(settings, "embedding_worker_orphan_full_scan_seconds", 1800)
    monkeypatch.setattr(settings, "embedding_worker_orphan_recent_seconds", 0)
    assert w._orphan_scan_window(now=3.0) is None


@pytest.mark.asyncio
async def test_run_pass_passes_the_window_to_the_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []

    async def _orphans(**kw: Any) -> list[dict[str, Any]]:
        seen.append(kw.get("since_seconds"))
        return []

    async def _none(**kw: Any) -> list[dict[str, Any]]:
        return []

    class _Embedder:
        model_name = "fake"
        dim = 3

    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", _orphans)
    monkeypatch.setattr(memory_repo, "find_embedding_stubs", _none)
    monkeypatch.setattr(w, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(w, "_orphan_scan_window", lambda: 900)
    await w.run_pass()
    assert seen == [900]


@pytest.mark.asyncio
async def test_phase_two_never_references_the_phase_one_cte() -> None:
    """The window's `(m.org, m.namespace) IN recent` must not ride the shared
    predicate into phase two, whose statement has no `recent` CTE (ClickHouse
    code 47 on every windowed pass, and orphaned memories never re-embedded).
    Phase two addresses exact keys; only the org/namespace filters may follow
    it."""

    class _TwoPhase:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
            self.calls.append((sql, dict(params or {})))
            if len(self.calls) == 1:
                return [
                    {
                        "org": "o",
                        "namespace": "n",
                        "agent": "a",
                        "user_id": "u",
                        "session_id": "s",
                        "memory_key": "k",
                    }
                ]
            return []

    ch = _TwoPhase()
    await memory_repo.find_memories_without_embedding_rows(
        batch_size=10, since_seconds=900, org_filter="o", ch=ch
    )
    assert len(ch.calls) == 2
    p1, p1_params = ch.calls[0]
    assert "WITH recent AS" in p1 and "(m.org, m.namespace) IN recent" in p1
    assert p1_params["since_s"] == 900
    p2, p2_params = ch.calls[1]
    assert "recent" not in p2, p2
    assert "toIntervalSecond" not in p2
    assert "m.org = {org_filter:String}" in p2  # the scope filter still applies
    assert "since_s" not in p2_params and p2_params["keys"] == [("o", "n", "a", "u", "s", "k")]


@pytest.mark.asyncio
@pytest.mark.parametrize("since", [None, 900])
async def test_rows_that_are_never_embedded_are_not_orphans(since: int | None) -> None:
    """memory.set writes no embedding row for an episodic or profile row
    (`is_embedded`), so the reconcile must not stub and embed one either."""
    from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES

    ch = _CH()
    await memory_repo.find_memories_without_embedding_rows(
        batch_size=10, since_seconds=since, ch=ch
    )
    sql, params = ch.calls[0]
    assert "AND m.memory_type NOT IN {unsearchable:Array(String)}" in sql
    assert params["unsearchable"] == sorted(NON_SEARCHABLE_TYPES)
