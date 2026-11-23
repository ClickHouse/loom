"""Frozen hosted source scheduling workload; never imported by local validation."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from scripts.ops.qualify_mcp_source_scale import accepted
from server.core.ch import get_client, set_query_tag
from server.knowledge.app import source_sync
from server.knowledge.app.mcp_source_schedule import PROGRESS_WORKER, run_pass
from server.knowledge.store.mcp_source_schedule import candidate_page
from server.settings import settings
from server.workers import status_store
from tests.integration.mcp_source_qualification_fixtures import qualification_app
from tests.integration.mcp_source_scale_fixtures import (
    ScaleWorld,
    catalog_latencies,
    operator_limits,
    query_limits,
    setup_world,
)
from tests.integration.mcp_source_scale_metrics import Sampler, percentiles, store_work

__all__ = ["qualification_app"]


async def source_copy_count(case: Any) -> int:
    set_query_tag("worker.knowledge_source_sync")
    row = await get_client().query_one(
        "SELECT count() AS n FROM loom.documents FINAL WHERE org = {org:String} "
        "AND namespace = {ns:String} AND source_uri = {uri:String}",
        params={"org": case.owner.org, "ns": (await case.source_row()).mirror, "uri": case.uri},
    )
    assert row is not None
    return int(row["n"])


async def cancellation_and_overlap(world: ScaleWorld) -> dict[str, float | int]:
    case = next(case for case in world.eligible.values() if case.source.role == "healthy")
    source = case.source
    assert await source_copy_count(case) == 0
    source.hold = True
    source.started.clear()
    source.release.clear()
    task = asyncio.create_task(case.sync())
    joined = 0.0
    try:
        async with asyncio.timeout(10):
            await source.started.wait()
        # Both turns use the actual source lease against the job's own Keeper.
        async with asyncio.timeout(5):
            overlap = await case.sync()
        assert overlap is None and not task.done()
        started = time.monotonic()
        task.cancel()
        async with asyncio.timeout(5):
            await asyncio.gather(task, return_exceptions=True)
        joined = time.monotonic() - started
        assert task.cancelled() and source.in_flight == 0
        assert await source_copy_count(case) == 0
    finally:
        source.hold = False
        source.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return {"cancel_join_seconds": joined, "overlap_fenced": 1}


async def scheduled_pass(world: ScaleWorld) -> tuple[dict[str, int], bool]:
    budget = source_sync._budget()
    parent = source_sync.Pass(budget, left=settings.knowledge_source_sync_documents_per_pass)
    metrics = await run_pass(
        limits=operator_limits(),
        parent=parent,
        make_pass=lambda credits: source_sync.Pass(budget, left=credits),
        execute=world.execute,
        endpoint_key=world.endpoint_key,
        deadline=min(budget.stop_after, world.started + 600),
        page=partial(candidate_page, query_limits=query_limits()),
        progress_timeout=query_limits().seconds,
    )
    observed = await status_store.resume_cursor(PROGRESS_WORKER)
    assert observed.startswith("mcp1:")
    return asdict(metrics), observed == "mcp1:" + "0" * 64


async def refresh_rounds(world: ScaleWorld, result: Any) -> dict[str, float | int]:
    world.started = time.monotonic()
    peaks = {
        key: 0
        for key in ("queue_highwater", "inflight_highwater", "org_highwater", "endpoint_highwater")
    }
    # This finite census bounds the number of pass restarts independently of time.
    passes = 2 * (len(world.eligible) + operator_limits().ready + 1)
    for _ in range(passes):
        set_query_tag("worker.knowledge_source_sync")
        if time.monotonic() - world.started >= 600:
            break
        metrics, wrapped = await scheduled_pass(world)
        world.failed_turns += metrics["failed"] + metrics["timed_out"]
        for key in peaks:
            peaks[key] = max(peaks[key], metrics[key])
        result.record(
            **peaks, healthy_completed=len(world.healthy_done), failed_turns=world.failed_turns
        )
        if wrapped:
            world.round += 1
            if len(world.attempted) == len(world.eligible) and len(world.healthy_done) == 200:
                break
        if world.round > 2:
            break
    elapsed = time.monotonic() - world.started
    assert len(world.attempted) == 1000
    return {
        **peaks,
        **percentiles(world.live_latencies, "live"),
        **percentiles(world.refresh_latencies, "refresh"),
        "healthy_completed": len(world.healthy_done),
        "refresh_all_seconds": elapsed,
        "healthy_tenants_completed": len(world.first_tenant),
        "healthy_tenant_first_max": max(value[0] for value in world.first_tenant.values()),
        "healthy_tenant_first_round_max": max(value[1] for value in world.first_tenant.values()),
        "throughput": len(world.healthy_done) / elapsed,
        "failed_turns": world.failed_turns,
        "restart_restored": 1,
        "original_readbacks": world.original_readbacks,
    }


async def persisted_copies(world: ScaleWorld) -> dict[str, int]:
    result: dict[str, int] = {}
    for table, key in (("documents", "document_count"), ("chunks", "chunk_count")):
        set_query_tag("worker.knowledge_source_sync")
        row = await get_client().query_one(
            f"SELECT count() AS n FROM loom.{table} FINAL WHERE org IN {{orgs:Array(String)}}",
            params={"orgs": world.orgs},
        )
        assert row is not None
        result[key] = int(row["n"])
    return result


async def workload(world: ScaleWorld, result: Any, app: Any, base: Path) -> None:
    for index, org in enumerate(world.orgs):
        for user in range(2):
            set_query_tag("worker.knowledge_source_sync")
            await world.seed_owner(app, base / str(index) / str(user), org, user)
    counts = await world.counts()
    result.record(
        **counts,
        healthy_eligible=200,
        healthy_tenants=10,
        endpoint_count=10,
        rate_limited_endpoints=2,
        denied_endpoints=2,
        malformed_endpoints=2,
        stalled_endpoints=2,
    )
    assert counts["registered"] == counts["registered_distinct"] == 10000
    assert counts["eligible"] == counts["eligible_distinct"] == len(world.eligible) == 1000
    result.record(**await cancellation_and_overlap(world))
    for source in world.sources:
        source.faults_enabled = True
    async with asyncio.TaskGroup() as group:
        refresh = group.create_task(refresh_rounds(world, result))
        catalog = group.create_task(catalog_latencies(world))
    result.record(**refresh.result(), **percentiles(catalog.result(), "catalog"))
    result.record(**await persisted_copies(world))


@pytest.mark.asyncio
async def test_source_scale_contract(qualification_app: Any, tmp_path: Path, request: Any) -> None:
    result = request.config._mcp_scale_result
    limits = operator_limits()
    assert (
        limits.page_size,
        limits.ready,
        limits.global_inflight,
        limits.per_org,
        limits.per_endpoint,
    ) == (100, 256, 16, 4, 2)
    assert limits.documents == 1 and limits.source_seconds == 5
    assert query_limits().rows == 1000000 and query_limits().bytes == 67108864
    started = datetime.now(UTC).isoformat()
    sampler, patch = Sampler(), pytest.MonkeyPatch()
    await sampler.start()
    # Prime the ordinary status writer before the ownership baseline; its lifecycle belongs to the app.
    now = datetime.now(UTC)
    await status_store.record_manual_pass(
        PROGRESS_WORKER,
        namespace="",
        status="ok",
        started=now,
        finished=now,
        resume_cursor="mcp1:" + "0" * 64,
    )
    baseline = set(asyncio.all_tasks())
    world: ScaleWorld | None = None
    teardown_started = 0.0
    try:
        async with AsyncExitStack() as stack:
            world = await setup_world(stack, patch)
            try:
                await workload(world, result, qualification_app, tmp_path)
            finally:
                teardown_started = time.monotonic()
                for source in world.sources:
                    source.release.set()
    finally:
        patch.undo()
        teardown = time.monotonic() - teardown_started if teardown_started else 0.0
        await sampler.close()
        residue = {task for task in asyncio.all_tasks() - baseline if not task.done()}
        result.record(
            **sampler.metrics(),
            teardown_seconds=teardown,
            owned_tasks_after_teardown=len(residue),
            source_inflight_after_teardown=sum(source.in_flight for source in world.sources)
            if world
            else 0,
        )
        result.record(**await store_work(started))
    assert accepted(result.metrics)
