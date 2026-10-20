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

An event's id is derived from what it is (`event_id`), so a repeat of one
event to one webhook shares its id. An announcement never fails the write it
describes: the row is in, so an outbox insert that fails is logged and noted
as a degradation, and that event is not sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from server.core import webhooks as signed
from server.core.datetimes import naive_utc_now
from server.core.errors import ValidationError
from server.core.logging_config import request_id_var
from server.core.webhook_secrets import webhook_secret_org_prefix, webhook_secret_ref_ok
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
from server.workers import pass_context

log = logging.getLogger(__name__)

# Live webhooks a namespace may hold.
MAX_PER_NAMESPACE = 10

Hooks = dict[tuple[str, str], tuple[tuple[str, frozenset[str]], ...]]
# (org, namespace) → ((webhook_id, its event types), ...), this process's copy.
_hooks: Hooks = {}
_next_read = 0.0
_lock = asyncio.Lock()


# ---------------------------------------------------------------- URL rules


def url_schemes() -> tuple[str, ...]:
    return ("http", "https") if settings.memory_webhook_allow_http else ("https",)


async def url_refusal(url: str) -> str:
    """Why `url` may not receive memory webhooks ("" when it may): https only
    unless the deployment allows http, no user name or password, and a host
    that resolves to public addresses unless memory_webhook_private_hosts
    names it."""
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        return "the URL may not carry a user name or password"
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
        degradations.note("memory.webhook", "registry_unread", e)
        log.warning("memory webhooks: registry read failed (%s); serving the last copy", e)
        return
    hooks: dict[tuple[str, str], list[tuple[str, frozenset[str]]]] = {}
    for r in rows:
        key = (str(r["org"]), str(r["namespace"]))
        hooks.setdefault(key, []).append((str(r["webhook_id"]), frozenset(r["events"])))
    _hooks = {k: tuple(v) for k, v in hooks.items()}


def forget_registry() -> None:
    """Read the registry again on the next write, for a change made here."""
    global _next_read
    _next_read = 0.0


async def _fresh_hooks() -> Hooks:
    """This process's copy of the registry, read again once its interval is up."""
    if time.monotonic() >= _next_read:
        async with _lock:
            if time.monotonic() >= _next_read:
                await _read()
    return _hooks


async def subscribers(org: str, namespace: str, event: str) -> list[str]:
    """The ids of the namespace's webhooks that take `event`."""
    hooks = await _fresh_hooks()
    return [wid for wid, events in hooks.get((org, namespace), ()) if event in events]


async def namespaces_with_hooks(org: str) -> list[str]:
    """The org's namespaces that have a webhook."""
    return sorted(ns for o, ns in await _fresh_hooks() if o == org)


# ---------------------------------------------------------------- announcing


def event_id(webhook_id: str, event: str, change: Mapping[str, Any]) -> str:
    """One id per (webhook, event, memory id, version), or per (webhook, erasure)
    for `memory.erased`: a write path that announces the same version again
    (a delete_version check that runs twice) repeats the id, and the receiver
    drops the repeat."""
    what = change.get("erasure_id") or change.get("memory_id")
    seed = "|".join((webhook_id, event, str(what), str(change.get("version", 0))))
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _outbox_row(
    event: str, webhook_id: str, change: Mapping[str, Any], *, actor: str, now: datetime
) -> dict[str, Any]:
    return {
        "org": str(change["org"]),
        "namespace": str(change["namespace"]),
        "webhook_id": webhook_id,
        "event_id": event_id(webhook_id, event, change),
        "event_type": event,
        "agent": str(change["agent"]),
        "user_id": str(change["user_id"]),
        "session_id": str(change["session_id"]),
        "memory_key": str(change["memory_key"]),
        "memory_id": str(change["memory_id"]),
        "memory_version": int(change["version"]),
        "actor": actor,
        "request_id": request_id_var.get() or "",
        "erasure_id": str(change.get("erasure_id", "")),
        "occurred_at": now,
    }


def _actor() -> str:
    """Who made the change: the request's principal, else `worker:<name>` for a
    background pass (the expiry sweep, the delete_version check)."""
    ctx = pass_context.current()
    return provenance.writer_actor() or (f"worker:{ctx.worker}" if ctx else "")


