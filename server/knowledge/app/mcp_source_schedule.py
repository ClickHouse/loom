"""Fair bounded admission around the existing leased MCP source engine."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from server.core.ch import set_query_tag
from server.knowledge.store.mcp_source_schedule import Header
from server.workers import pass_context, status_store

PROGRESS_WORKER = "knowledge_source_sync.mcp"


class Budget(Protocol):
    left: int


Page = Callable[[str, int], Awaitable[list[Header]]]


@dataclass(frozen=True)
class Limits:
    page_size: int
    scan_pages: int
    ready: int
    global_inflight: int
    per_org: int
    per_endpoint: int
    documents: int
    source_seconds: float

    def __post_init__(self) -> None:
        for value in (
            self.page_size,
            self.scan_pages,
            self.ready,
            self.global_inflight,
            self.per_org,
            self.per_endpoint,
            self.documents,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("mcp_schedule_limit")
        if not 0 < self.source_seconds < float("inf"):
            raise ValueError("mcp_schedule_limit")


@dataclass
class Metrics:
    pages: int = 0
    scanned: int = 0
    started: int = 0
    completed: int = 0
    failed: int = 0
    timed_out: int = 0
    rejected: int = 0
    deferred: int = 0
    queue_highwater: int = 0
    inflight_highwater: int = 0
    org_highwater: int = 0
    endpoint_highwater: int = 0
    wraps: int = 0
    deadline_cut: int = 0


def _resume(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"mcp1:[0-9A-F]{64}", value) is None:
        raise ValueError("mcp_schedule_cursor")
    return value[5:]


@dataclass
class _Job:
    header: Header
    endpoint: str
    budget: Budget
    reserved: int


class _Ready:
    def __init__(self) -> None:
        self.groups: dict[str, deque[tuple[Header, str]]] = {}
        self.order: deque[str] = deque()
        self.size = 0

    def append(self, header: Header, endpoint: str) -> None:
        if header.org not in self.groups:
            self.groups[header.org] = deque()
            self.order.append(header.org)
        self.groups[header.org].append((header, endpoint))
        self.size += 1

    def pop(
        self, orgs: Counter[str], endpoints: Counter[str], limits: Limits
    ) -> tuple[Header, str] | None:
        for _ in range(len(self.order)):
            org = self.order.popleft()
            group = self.groups[org]
            chosen = None
            if orgs[org] < limits.per_org:
                for _ in range(len(group)):
                    header, endpoint = group.popleft()
                    if endpoints[endpoint] < limits.per_endpoint:
                        chosen = header, endpoint
                        self.size -= 1
                        break
                    group.append((header, endpoint))
            if group:
                self.order.append(org)
            else:
                del self.groups[org]
            if chosen is not None:
                return chosen
        return None


async def _execute[B: Budget](
    execute: Callable[[Header, B], Awaitable[None]], header: Header, budget: B, seconds: float
) -> str:
    set_query_tag("worker.knowledge_source_sync")
    owner = pass_context.current()
    child_context = pass_context.begin(owner.worker if owner else "knowledge_source_sync")
    if owner is not None:
        child_context.stopped = owner.stopped
    try:
        async with asyncio.timeout(seconds):
            await execute(header, budget)
        return "completed"
    except TimeoutError:
        return "timed_out"
    except Exception:  # noqa: BLE001 — one source failure must not stop other tenants
        return "failed"
    finally:
        pass_context.clear()


def _refund(parent: Budget, job: _Job) -> None:
    if type(job.budget.left) is not int or not 0 <= job.budget.left <= job.reserved:
        raise ValueError("mcp_schedule_budget")
    parent.left += job.budget.left


class _Dispatch[B: Budget]:
    def __init__(
        self,
        limits: Limits,
        parent: Budget,
        make_pass: Callable[[int], B],
        execute: Callable[[Header, B], Awaitable[None]],
        endpoint_key: Callable[[Header], str],
        deadline: float,
        page: Page,
        resume_cursor: str,
        checkpoint: Callable[[str], None],
    ) -> None:
        self.limits, self.parent, self.make_pass = limits, parent, make_pass
        self.execute, self.endpoint_key, self.deadline, self.page = (
            execute,
            endpoint_key,
            deadline,
            page,
        )
        self.metrics, self.ready = Metrics(), _Ready()
        self.running: dict[asyncio.Task[str], _Job] = {}
        self.orgs: Counter[str] = Counter()
        self.endpoints: Counter[str] = Counter()
        self.after, self.exhausted = _resume(resume_cursor), False
        self.save_cursor = checkpoint
        self.pending: dict[str, str] = {}

    async def fill(self) -> None:
        size = min(self.limits.page_size, self.limits.ready - self.ready.size)
        async with asyncio.timeout_at(self.deadline):
            rows = await self.page(self.after, size)
        self.metrics.pages += 1
        if len(rows) > size:
            raise ValueError("mcp_schedule_page_overflow")
        for header in rows:
            if (
                re.fullmatch("[0-9A-F]{64}", header.schedule_key) is None
                or header.schedule_key <= self.after
            ):
                raise ValueError("mcp_schedule_page_order")
            previous, self.after = self.after, header.schedule_key
            self.metrics.scanned += 1
            try:
                endpoint = self.endpoint_key(header)
                if not endpoint:
                    raise ValueError("mcp_schedule_endpoint")
            except (ValueError, KeyError):
                self.metrics.rejected += 1
                continue
            self.ready.append(header, endpoint)
            self.pending[header.schedule_key] = previous
        self.metrics.queue_highwater = max(self.metrics.queue_highwater, self.ready.size)
        if len(rows) < size:
            self.exhausted = True
            self.metrics.wraps += 1
        self.checkpoint()

    def checkpoint(self) -> None:
        # Replay the first unstarted header rather than skipping a full ready window
        # whenever a small document budget ends a pass. Already started work is leased.
        cursor = self.pending[min(self.pending)] if self.pending else self.after
        if not self.pending and self.exhausted:
            cursor = ""
        self.save_cursor("mcp1:" + (cursor or "0" * 64))

    def start(self) -> None:
        while len(self.running) < self.limits.global_inflight and self.parent.left > 0:
            item = self.ready.pop(self.orgs, self.endpoints, self.limits)
            if item is None:
                break
            header, endpoint = item
            reserved = min(self.parent.left, self.limits.documents)
            child = self.make_pass(reserved)
            if (
                child is self.parent
                or type(child.left) is not int
                or child.left != reserved
                or any(child is job.budget for job in self.running.values())
            ):
                raise ValueError("mcp_schedule_budget")
            self.parent.left -= reserved
            seconds = min(self.limits.source_seconds, max(0.0, self.deadline - time.monotonic()))
            task = asyncio.create_task(_execute(self.execute, header, child, seconds))
            self.running[task] = _Job(header, endpoint, child, reserved)
            del self.pending[header.schedule_key]
            self.checkpoint()
            self.orgs[header.org] += 1
            self.endpoints[endpoint] += 1
            self.metrics.started += 1
            self.metrics.inflight_highwater = max(
                self.metrics.inflight_highwater, len(self.running)
            )
            self.metrics.org_highwater = max(self.metrics.org_highwater, self.orgs[header.org])
            self.metrics.endpoint_highwater = max(
                self.metrics.endpoint_highwater, self.endpoints[endpoint]
            )

    async def finish(self) -> None:
        async with asyncio.timeout_at(self.deadline):
            done, _ = await asyncio.wait(self.running, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            job = self.running.pop(task)
            self.orgs[job.header.org] -= 1
            self.endpoints[job.endpoint] -= 1
            _refund(self.parent, job)
            outcome = task.result()
            setattr(self.metrics, outcome, getattr(self.metrics, outcome) + 1)

    async def close(self) -> None:
        for task in self.running:
            task.cancel()
        if self.running:
            await asyncio.gather(*self.running, return_exceptions=True)
        invalid = False
        for job in self.running.values():
            try:
                _refund(self.parent, job)
            except ValueError:
                invalid = True
        self.metrics.deferred = self.ready.size
        if invalid:
            raise ValueError("mcp_schedule_budget")

    async def run(self) -> Metrics:
        try:
            while time.monotonic() < self.deadline:
                pass_context.check_stop()
                if (
                    not self.exhausted
                    and self.ready.size < self.limits.ready
                    and self.metrics.pages < self.limits.scan_pages
                ):
                    await self.fill()
                self.start()
                if not self.running:
                    if (
                        not self.parent.left
                        or (self.exhausted or self.metrics.pages >= self.limits.scan_pages)
                        and not self.ready.size
                    ):
                        break
                    continue
                await self.finish()
        finally:
            await self.close()
        return self.metrics


async def dispatch[B: Budget](
    *,
    limits: Limits,
    parent: Budget,
    make_pass: Callable[[int], B],
    execute: Callable[[Header, B], Awaitable[None]],
    endpoint_key: Callable[[Header], str],
    deadline: float,
    page: Page,
    resume_cursor: str,
    checkpoint: Callable[[str], None],
) -> Metrics:
    """Execute retains source/account leases and revalidates publication authority.

    make_pass returns an independent Pass.left with the parent's existing deadline.
    endpoint_key resolves the current approved profile without opening a session.
    checkpoint receives only an opaque cursor. run_pass persists it in worker_status.
    """
    if type(parent.left) is not int or parent.left < 0:
        raise ValueError("mcp_schedule_budget")
    set_query_tag("worker.knowledge_source_sync")
    state = _Dispatch(
        limits, parent, make_pass, execute, endpoint_key, deadline, page, resume_cursor, checkpoint
    )
    try:
        await state.run()
    except TimeoutError:
        if time.monotonic() < deadline:
            raise
        state.metrics.deadline_cut = 1
    else:
        state.metrics.deadline_cut = int(time.monotonic() >= deadline)
    return state.metrics


async def run_pass[B: Budget](
    *,
    limits: Limits,
    parent: Budget,
    make_pass: Callable[[int], B],
    execute: Callable[[Header, B], Awaitable[None]],
    endpoint_key: Callable[[Header], str],
    deadline: float,
    page: Page,
    progress_timeout: float,
) -> Metrics:
    """Use existing durable worker progress without replacing the recording lane cursor."""
    if not 0 < progress_timeout < float("inf"):
        raise ValueError("mcp_schedule_progress_bound")
    set_query_tag("worker.knowledge_source_sync")
    resumed = await status_store.resume_cursor(PROGRESS_WORKER, timeout_s=progress_timeout)
    cursor = resumed or "mcp1:" + "0" * 64

    def checkpoint(value: str) -> None:
        nonlocal cursor
        cursor = value

    started = datetime.now(UTC)
    status = "failed"
    try:
        result = await dispatch(
            limits=limits,
            parent=parent,
            make_pass=make_pass,
            execute=execute,
            endpoint_key=endpoint_key,
            deadline=deadline,
            page=page,
            resume_cursor=resumed,
            checkpoint=checkpoint,
        )
        status = "ok"
        return result
    finally:
        row = await status_store.record_manual_pass(
            PROGRESS_WORKER,
            namespace="",
            status=status,
            started=started,
            finished=datetime.now(UTC),
            timeout_s=progress_timeout,
            resume_cursor=cursor,
        )
        if row is None:
            raise RuntimeError("mcp_schedule_progress_unwritten")
        observed = await status_store.resume_cursor(PROGRESS_WORKER, timeout_s=progress_timeout)
        if observed != cursor:
            raise RuntimeError("mcp_schedule_progress_unwritten")
