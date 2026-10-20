"""Memory webhooks: the admin ops, and the outbox rows the write paths leave.

A write path calls `announce` after its rows are in. For every webhook of the
row's namespace that takes the event, it inserts one outbox row — ids and the
identity tuple, never content — and the memory_webhook_deliver worker POSTs it
later (server/memory/workers/webhook_deliver.py). No network call is made on
the request path. A namespace with no webhook pays no outbox insert; the only
cost is the registry read below, once per process per refresh interval, which
the first write after the interval waits for.

Which namespaces have webhooks is a per-process copy of loom.memory_webhooks,
read again every `memory_webhook_refresh_seconds`: a webhook registered on
another process applies to this one's writes within that interval. A
register or remove here reads it again on the next write.

An announcement never fails the write it describes: the row is in, so an
outbox insert that fails is logged and noted as a degradation, and that event
is not sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core import webhooks as signed
from server.core.errors import ValidationError
from server.core.logging_config import request_id_var
from server.memory.api.models_webhook import (
    WebhookDeliveriesInput,
    WebhookDeliveriesOutput,
    WebhookDelivery,
    WebhookInfo,
    WebhookListInput,
    WebhookListOutput,
    WebhookRegisterInput,
    WebhookRegisterOutput,
    WebhookRemoveInput,
    WebhookRemoveOutput,
)
from server.memory.app import provenance
from server.memory.domain.webhook_events import EventType
from server.memory.store import webhooks as store
from server.memory.store.rows import next_version
from server.observability import degradations
from server.settings import settings

log = logging.getLogger(__name__)

# Live webhooks a namespace may hold.
MAX_PER_NAMESPACE = 10

# (org, namespace) → ((webhook_id, its event types), ...), this process's copy.
_hooks: dict[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]] = {}
_next_read = 0.0
_lock = asyncio.Lock()


# ---------------------------------------------------------------- URL rules


def url_schemes() -> tuple[str, ...]:
    return ("http", "https") if settings.memory_webhook_allow_http else ("https",)


async def url_refusal(url: str) -> str:
    """Why `url` may not receive memory webhooks ("" when it may): https only
    unless the deployment allows http, and a host that resolves to public
    addresses unless memory_webhook_private_hosts names it."""
    refusal, *_ = await signed.pinned_target(
        url, schemes=url_schemes(), private_hosts=settings.memory_webhook_private_host_set()
    )
    return refusal


# ---------------------------------------------------------------- the registry copy


async def _read() -> None:
    global _hooks, _next_read
    _next_read = time.monotonic() + settings.memory_webhook_refresh_seconds
    try:
        rows = await store.live_webhooks()
    except Exception as e:  # noqa: BLE001 — the last copy serves until a read lands
        log.warning("memory webhooks: registry read failed (%s); serving the last copy", e)
        return
    hooks: dict[tuple[str, str], list[tuple[str, frozenset[str]]]] = {}
    for r in rows:
        key = (str(r["org"]), str(r["namespace"]))
        hooks.setdefault(key, []).append((str(r["webhook_id"]), frozenset(r["events"] or ())))
    _hooks = {k: tuple(v) for k, v in hooks.items()}


def forget_registry() -> None:
    """Read the registry again on the next write, for a change made here."""
    global _next_read
    _next_read = 0.0


async def subscribers(org: str, namespace: str, event: str) -> list[str]:
    """The ids of the namespace's webhooks that take `event`."""
    if time.monotonic() >= _next_read:
        async with _lock:
            if time.monotonic() >= _next_read:
                await _read()
    return [wid for wid, events in _hooks.get((org, namespace), ()) if event in events]


# ---------------------------------------------------------------- announcing


def _outbox_row(
    event: str, webhook_id: str, change: Mapping[str, Any], *, actor: str, now: datetime
) -> dict[str, Any]:
    return {
        "org": str(change["org"]),
        "namespace": str(change["namespace"]),
        "webhook_id": webhook_id,
        "event_id": str(uuid.uuid4()),
        "event_type": event,
        "agent": str(change.get("agent") or "-"),
        "user_id": str(change.get("user_id") or "-"),
        "session_id": str(change.get("session_id") or "-"),
        "memory_key": str(change.get("memory_key") or ""),
        "memory_id": str(change.get("memory_id") or ""),
        "memory_version": int(change.get("version") or 0),
        "actor": actor,
        "request_id": request_id_var.get() or "",
        "erasure_id": str(change.get("erasure_id") or ""),
        "occurred_at": now,
    }


async def announce(event: EventType, changes: Sequence[Mapping[str, Any]]) -> None:
    """Leave one outbox row per (change, webhook that takes `event`). A change is
    a mapping with the memory's org, namespace, agent, user_id, session_id,
    memory_key, memory_id and version (a memory version row is one). Never
    raises."""
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        actor = provenance.writer_actor()
        rows = [
            _outbox_row(event, wid, c, actor=actor or str(c.get("actor") or ""), now=now)
            for c in changes
            for wid in await subscribers(str(c["org"]), str(c["namespace"]), event)
        ]
        await store.insert_outbox(rows)
    except Exception as e:  # noqa: BLE001 — the write is in; its announcement must not fail it
        degradations.note("memory.webhook", "not_announced", e)
        log.warning("memory webhooks: %s not announced: %s", event, e)