async def announce(changes: Sequence[tuple[EventType, Mapping[str, Any]]]) -> None:
    """Leave one outbox row per (event, change) and webhook that takes the event,
    in one insert. A change is a mapping with the memory's org, namespace, agent,
    user_id, session_id, memory_key, memory_id and version (a memory version row
    is one). Never raises."""
    if not changes:
        return
    try:
        now = naive_utc_now()
        actor = _actor()
        rows = [
            _outbox_row(event, wid, c, actor=actor, now=now)
            for event, c in changes
            for wid in await subscribers(str(c["org"]), str(c["namespace"]), event)
        ]
        await store.insert_outbox(rows)
    except Exception as e:  # noqa: BLE001 — the write is in; its announcement must not fail it
        degradations.note("memory.webhook", "not_announced", e)
        log.warning("memory webhooks: %d changes not announced: %s", len(changes), e)


def memory_change(memory: Any, version: int) -> dict[str, Any]:
    """The change `announce` takes, for a memory row object at `version`."""
    return {
        "org": memory.org,
        "namespace": memory.namespace,
        "agent": memory.agent,
        "user_id": memory.user_id,
        "session_id": memory.session_id,
        "memory_key": memory.memory_key,
        "memory_id": memory.memory_id,
        "version": version,
    }


async def announce_erasure(org: str, namespaces: Sequence[str] | None, erasure_id: str) -> None:
    """`memory.erased` once a saga completed: to the webhooks of `namespaces`,
    or of every namespace of the org for a user's erasure (None). The event
    names the erasure, never the user or a key: the receiver asked for it and
    knows what it covered."""
    if namespaces is None:
        namespaces = await namespaces_with_hooks(org)
    no_memory = {
        "agent": "-",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "",
        "memory_id": "",
        "version": 0,
    }
    await announce(
        [
            ("memory.erased", {"org": org, "namespace": ns, **no_memory, "erasure_id": erasure_id})
            for ns in namespaces
        ]
    )


# ---------------------------------------------------------------- the ops


def _new_webhook_id(org: str, namespace: str, url: str, created_at: datetime) -> str:
    """A registration's own id: the same URL registered again after a remove
    gets a new one, so the removed registration's events stay its own."""
    seed = "|".join((org, namespace, url, created_at.isoformat()))
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _info(row: Mapping[str, Any]) -> WebhookInfo:
    ref = str(row["secret_ref"])
    return WebhookInfo(
        webhook_id=str(row["webhook_id"]),
        url=str(row["url"]),
        secret_ref=ref,
        events=list(row["events"]),
        created_by=str(row["created_by"]),
        created_at=row["created_at"],
        secret_present=signed.read_secret(str(row["org"]), ref, os.environ) is not None,
    )


async def handle_webhook_register(input: WebhookRegisterInput) -> WebhookRegisterOutput:
    if not webhook_secret_ref_ok(input.org, input.secret_ref):
        raise ValidationError(
            "secret_ref must be "
            f"{webhook_secret_org_prefix(input.org)}<letters and digits>, "
            "the name of an environment variable this org owns"
        )
    try:
        async with asyncio.timeout(signed.ACK_TIMEOUT_SECONDS):
            refusal = await url_refusal(input.url)
    except TimeoutError:
        refusal = "the host did not resolve in time"
    if refusal:
        raise ValidationError(f"url refused: {refusal}")
    existing = await store.namespace_webhooks(input.org, input.namespace)
    # Read, then written: two registrations of one new URL at once can both pass, and
    # make two webhooks (one more than the cap at most); webhook.remove takes either.
    same_url = next((r for r in existing if r["url"] == input.url), None)
    if same_url is None and len(existing) >= MAX_PER_NAMESPACE:
        raise ValidationError(
            f"a namespace holds at most {MAX_PER_NAMESPACE} webhooks; remove one first"
        )
    now = naive_utc_now()
    # The URL registered again keeps its id and replaces its secret_ref and events.
    kept = same_url or {
        "webhook_id": _new_webhook_id(input.org, input.namespace, input.url, now),
        "created_by": provenance.writer_actor(),
        "created_at": now,
    }
    row = {
        "org": input.org,
        "namespace": input.namespace,
        "webhook_id": kept["webhook_id"],
        "url": input.url,
        "secret_ref": input.secret_ref,
        "events": sorted(set(input.events)),
        "created_by": kept["created_by"],
        "created_at": kept["created_at"],
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
        since=naive_utc_now() - timedelta(days=store.KEEP_DAYS),
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
                status=r["status"],
                attempts=int(r["attempts"]),
                next_attempt_at=r["next_attempt_at"],
                last_status=int(r["last_status"]),
                last_error=str(r["last_error"]),
            )
            for r in rows
        ]
    )
