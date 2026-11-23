"""Bind bounded MCP admission to the existing leased document-source engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
from functools import partial
from typing import TYPE_CHECKING

import httpx

from server.knowledge.app import mcp_source_schedule as scheduler
from server.knowledge.app.mcp_profiles import load_mcp_profile_catalog
from server.knowledge.store import mcp_source_schedule as headers
from server.knowledge.store import sources as store
from server.settings import settings

if TYPE_CHECKING:
    from server.code.app.index.pass_budget import PassBudget
    from server.knowledge.app.source_sync import PassResult


def limits() -> scheduler.Limits:
    return scheduler.Limits(
        page_size=settings.knowledge_source_mcp_schedule_page_size,
        scan_pages=settings.knowledge_source_mcp_schedule_scan_pages,
        ready=settings.knowledge_source_mcp_schedule_ready,
        global_inflight=settings.knowledge_source_mcp_schedule_global,
        per_org=settings.knowledge_source_mcp_schedule_per_org,
        per_endpoint=settings.knowledge_source_mcp_schedule_per_endpoint,
        documents=settings.knowledge_source_mcp_schedule_documents,
        source_seconds=settings.knowledge_source_mcp_schedule_source_seconds,
    )


def query_limits() -> headers.QueryLimits:
    return headers.QueryLimits(
        rows=settings.knowledge_source_mcp_schedule_query_rows,
        bytes=settings.knowledge_source_mcp_schedule_query_bytes,
        seconds=settings.knowledge_source_mcp_schedule_query_seconds,
    )


async def run(whole: PassBudget, result: PassResult, *, full: bool = False) -> None:
    from server.knowledge.app import source_sync, sources

    parent = source_sync.Pass(
        budget=whole, left=settings.knowledge_source_mirror_documents_per_pass
    )
    catalog = load_mcp_profile_catalog()
    if catalog.empty:
        return
    registry_lock = asyncio.Lock()
    registry_ready = False

    def endpoint_key(header: headers.Header) -> str:
        profile = catalog.get(header.org, header.profile_id)
        if profile.metadata.profile_sha256 != header.profile_sha256:
            raise ValueError("mcp_schedule_profile_changed")
        endpoint = str(httpx.URL(json.loads(profile.config_snapshot)["config"]["endpoint"]))
        return hashlib.sha256(endpoint.encode()).hexdigest()

    async def execute(header: headers.Header, child: source_sync.Pass) -> None:
        nonlocal registry_ready
        source = await store.get_source(header.org, header.namespace, header.name)
        if source is None or source.source_id != header.source_id:
            return
        usable = await source_sync._usable([source])
        if not usable:
            return
        async with registry_lock:
            if not registry_ready:
                await source_sync.require_source_registry()
                registry_ready = True
        if full:
            await sources.request_full_sync(source)
        state = (await source_sync._states([source])).get(source_sync._cursor_of(source), {})
        result.sources += 1
        await source_sync._sync_one(source, state, child, result)

    query = query_limits()
    measured = await scheduler.run_pass(
        limits=limits(),
        parent=parent,
        make_pass=lambda credits: source_sync.Pass(budget=whole, left=credits),
        execute=execute,
        endpoint_key=endpoint_key,
        deadline=whole.stop_after,
        page=partial(headers.candidate_page, query_limits=query),
        progress_timeout=query.seconds,
    )
    result.private_work |= measured.started > 0
    result.cut |= measured.deferred > 0 or measured.timed_out > 0 or bool(measured.deadline_cut)
