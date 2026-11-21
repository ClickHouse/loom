"""Bounded deletion of stopped sources' owned copies, independent of content sync."""

from __future__ import annotations

import dataclasses
import json
import time

from server.code.app.index.pass_budget import PassBudget
from server.core import leases
from server.core.ch import sequential_reads
from server.knowledge.app import private_sync, source_sync
from server.knowledge.app import sources as source_ops
from server.knowledge.domain.sources import (
    SYNC_WORKER_ACTOR,
    home,
    mirror_uri_suffix,
    turn_lease_name,
)
from server.knowledge.store import documents
from server.knowledge.store import source_retirements as queue
from server.knowledge.store import sources as store
from server.llm import customer
from server.memory.domain.provenance import pipeline_actor
from server.settings import settings
from server.workers import pass_context
from server.workers.status import set_worker_identity

WORKER = "knowledge_source_retire"
MAX_PASS_SECONDS = 60.0  # not a setting: cleanup has its own bounded turn


def _after() -> tuple[str, str]:
    raw = pass_context.resume_cursor()
    if not raw:
        return "", ""
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, str) for v in value):
        raise ValueError("SYNC_FAILED")
    return value[0], value[1]


def _failure() -> None:
    pass_context.note_failure(RuntimeError("SYNC_FAILED"), "source retirement")


async def run_once() -> None:
    with private_sync.scope():
        await _run_once()


async def _run_once() -> None:
    started = time.monotonic()
    run = source_sync.Pass(
        PassBudget(started, started + MAX_PASS_SECONDS * 0.75),
        int(settings.knowledge_source_retire_documents_per_pass),
    )
    page = await queue.page(_after())
    if not page:
        pass_context.set_cursor(json.dumps(["", ""]))
        pass_context.mark_idle()
        return
    for row in page:
        if run.spent():
            break
        # Registration metadata only: no mirror name goes into worker status.
        pass_context.set_cursor(json.dumps([row["state_key"], row["org_cursor"]]))
        try:
            item = queue.from_row(row)
            async with leases.cluster_lease(
                turn_lease_name(item.org, item.namespace, item.name), ttl_s=120.0
            ):
                with (
                    customer.for_org(item.org, namespace=item.mirror),
                    pipeline_actor(SYNC_WORKER_ACTOR),
                ):
                    set_worker_identity(WORKER, org=item.org, namespace=item.mirror)
                    await _retire(item, run)
        except leases.LeaseBusy:
            continue
        except Exception:  # noqa: BLE001 — one queued source must not stop the others
            _failure()
    else:
        if len(page) < queue.PAGE_SIZE:
            pass_context.set_cursor(json.dumps(["", ""]))


async def _retire(item: queue.Retirement, run: source_sync.Pass) -> None:
    async with source_ops.sources_lease(item.org, item.namespace):
        with sequential_reads():
            await source_ops._require_no_erasure(item.org, item.namespace)
            if not await queue.pending(item):
                return
            current = await store.latest_source(item.org, item.namespace, item.name)
        if current is not None and current.source_id == item.source_id and current.active:
            return  # a crash after enqueue but before inactivation cannot delete live content
    src = item.source()
    for doc in await store.source_documents(item.org, *home(src)):
        if run.spent():
            return
        pass_context.check_stop()
        run.left -= 1
        await source_sync.retire(src, str(doc["source_uri"]))
    await _finish(item)


async def _finish(item: queue.Retirement) -> None:
    async with source_ops.sources_lease(item.org, item.namespace):
        with sequential_reads():
            await source_ops._require_no_erasure(item.org, item.namespace)
            if not await queue.pending(item):
                return
            current = await store.latest_source(item.org, item.namespace, item.name)
            if current is not None and current.source_id == item.source_id and current.active:
                return
            if await store.count_documents(item.org, *home(item.source())):
                return
            if await documents.publications_owed(
                item.org, item.mirror, mirror_uri_suffix(item.source_id)
            ):
                _failure()
                return
        if current is not None and current.source_id == item.source_id and current.pending_delete:
            await store.insert_source(dataclasses.replace(current, pending_delete=False))
        await queue.write(item, pending=False)
