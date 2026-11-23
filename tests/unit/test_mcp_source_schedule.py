"""Finite scheduler tests without stores, clients or source-engine substitution claims."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, replace

import pytest

from server.knowledge.app import mcp_source_schedule as schedule
from server.knowledge.store.mcp_source_schedule import Header, QueryLimits, candidate_page


@dataclass
class Budget:
    left: int


LIMITS = schedule.Limits(4, 20, 8, 4, 2, 1, 2, 1)


def header(index: int, org: str = "org") -> Header:
    return Header(
        org, "ns", str(index), "random-" + str(index), "profile", "a" * 64, f"{index:064X}"
    )


@pytest.fixture
def cursors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    values: list[str] = []
    monkeypatch.setattr(schedule.pass_context, "resume_cursor", lambda: "")
    monkeypatch.setattr(schedule.pass_context, "set_cursor", values.append)
    monkeypatch.setattr(schedule.pass_context, "check_stop", lambda: None)
    return values


def page_of(rows: list[Header]):
    async def page(after: str, size: int) -> list[Header]:
        return [row for row in rows if row.schedule_key > after][:size]

    return page


async def run(rows: list[Header], execute, *, parent: Budget | None = None, limits=LIMITS):
    return await schedule.dispatch(
        limits=limits,
        parent=parent or Budget(100),
        make_pass=Budget,
        execute=execute,
        endpoint_key=lambda row: row.name,
        deadline=time.monotonic() + 5,
        page=page_of(rows),
        resume_cursor=schedule.pass_context.resume_cursor(),
        checkpoint=schedule.pass_context.set_cursor,
    )


@pytest.mark.asyncio
async def test_reserves_before_task_start_and_refunds_unused(cursors):
    parent = Budget(5)
    current = peak = consumed = 0

    async def execute(row, budget):
        nonlocal current, peak, consumed
        current += budget.left
        peak = max(peak, current)
        assert current + parent.left <= 5
        reserved = budget.left
        await asyncio.sleep(0)
        budget.left -= 1
        consumed += 1
        current -= reserved

    metrics = await run([header(i, str(i % 3)) for i in range(1, 10)], execute, parent=parent)
    assert peak <= 5
    assert consumed == 5 and parent.left == 0
    assert metrics.started == 5
    assert metrics.deferred > 0


@pytest.mark.asyncio
async def test_round_robin_and_endpoint_caps(cursors):
    starts: list[str] = []
    active: Counter[str] = Counter()
    endpoints: Counter[str] = Counter()
    gate = asyncio.Event()

    async def execute(row, budget):
        endpoint = "shared" if int(row.name) <= 6 else row.name
        starts.append(row.org)
        active[row.org] += 1
        endpoints[endpoint] += 1
        assert active[row.org] <= 2 and endpoints[endpoint] <= 1
        if len(starts) >= 3:
            gate.set()
        await gate.wait()
        budget.left -= 1
        active[row.org] -= 1
        endpoints[endpoint] -= 1

    rows = [header(i, "faulty") for i in range(1, 7)] + [header(7, "healthy"), header(8, "third")]
    metrics = await schedule.dispatch(
        limits=replace(LIMITS, page_size=8),
        parent=Budget(100),
        make_pass=Budget,
        execute=execute,
        endpoint_key=lambda row: "shared" if int(row.name) <= 6 else row.name,
        deadline=time.monotonic() + 5,
        page=page_of(rows),
        resume_cursor=schedule.pass_context.resume_cursor(),
        checkpoint=schedule.pass_context.set_cursor,
    )
    assert starts[:3] == ["faulty", "healthy", "third"]
    assert metrics.completed == 8
    assert metrics.queue_highwater <= 8 and metrics.inflight_highwater <= 4
    assert metrics.endpoint_highwater == 1


@pytest.mark.asyncio
async def test_failed_busy_sources_do_not_pin_scan_cursor(cursors):
    seen: list[str] = []

    async def execute(row, budget):
        seen.append(row.org)
        if row.org == "faulty":
            raise ValueError("private detail must not escape")
        budget.left -= 1

    metrics = await run([header(1, "faulty"), header(2, "healthy")], execute)
    assert metrics.failed == 1 and metrics.completed == 1
    assert seen == ["faulty", "healthy"]
    assert cursors[-1] == "mcp1:" + "0" * 64
    assert all("faulty" not in value and "healthy" not in value for value in cursors)
    assert "private" not in repr(metrics)


@pytest.mark.asyncio
async def test_cancellation_joins_and_returns_remaining_credits(cursors):
    parent, started, closed = Budget(4), asyncio.Event(), asyncio.Event()

    async def execute(row, budget):
        budget.left -= 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(run([header(1)], execute, parent=parent))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done() and closed.is_set()
    assert parent.left == 3


@pytest.mark.asyncio
async def test_counter_inflation_fails_closed(cursors):
    parent = Budget(4)

    async def execute(row, budget):
        budget.left += 1

    with pytest.raises(ValueError, match="mcp_schedule_budget"):
        await run([header(1)], execute, parent=parent)
    assert parent.left == 2


@pytest.mark.asyncio
async def test_store_page_has_no_body_and_has_physical_query_bounds():
    class Store:
        async def query_all(self, sql, params, *, settings):
            assert (
                "items_json" not in sql and "credential_enc" not in sql and "secret_enc" not in sql
            )
            assert "source_kind = 'mcp'" in sql and "source_contract_version = 1" in sql
            assert "'snapshot', 'selected_refresh'" in sql and "'paused'" in sql
            assert "LIMIT {limit:UInt64}" in sql
            assert params["org"] == "tenant" and params["ns"] == "collection"
            assert settings == {
                "max_rows_to_read": 1000,
                "max_bytes_to_read": 100000,
                "max_execution_time": 1,
                "read_overflow_mode": "throw",
            }
            return [vars(header(1, "tenant"))]

    rows = await candidate_page(
        "",
        2,
        query_limits=QueryLimits(1000, 100000, 1),
        org="tenant",
        namespace="collection",
        ch=Store(),
    )
    assert rows == [header(1, "tenant")]


@pytest.mark.asyncio
async def test_page_and_scope_validation():
    class Store:
        async def query_all(self, sql, params, *, settings):
            return [vars(header(2)), vars(header(1))]

    for kwargs in (
        {"limit": 0},
        {"limit": 1, "after": "raw tenant"},
        {"limit": 1, "namespace": "foreign"},
        {"limit": 1, "only": "name"},
    ):
        with pytest.raises(ValueError):
            await candidate_page(
                **{"after": "", **kwargs}, query_limits=QueryLimits(10, 100, 1), ch=Store()
            )
    with pytest.raises(ValueError, match="page_order"):
        await candidate_page("", 2, query_limits=QueryLimits(10, 100, 1), ch=Store())


@pytest.mark.asyncio
async def test_scan_window_and_restart_use_supervisor_cursor(cursors, monkeypatch):
    monkeypatch.setattr(schedule.pass_context, "resume_cursor", lambda: "mcp1:" + f"{2:064X}")
    seen = []

    async def execute(row, budget):
        seen.append(row.name)
        budget.left -= 1

    metrics = await run(
        [header(i) for i in range(1, 20)],
        execute,
        limits=replace(LIMITS, page_size=2, scan_pages=1),
    )
    assert seen == ["3", "4"] and metrics.pages == 1 and metrics.scanned == 2
    assert cursors[-1] == "mcp1:" + f"{4:064X}"


def test_limits_have_no_implicit_unbounded_mode():
    for field in (
        "page_size",
        "scan_pages",
        "ready",
        "global_inflight",
        "per_org",
        "per_endpoint",
        "documents",
        "source_seconds",
    ):
        with pytest.raises(ValueError):
            replace(LIMITS, **{field: 0})


@pytest.mark.asyncio
async def test_small_pass_budget_does_not_skip_ready_tails(cursors, monkeypatch):
    rows = [header(i, str(i % 3)) for i in range(1, 13)]
    seen: set[str] = set()

    async def execute(row, budget):
        seen.add(row.name)
        budget.left -= 1

    for _ in range(12):
        resume = cursors[-1] if cursors else ""
        monkeypatch.setattr(schedule.pass_context, "resume_cursor", lambda value=resume: value)
        await run(rows, execute, parent=Budget(1))
    assert seen == {str(i) for i in range(1, 13)}


@pytest.mark.asyncio
async def test_task_context_keeps_private_namespace_out_of_global_status(cursors):
    owner = schedule.pass_context.begin("knowledge_source_sync")

    async def execute(row, budget):
        child = schedule.pass_context.current()
        assert child is not owner and child.stopped is owner.stopped
        schedule.pass_context.set_namespace("private-namespace")
        budget.left -= 1

    try:
        await run([header(1)], execute)
        assert owner.namespace == ""
        assert schedule.pass_context.current() is owner
    finally:
        schedule.pass_context.clear()


@pytest.mark.asyncio
async def test_durable_wrapper_preserves_recording_cursor(cursors, monkeypatch):
    persisted = {}
    seen = []

    async def resume(worker, *, timeout_s):
        assert worker == schedule.PROGRESS_WORKER and timeout_s == 0.25
        return persisted.get(worker, "")

    async def record(worker, **fields):
        assert fields["namespace"] == ""
        persisted[worker] = fields["resume_cursor"]
        return {"resume_cursor": fields["resume_cursor"]}

    async def execute(row, budget):
        seen.append(row.name)
        budget.left -= 1

    monkeypatch.setattr(schedule.status_store, "resume_cursor", resume)
    monkeypatch.setattr(schedule.status_store, "record_manual_pass", record)
    owner = schedule.pass_context.begin("knowledge_source_sync")
    owner.cursor = "recording-original"
    try:
        for _ in range(2):
            await schedule.run_pass(
                limits=LIMITS,
                parent=Budget(1),
                make_pass=Budget,
                execute=execute,
                endpoint_key=lambda row: row.name,
                deadline=time.monotonic() + 5,
                page=page_of([header(i) for i in range(1, 5)]),
                progress_timeout=0.25,
            )
        assert seen == ["1", "2"]
        assert owner.cursor == "recording-original"
        assert persisted[schedule.PROGRESS_WORKER] == "mcp1:" + f"{2:064X}"
    finally:
        schedule.pass_context.clear()


@pytest.mark.asyncio
async def test_shared_child_counter_is_rejected_before_parallel_use(cursors):
    shared, parent = Budget(2), Budget(10)

    async def execute(row, budget):
        pytest.fail("aliased budgets must never execute")

    with pytest.raises(ValueError, match="mcp_schedule_budget"):
        await schedule.dispatch(
            limits=LIMITS,
            parent=parent,
            make_pass=lambda credits: shared,
            execute=execute,
            endpoint_key=lambda row: row.name,
            deadline=time.monotonic() + 5,
            page=page_of([header(1), header(2)]),
            resume_cursor="",
            checkpoint=cursors.append,
        )
    assert parent.left == 10


@pytest.mark.asyncio
async def test_source_timeout_frees_capacity_for_healthy_work(cursors):
    closed = asyncio.Event()

    async def execute(row, budget):
        if row.name == "1":
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        else:
            budget.left -= 1

    metrics = await run(
        [header(1), header(2, "healthy")],
        execute,
        limits=replace(LIMITS, global_inflight=1, source_seconds=0.001),
    )
    assert closed.is_set()
    assert metrics.timed_out == 1 and metrics.completed == 1


@pytest.mark.asyncio
async def test_metadata_page_cannot_outlive_the_parent_pass(cursors):
    cancelled = False
    parent = Budget(4)

    async def page(after, size):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    async def execute(row, budget):
        pytest.fail("a hanging metadata page cannot admit a source")

    async with asyncio.timeout(0.3):
        measured = await schedule.dispatch(
            limits=LIMITS,
            parent=parent,
            make_pass=Budget,
            execute=execute,
            endpoint_key=lambda row: row.name,
            deadline=time.monotonic() + 0.02,
            page=page,
            resume_cursor="",
            checkpoint=cursors.append,
        )
    assert measured.deadline_cut == 1 and measured.started == 0
    assert cancelled and parent.left == 4