def memory_change(memory: Any, version: int) -> dict[str, Any]:
    """The change `announce` takes, for a memory row object at `version`."""
    fields = ("org", "namespace", "agent", "user_id", "session_id", "memory_key", "memory_id")
    return {**{f: getattr(memory, f, "") for f in fields}, "version": version}


async def announce_erasure(org: str, namespaces: Sequence[str] | None, erasure_id: str) -> None:
    """`memory.erased` once a saga completed: to the webhooks of `namespaces`,
    or of every namespace of the org for a user's erasure (None). The event
    names the erasure, never the user or a key: the receiver asked for it and
    knows what it covered."""
    if namespaces is None:
        await subscribers(org, "", "memory.erased")  # refresh the copy when due
        namespaces = sorted(ns for o, ns in _hooks if o == org)
    await announce(
        "memory.erased",
        [{"org": org, "namespace": ns, "erasure_id": erasure_id} for ns in namespaces],
    )


# ---------------------------------------------------------------- the ops


def _webhook_id(org: str, namespace: str, url: str) -> str:
    """One registration per URL in a namespace: registering it again replaces it."""
    return hashlib.sha256(f"{org}\0{namespace}\0{url}".encode()).hexdigest()[:16]


def _info(row: Mapping[str, Any]) -> WebhookInfo:
    ref = str(row["secret_ref"])
    return WebhookInfo(
        webhook_id=str(row["webhook_id"]),
        url=str(row["url"]),
        secret_ref=ref,
        events=[str(e) for e in row["events"]],
        created_by=str(row.get("created_by") or ""),
        created_at=row.get("created_at"),
        secret_present=signed.read_secret(str(row["org"]), ref, os.environ) is not None,
    )


async def handle_webhook_register(input: WebhookRegisterInput) -> WebhookRegisterOutput:
    if not signed.webhook_secret_ref_ok(input.org, input.secret_ref):
        raise ValidationError(
            "secret_ref must be "
            f"{signed.webhook_secret_org_prefix(input.org)}<letters and digits>, "
            "the name of an environment variable this org owns"
        )
    refusal = await url_refusal(input.url)
    if refusal:
        raise ValidationError(f"url refused: {refusal}")
    webhook_id = _webhook_id(input.org, input.namespace, input.url)
    existing = await store.namespace_webhooks(input.org, input.namespace)
    if webhook_id not in {r["webhook_id"] for r in existing} and (
        len(existing) >= MAX_PER_NAMESPACE
    ):
        raise ValidationError(
            f"a namespace holds at most {MAX_PER_NAMESPACE} webhooks; remove one first"
        )
    row = {
        "org": input.org,
        "namespace": input.namespace,
        "webhook_id": webhook_id,
        "url": input.url,
        "secret_ref": input.secret_ref,
        "events": sorted(set(input.events)),
        "created_by": provenance.writer_actor(),
        "created_at": datetime.now(UTC).replace(tzinfo=None),
        "is_deleted": 0,
        "version": next_version(),
    }
    await store.insert_webhook(row)
    forget_registry()
    return WebhookRegisterOutput(webhook=_info(row))


async def handle_webhook_list(input: WebhookListInput) -> WebhookListOutput:
    rows = await store.namespace_webhooks(input.org, input.namespace)
    return WebhookListOutput(webhooks=[_info(r) for r in rows])


async def handle_webhook_remove(input: WebhookRemoveInput) -> WebhookRemoveOutput:
    rows = await store.namespace_webhooks(input.org, input.namespace)
    row = next((r for r in rows if r["webhook_id"] == input.webhook_id), None)
    if row is None:
        return WebhookRemoveOutput(removed=False)
    await store.insert_webhook({**row, "is_deleted": 1, "version": next_version()})
    forget_registry()
    return WebhookRemoveOutput(removed=True)


async def handle_webhook_deliveries(input: WebhookDeliveriesInput) -> WebhookDeliveriesOutput:
    rows = await store.namespace_deliveries(
        input.org,
        input.namespace,
        since=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=store.KEEP_DAYS),
        webhook_id=input.webhook_id,
        status=input.status or "",
        limit=input.limit,
    )
    return WebhookDeliveriesOutput(
        deliveries=[
            WebhookDelivery(
                event_id=str(r["event_id"]),
                webhook_id=str(r["webhook_id"]),
                event_type=str(r["event_type"]),
                memory_key=str(r["memory_key"]),
                memory_id=str(r["memory_id"]),
                version=int(r["memory_version"]),
                occurred_at=r["occurred_at"],
                status=str(r["status"]),
                attempts=int(r["attempts"]),
                next_attempt_at=r["next_attempt_at"],
                last_status=int(r["last_status"]),
                last_error=str(r["last_error"]),
            )
            for r in rows
        ]
    )
