"""Opt-in query groups become private drafts; the supervisor persists source rotation."""

from __future__ import annotations

import time
import uuid
from typing import Any

from server.knowledge.app.gaps import erasure_since
from server.knowledge.store import query_history as repo
from server.knowledge.store.query_history_remote import read_groups
from server.llm import customer
from server.memory.app.definition_drafts import already_claimed, draft
from server.memory.app.definition_review import require_draft_fleet
from server.observability import cost_accumulator
from server.observability.budget import namespace_over_cap
from server.settings import settings
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.status import record_pass

_START_CURSOR = str(uuid.UUID(int=0))


def _cursor() -> str:
    try:
        return str(uuid.UUID(pass_context.resume_cursor()))
    except ValueError:
        return _START_CURSOR


async def run_pass() -> dict[str, int]:
    result = {"sources": 0, "attempted": 0}
    if not settings.knowledge_query_history_enabled:
        pass_context.mark_idle()
        return result
    await require_draft_fleet()
    deadline = time.monotonic() + settings.knowledge_query_history_max_pass_seconds / 2
    sources = await repo.active_after(_cursor(), settings.knowledge_query_history_max_sources)
    with record_pass("query_history") as rec:
        for source in sources:
            pass_context.check_stop()
            if time.monotonic() >= deadline:
                break
            pass_context.set_cursor(str(source["source_id"]))
            pass_context.set_namespace(f"{source['org']}/{source['namespace']}")
            try:
                result["attempted"] += await _source(source, deadline)
                result["sources"] += 1
            except customer.SwitchUnread:
                raise
            except customer.HOLDS:
                continue
            except Exception as e:  # noqa: BLE001 — one source must not stop the rotation
                pass_context.note_failure(e, "query-history source")
        rec.outcome = result
    if not sources:
        pass_context.set_cursor(_START_CURSOR)
        pass_context.mark_idle()
    return result


async def _source(source: dict[str, Any], deadline: float) -> int:
    org, namespace = source["org"], source["namespace"]
    read_at = int(time.time())

    async def valid() -> bool:
        if not settings.knowledge_query_history_enabled or await erasure_since(
            org, namespace, read_at, set()
        ):
            return False
        current = await repo.find(org, namespace, source["name"])
        return (
            current is not None
            and current["tombstoned_at"] is None
            and str(current["source_id"]) == str(source["source_id"])
        )

    if await namespace_over_cap(org, namespace, fail_open=False) or not await valid():
        return 0
    window_days = settings.knowledge_query_history_window_days
    groups = await read_groups(source)
    attempted = 0
    for group in groups:
        pass_context.check_stop()
        if attempted >= settings.knowledge_query_history_max_drafts or time.monotonic() >= deadline:
            break
        resource = f"{source['source_id']}/{group['query_hash']}/{group['template_hash']}"
        if await already_claimed(org, namespace, "query_history", resource):
            continue

        async def group_valid(users: int = int(group["users"])) -> bool:
            return (
                users >= settings.knowledge_query_history_min_users
                and window_days == settings.knowledge_query_history_window_days
                and await valid()
            )

        if not await group_valid():
            continue
        attempted += 1
        started = time.monotonic()
        cost_accumulator.begin_op()
        try:
            await draft(
                org,
                namespace,
                kind="query_history",
                resource_id=resource,
                evidence={"sql": group["sql"]},
                supporters=int(group["users"]),
                still_valid=group_valid,
            )
        except customer.HOLDS:
            raise
        except Exception as e:  # noqa: BLE001 — a failed claim never retries its model attempt
            pass_context.note_failure(e, "query-history draft")
        finally:
            emit_worker_cost_event_for(
                org=org, namespace=namespace, operation="worker.query_history", started=started
            )
    return attempted
