"""code.scip_ingest / code.scip_status / code.refresh handlers — the
surfaces CI pushes through: compiler-tier deltas (scip_ingest) and the
merge trigger (refresh) that tells loom "new code, index it now"."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from server.code.api.models_ingest import (
    CodeRefreshInput,
    CodeRefreshOutput,
    CodeScipIngestInput,
    CodeScipIngestOutput,
    CodeScipStatusInput,
    CodeScipStatusOutput,
)
from server.code.app.common import invalidate_index_as_of, require_code_intel, resolve_code_identity
from server.code.app.spans import set_code_span_attributes
from server.code.domain.index.blame_range import sha_text
from server.code.store.index import CodeRepo
from server.core import leases
from server.core.background import TaskSet
from server.core.errors import LoomServerError, NotFoundError, ValidationError, stored_caller_text
from server.settings import settings

logger = logging.getLogger(__name__)


_REFRESH_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_REFRESH_TASKS = TaskSet()
# A refresh holds the namespace's lease while it runs, so CI calls that land on
# different replicas run one pass, not one each; a dead holder's lease expires.
_LEASE_TTL_S = 120  # not a setting: the lease takeover protocol between replicas


async def _take_lease(org: str, ns: str) -> leases.Lease | None:
    """The namespace's deployment-wide refresh lease; None when ClickHouse cannot be
    asked (the process lock alone). LeaseBusy when another process holds it."""
    name = f"code_refresh:{quote(org, safe='')}/{quote(ns, safe='')}"
    try:
        lease = await leases.take(name, ttl_s=_LEASE_TTL_S)
    except leases.LeaseUnavailable as e:
        logger.warning("code.refresh lease %s: %s", name, e.__cause__ or e)
        return None
    if lease is None:
        raise leases.LeaseBusy(name)
    return lease


async def handle_code_refresh(input: CodeRefreshInput) -> CodeRefreshOutput:
    """CI push-trigger: 'new code just merged — index it NOW'.

    The per-merge poller (code_ingest worker) is the fallback cadence; this
    op is the fast path a merge-queue CI job calls right after pushing, so
    agents see the new code in seconds instead of waiting out the poll
    interval. Runs the SAME ingest pass the worker runs (same watermark,
    same idempotent upserts), scoped to the addressed namespace, under the
    namespace's lease across every replica — concurrent triggers for one
    namespace coalesce wherever they land, and a trigger racing the poller is
    harmless. A mirror this host lacks is cloned in the background, not in the pass.

    Only a CONFIGURED namespace can be refreshed: the addressed identity
    must match a repo entry (scalar config or LOOM_CODE_REPOS_JSON), else
    404. Branch choice lives in that config — CI can fire for any push,
    but only branches an operator configured ever get indexed.

    Works even when the poller worker is disabled, so a deployment can be
    trigger-only (LOOM_CODE_INGEST_ENABLED=false + CI calls after merge).
    """
    from server.code.app.index.ingest_pass import run_pass
    from server.code.domain.index.repos import repo_for_namespace

    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)
    if repo_for_namespace(org, ns) is None:
        raise NotFoundError(f"namespace {org}/{ns} is not configured for code indexing")
    repo = CodeRepo()
    head_before = await repo.get_state(org, ns, "git_head")
    # Durable "CI reached us" breadcrumb. The poller keeps the index fresh
    # on its own, so a hook that never fires is invisible in the watermarks;
    # the dashboard's Health view reads this key to show "last CI refresh".
    await repo.set_state(
        org,
        ns,
        "last_refresh_at",
        datetime.now(UTC).isoformat(timespec="seconds"),
        detail=" ".join(
            f"{k}={v}"
            for k, v in (("agent", input.agent), ("expected_head", input.expected_head[:12]))
            if v
        ),
    )

    lock = _REFRESH_LOCKS.setdefault((org, ns), asyncio.Lock())
    try:
        # A fast answer for a pass already running here, not exclusion: two triggers
        # can both get past it; the lease, or without one the lock, orders them.
        if lock.locked():
            raise leases.LeaseBusy(f"code_refresh:{org}/{ns}")
        lease = await _take_lease(org, ns)
    except leases.LeaseBusy:
        return CodeRefreshOutput(
            triggered=False,
            already_running=True,
            head_before=head_before,
        )

    async def _pass() -> None:
        result = await run_pass(org, ns)
        # The freshness envelope (as_of, 30 s cache) must not lag its own refresh.
        invalidate_index_as_of(org, ns)
        logger.info(
            "code.refresh(%s/%s): ingested %d commit(s), head %s",
            org,
            ns,
            result.commits,
            result.head[:12],
        )

    async def _one_pass() -> None:
        # Renewed (and released) from before the lock wait, so a pass cancelled
        # while it waits gives the lease up too.
        held: contextlib.AbstractAsyncContextManager[object] = (
            lease.renewed() if lease is not None else contextlib.nullcontext()
        )
        try:
            async with held, lock:
                await _pass()
        except leases.LeaseLost:
            logger.warning("code.refresh(%s/%s): lease lost; the pass stopped", org, ns)
        except Exception:
            logger.exception("code.refresh: ingest pass failed")

    if input.wait:
        await asyncio.wait_for(_one_pass(), timeout=input.wait_timeout_seconds)
        head_after = await repo.get_state(org, ns, "git_head")
        return CodeRefreshOutput(
            triggered=True,
            already_running=False,
            head_before=head_before,
            head_after=head_after,
        )

    _REFRESH_TASKS.spawn(_one_pass(), name="code-refresh")
    return CodeRefreshOutput(
        triggered=True,
        already_running=False,
        head_before=head_before,
    )


class EntryLeaseNotHeld(LoomServerError):
    """The pass that built this upload no longer holds its entry's lease:
    another pass may be indexing the entry, so its batch must not complete."""

    code = "ENTRY_LEASE_NOT_HELD"
    status_code = 409


async def _require_entry_lease(org: str, ns: str, holder: str) -> None:
    from server.code.app.index.entry_lock import entry_lease_name

    name = entry_lease_name(org, ns)
    if not await leases.held_by(name, holder):
        raise EntryLeaseNotHeld(f"{holder} no longer holds {name}; the upload is refused")


async def handle_code_scip_ingest(input: CodeScipIngestInput) -> CodeScipIngestOutput:
    """Accept one part of a SCIP index upload (compiler-tier symbols and references) for a batch."""
    require_code_intel()
    org, ns = resolve_code_identity(input.org, input.namespace)

    payload_bytes = len(input.payload.encode("utf-8"))
    if payload_bytes > settings.code_scip_max_part_bytes:
        raise ValidationError(
            f"payload part is {payload_bytes} bytes; max {settings.code_scip_max_part_bytes}"
        )
    if input.total_parts > settings.code_scip_max_parts:
        raise ValidationError(
            f"total_parts {input.total_parts} exceeds max {settings.code_scip_max_parts}"
        )
    if not (0 <= input.seq < input.total_parts):
        raise ValidationError(f"seq {input.seq} outside [0, {input.total_parts})")

    if input.entry_lease_holder:
        await _require_entry_lease(org, ns, input.entry_lease_holder)

    repo = CodeRepo()
    payload_hash = hashlib.sha256(input.payload.encode("utf-8")).hexdigest()
    await repo.insert_scip_part(org, ns, input.batch_id, input.seq, input.payload, payload_hash)
    # Upsert the batch bookkeeping row (idempotent re-uploads refresh it).
    existing = await repo.get_scip_batch(org, ns, input.batch_id)
    if existing is None or str(existing.get("status")) == "pending":
        await repo.upsert_scip_batch(
            org,
            ns,
            input.batch_id,
            commit_sha=input.commit_sha,
            base_sha=input.base_sha,
            total_parts=input.total_parts,
            status="pending",
            producer=input.producer,
            created_at=existing.get("created_at") if existing else None,
        )
    received = await repo.scip_parts_received(org, ns, input.batch_id)
    return CodeScipIngestOutput(
        batch_id=input.batch_id,
        parts_received=received,
        total_parts=input.total_parts,
        complete=received >= input.total_parts,
    )


async def handle_code_scip_status(input: CodeScipStatusInput) -> CodeScipStatusOutput:
    """Report the progress of a SCIP upload batch: parts received and apply status."""
    require_code_intel()
    _require_uuid(input.batch_id)
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    batch = await repo.get_scip_batch(org, ns, input.batch_id)
    if batch is None:
        raise NotFoundError(f"scip batch not found: {input.batch_id}")
    received = await repo.scip_parts_received(org, ns, input.batch_id)
    set_code_span_attributes(route=str(batch["status"]), results=received)
    return CodeScipStatusOutput(
        batch_id=input.batch_id,
        status=str(batch["status"]),
        parts_received=received,
        total_parts=int(batch["total_parts"]),
        # A stored error can carry the driver's text (a statement, an address): the kind.
        error=_shown_batch_error(str(batch.get("error") or "")),
        commit_sha=sha_text(batch.get("commit_sha")),
        applied_at=str(batch.get("applied_at") or ""),
    )


def _shown_batch_error(stored: str) -> str:
    """A failed batch's `error` as `code.scip_status` returns it: `caller_text`'s shape,
    or a fixed sentence for text in no such shape."""
    return stored_caller_text(stored) or (
        "the apply failed; the error is in the server log" if stored else ""
    )


def _require_uuid(batch_id: str) -> None:
    """Reject a batch_id that is not a UUID with a 422.

    loom.code_scip_batches keys on a UUID column, so an unparsable id would otherwise reach
    ClickHouse and fail there as a 500 (code 376, CANNOT_PARSE_UUID); it is the caller's error.
    """
    try:
        uuid.UUID(batch_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError(f"batch_id is not a UUID: {batch_id!r}") from exc
