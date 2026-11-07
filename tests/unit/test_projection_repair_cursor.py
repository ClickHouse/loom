from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import projection_failures as pf
from server.memory.workers import projection_repair as worker


def _failure(i: int, *, at: datetime | None = None, failure_id: str | None = None) -> Any:
    return SimpleNamespace(
        failure_id=failure_id or f"00000000-0000-0000-0000-{i:012d}",
        created_at=at or datetime(2026, 1, 1) + timedelta(seconds=i),
        projection="memory_chunks",
    )


def _selection(monkeypatch: pytest.MonkeyPatch, rows: list[Any]):
    seen = []

    async def off():
        return frozenset()

    async def scan(*, limit, exclude_orgs, after):
        seen.append(after)
        start = next(
            (i + 1 for i, row in enumerate(rows) if after == (row.created_at, row.failure_id)),
            0,
        )
        return rows[start : start + limit]

    monkeypatch.setattr(worker.customer, "switched_off_orgs", off)
    monkeypatch.setattr(pf, "scan_active_projection_failures", scan)
    return seen


async def test_three_full_held_pages_do_not_starve_a_healthy_writer(monkeypatch):
    from server.workers import holds

    rows = [_failure(i) for i in range(76)]
    _selection(monkeypatch, rows)
    now = [0.0]
    monkeypatch.setattr(holds.time, "monotonic", lambda: now[0])
    retried = []

    async def retry(failures):
        retried.append([row.failure_id for row in failures])
        return [
            pf.ProjectionFailureRetryResult(
                row.failure_id,
                row.projection,
                retried=row is rows[-1],
                resolved=row is rows[-1],
                reason="" if row is rows[-1] else "model_calls_held",
            )
            for row in failures
        ]

    monkeypatch.setattr(pf, "retry_failures", retry)
    cfg = worker.ProjectionRepairConfig(max_failures_per_pass=25)
    outcomes = []
    for i in range(4):
        now[0] = i * 300.0
        outcomes.append(await worker.run_pass(cfg))

    assert [o.scanned for o in outcomes] == [25, 25, 25, 1]
    assert [o.retried for o in outcomes] == [0, 0, 0, 1]
    assert [key for page in retried for key in page] == [row.failure_id for row in rows]
    assert all(not outcome.failed for outcome in outcomes)


async def test_timestamp_ties_follow_the_native_uuid_order_returned_by_the_store(monkeypatch):
    at = datetime(2026, 1, 1)
    rows = [
        _failure(1, at=at, failure_id="00000000-0000-0001-0000-000000000000"),
        _failure(2, at=at, failure_id="00000000-0000-0000-0000-000000000001"),
        _failure(3, at=at, failure_id="00000000-0000-0000-0000-000000000002"),
    ]
    seen = _selection(monkeypatch, rows)
    visited = []

    async def retry(failures):
        visited.extend(row.failure_id for row in failures)
        return [pf._held(row) for row in failures]

    monkeypatch.setattr(pf, "retry_failures", retry)
    cfg = worker.ProjectionRepairConfig(max_failures_per_pass=1)
    for _ in rows:
        await worker.run_pass(cfg)
    assert visited == [row.failure_id for row in rows]
    assert seen == [None, (at, rows[0].failure_id), (at, rows[1].failure_id)]


@pytest.mark.parametrize("size", [2, 3])
async def test_wrapping_retries_a_previously_held_failure(monkeypatch, size):
    rows = [_failure(i) for i in range(size)]
    _selection(monkeypatch, rows)
    held = True
    retried = []

    async def retry(failures):
        if held:
            return [pf._held(row) for row in failures]
        retried.extend(row.failure_id for row in failures)
        return [
            pf.ProjectionFailureRetryResult(row.failure_id, row.projection, True, True)
            for row in failures
        ]

    monkeypatch.setattr(pf, "retry_failures", retry)
    cfg = worker.ProjectionRepairConfig(max_failures_per_pass=2)
    await worker.run_pass(cfg)
    await worker.run_pass(cfg)  # A short or empty page completes the walk.
    held = False
    await worker.run_pass(cfg)
    assert retried == [row.failure_id for row in rows[:2]]


@pytest.mark.parametrize("stage", ["scan", "retry"])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_pass_keeps_its_page(monkeypatch, stage, error):
    rows = [_failure(i) for i in range(3)]
    seen = _selection(monkeypatch, rows)

    async def retry(failures):
        return [pf._held(row) for row in failures]

    monkeypatch.setattr(pf, "retry_failures", retry)
    cfg = worker.ProjectionRepairConfig(max_failures_per_pass=1)
    await worker.run_pass(cfg)
    position = (rows[0].created_at, rows[0].failure_id)

    async def fail(*args, **kwargs):
        raise error()

    with monkeypatch.context() as patch:
        patch.setattr(
            pf, "scan_active_projection_failures" if stage == "scan" else "retry_failures", fail
        )
        with pytest.raises(error):
            await worker.run_pass(cfg)
    await worker.run_pass(cfg)
    assert seen[-1] == position


@pytest.mark.parametrize("after", [False, True])
async def test_scan_uses_the_same_native_key_for_comparison_and_order(after):
    class CH:
        async def query_all(self, sql, params):
            self.sql, self.params = sql, params
            return []

    ch = CH()
    at = datetime(2026, 1, 1)
    failure_id = "00000000-0000-0001-0000-000000000000"
    await pf.scan_active_projection_failures(after=(at, failure_id) if after else None, ch=ch)
    assert "ORDER BY f.created_at ASC, f.failure_id ASC" in ch.sql
    assert "held" not in ch.params
    # Intents a retry leaves alone within their grace hold no place in the page.
    assert "AND NOT (projection IN {graced:Array(String)}" in ch.sql
    assert "created_at > now() - INTERVAL {grace:UInt32} SECOND)" in ch.sql
    assert set(ch.params["graced"]) == {pf.FOLLOW_OWED, pf.SENSITIVITY_FACTS}
    assert ch.params["grace"] == pf.OWED_GRACE_S
    if after:
        assert "(f.created_at, f.failure_id) > ({after_at:DateTime}, {after_id:UUID})" in ch.sql
        assert ch.params["after_at"] == at.replace(tzinfo=UTC)
        assert ch.params["after_id"] == failure_id
    else:
        assert "after_at" not in ch.params
        assert "after_id" not in ch.params


async def test_scan_pages_timestamp_ties_in_clickhouse_uuid_order():
    from tests.unit import clickhouse_local

    if clickhouse_local.CLICKHOUSE is None:
        pytest.skip("clickhouse binary not installed")
    ids = [
        "00000000-0000-0001-0000-000000000000",
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0002-0000-000000000000",
    ]
    values = ",".join(
        f"('{key}', 'org', 'ns', 'memory_chunks', toDateTime('2026-01-01 00:00:00', 'UTC'))"
        for key in ids
    )
    setup = [
        clickhouse_local.ddl("memory_projection_failures"),
        clickhouse_local.ddl("erasure_requests"),
        "INSERT INTO loom.memory_projection_failures "
        "(failure_id, org, namespace, projection, created_at) VALUES " + values,
    ]
    expected = [
        row["failure_id"]
        for row in clickhouse_local.run(
            setup,
            "SELECT toString(f.failure_id) AS failure_id "
            "FROM loom.memory_projection_failures AS f "
            "ORDER BY f.created_at ASC, f.failure_id ASC",
        )
    ]
    assert expected != sorted(expected), "the fixture must distinguish UUID and string ordering"

    class CH:
        async def query_all(self, sql, params):
            rows = clickhouse_local.run(setup, sql, params)
            for row in rows:
                row["created_at"] = datetime.fromisoformat(row["created_at"])
            return rows

    after = None
    found = []
    for _ in range(3):
        page = await pf.scan_active_projection_failures(limit=2, after=after, ch=CH())
        found.extend(row.failure_id for row in page)
        if not page:
            break
        after = (page[-1].created_at, page[-1].failure_id)
    assert found == expected
    assert page == []
