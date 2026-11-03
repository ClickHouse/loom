"""Erasure plane handlers — the four `erasure.*` ops. They validate the
request and authorize the caller against its scope, then delegate: the saga
engine (steps, checkpoints, resume, residue census) lives in
`server.erasure.app.saga`; export, status and list read the erasure
store directly.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from server.auth import authz, sharing
from server.auth import context as auth_context
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.core.principals import personal_name
from server.erasure.api.erasure import (
    ErasureExportInput,
    ErasureExportOutput,
    ErasureExportSection,
    ErasureListInput,
    ErasureListOutput,
    ErasureListRow,
    ErasureRequestInput,
    ErasureRequestOutput,
    ErasureStatus,
    ErasureStatusInput,
    ErasureStatusOutput,
)
from server.erasure.app.saga import audit_namespace, begin_erasure, run_saga
from server.erasure.store import exports as erasure_exports
from server.erasure.store import ledger as erasure_ledger
from server.erasure.store.rows import RequestStatusRow
from server.memory.app.provenance import writer_actor
from server.memory.domain.sensitivity import reads_high
from server.observability import audit
from server.operations.base import Role
from server.settings import settings


def _org_wide_admin(auth: AuthInfo, org: str) -> bool:
    """The org-wide view of erasure: a flat ORG_ADMIN of `org` (or of every
    org) or a PLATFORM_ADMIN. Never a scoped token: its authority is its
    namespaces, so its flat roles confer nothing org-wide."""
    return not auth.has_ns_roles() and (
        auth.has_role(Role.PLATFORM_ADMIN)
        or (auth.has_role(Role.ORG_ADMIN) and auth.org in ("*", org))
    )


async def handle_erasure_request(input: ErasureRequestInput) -> ErasureRequestOutput:
    # Scope-specific authz beyond the wire-level WRITER check: erasure needs
    # admin on the target. namespace scope → ADMIN on the namespace by the
    # canonical check (a scoped token's ns_roles entry is its ceiling there);
    # user_id and namespace_list span namespaces → the org-wide view.
    auth = auth_context.current_auth()
    if auth is not None:
        allowed = _org_wide_admin(auth, input.org) or (
            input.scope_type == "namespace"
            and await authz.row_in_scope(input.org, input.scope_value, Role.ADMIN)
        )
        if not allowed:
            raise AuthInsufficientError(
                f"erasure on scope_type={input.scope_type!r} requires admin on the target "
                f"{input.scope_value!r}; principal {auth.subject!r} doesn't have it"
            )

    input, row, erasure_id, now = await begin_erasure(input)
    return await run_saga(
        input=input,
        row=row,
        erasure_id=erasure_id,
        requested_at=now,
        previously_completed=set(),
    )


async def erase_namespace(org: str, namespace: str, *, requester: str) -> ErasureRequestOutput:
    """A purging erasure of one namespace, for an op that deletes it with what it
    holds (`collection.delete`, whose transport checked admin on it): its rows
    leave the disk, not just the readable set. A one-name `namespace_list` scope,
    the scope a purge rides on (and a resume reads it back from)."""
    input = ErasureRequestInput(
        org=org,
        scope_type="namespace_list",
        scope_value=json.dumps({"namespaces": [namespace], "purge": True}),
        requester=requester,
        purge=True,
    )
    input, row, erasure_id, now = await begin_erasure(input)
    return await run_saga(
        input=input, row=row, erasure_id=erasure_id, requested_at=now, previously_completed=set()
    )


def _serialize_row(row: Mapping[str, object]) -> dict[str, object]:
    """Bytes decoded; datetimes, UUIDs and Decimals stay for Pydantic's JSON dump."""
    return {
        k: v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
        for k, v in row.items()
    }


def _page(rows: list[Any], limit: int) -> tuple[list[dict[str, object]], int, bool]:
    """(serialized rows, count, truncated) of a read that asked for `limit + 1`."""
    truncated = len(rows) > limit
    kept = [_serialize_row(r) for r in rows[:limit]]
    return kept, len(kept), truncated


async def _export_memory(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    include_tombstoned: bool,
    limit: int,
    shown: erasure_exports.Shown,
) -> tuple[list[dict[str, object]], int, bool]:
    rows = await erasure_exports.export_memory(
        org,
        scope_type,
        scope_value,
        include_tombstoned=include_tombstoned,
        limit=limit + 1,
        shown=shown,
    )
    return _page(rows, limit)


_MEMORY_SUBJECT = ("memory_key", "memory_id", "version")
# Conversation rows carry no label; their text decides, as for raw evidence.
_CONVERSATION_TEXT = ("content", "reasoning", "metadata")
_CONVERSATION_SUBJECT = ("agent", "thread_id", "msg_id")
_THREAD_TEXT = ("title", "metadata")
_THREAD_SUBJECT = ("agent", "thread_id")


async def _audit_high_rows(
    org: str, rows: list[dict[str, object]], subject: tuple[str, ...] = _MEMORY_SUBJECT
) -> None:
    """The export serves high rows on purpose; each one it serves is audited as a key read is."""
    by_ns: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        if r.get("sensitivity") == "high":
            by_ns.setdefault(str(r["namespace"]), []).append({k: r[k] for k in subject})
    for ns, subjects in by_ns.items():
        await audit.emit_many(
            "memory.sensitive_read",
            org=org,
            namespace=ns,
            actor=writer_actor("-"),
            subjects=subjects,
        )


async def _export_knowledge(
    org: str, scope_type: str, scope_value: str, *, limit: int, shown: erasure_exports.Shown
) -> tuple[list[dict[str, object]], int, bool]:
    """The chunks of one namespace, or every chunk one user wrote."""
    rows = await erasure_exports.export_knowledge(
        org, scope_type, scope_value, limit=limit + 1, shown=shown
    )
    return _page(rows, limit)


def _label_high(rows: list[dict[str, object]], fields: tuple[str, ...]) -> None:
    """Set `sensitivity` to 'high' on each row whose text reads as high, as a
    labelled memory row carries it, so the export audits it the same way."""
    for r in rows:
        r["sensitivity"] = "high" if any(reads_high(str(r[f])) for f in fields) else ""


async def _export_agent_events(
    org: str, scope_type: str, scope_value: str, *, limit: int, shown: erasure_exports.Shown
) -> tuple[list[dict[str, object]], int, bool]:
    rows = await erasure_exports.export_agent_events(
        org, scope_type, scope_value, limit=limit + 1, shown=shown
    )
    return _page(rows, limit)


async def _export_audit_log(
    org: str, scope_type: str, scope_value: str, *, limit: int, shown: erasure_exports.Shown
) -> tuple[list[dict[str, object]], int, bool]:
    """Audit rows about this subject. namespace scope filters by tag; user_id
    scope filters by substring over the JSON `subject` blob (no native user_id
    column) — coarse but adequate for an export."""
    if scope_type == "namespace":
        rows = await erasure_exports.export_audit_namespace(
            org, scope_value, limit=limit + 1, shown=shown
        )
    else:
        rows = await erasure_exports.export_audit_by_subject_substring(
            org, scope_value, limit=limit + 1, shown=shown
        )
    return _page(rows, limit)


async def handle_erasure_export(input: ErasureExportInput) -> ErasureExportOutput:
    now = datetime.now(UTC).replace(tzinfo=None)

    # The input carries no `namespace`, so the transport bound an org-only check. A
    # namespace export is authorized against the namespace itself (a restricted one needs
    # the two-factor ADMIN check); a subject export spans every namespace of the org and
    # needs the org-wide view, less the restricted namespaces and the collections this
    # caller may not read (an org admin may erase what it may not read).
    auth = auth_context.current_auth()
    hidden: set[str] = set()
    # The collections the export may show; None: every one (no auth, a platform admin).
    readable_collections: set[str] | None = None
    if auth is not None:
        if input.scope_type == "namespace":
            try:
                await authz.check_authorized(
                    auth, Role.ADMIN, org=input.org, namespace=input.scope_value
                )
            except PermissionError as e:
                raise AuthInsufficientError(str(e)) from e
        else:
            if not _org_wide_admin(auth, input.org):
                raise AuthInsufficientError(
                    f"erasure.export scope_type={input.scope_type!r} requires org admin"
                )
            hidden = set(await auth_context.restricted_namespaces_hidden_from(input.org))
            if "*" in hidden:
                raise AuthInsufficientError(
                    f"cannot export {input.org}: namespace classification unavailable"
                )
            if not auth.has_role(Role.PLATFORM_ADMIN):
                readable_collections = set(await sharing.readable(auth, input.org))
                if input.scope_type == "user_id":
                    # The subject's own collection is theirs to have exported.
                    readable_collections.add(personal_name(input.org, "user:" + input.scope_value))

    # Applied in each read, before its LIMIT: a hidden row never takes the
    # place of one the caller may see.
    shown = erasure_exports.Shown(
        hidden=tuple(sorted(hidden)),
        collections=None if readable_collections is None else tuple(sorted(readable_collections)),
        collection_prefix=sharing.COLLECTION_PREFIX,
    )
    mem_rows, mem_count, mem_trunc = await _export_memory(
        input.org,
        input.scope_type,
        input.scope_value,
        include_tombstoned=input.include_tombstoned,
        limit=input.section_limit,
        shown=shown,
    )
    know_rows, know_count, know_trunc = await _export_knowledge(
        input.org,
        input.scope_type,
        input.scope_value,
        limit=input.section_limit,
        shown=shown,
    )
    turns, conv_trunc = await erasure_exports.export_conversations(
        input.org,
        input.scope_type,
        input.scope_value,
        include_tombstoned=input.include_tombstoned,
        limit=input.section_limit,
        max_bytes=settings.erasure_export_conversation_max_bytes,
        as_of=now,
        shown=shown,
    )
    # The read is already capped at `section_limit` and says whether it cut rows.
    conv_rows = [_serialize_row(r) for r in turns]
    conv_count = len(conv_rows)
    threads = await erasure_exports.export_conversation_threads(
        input.org,
        input.scope_type,
        input.scope_value,
        include_tombstoned=input.include_tombstoned,
        limit=input.section_limit + 1,
        shown=shown,
    )
    thread_rows, thread_count, thread_trunc = _page(threads, input.section_limit)
    # A turn can be megabytes long, so the check runs off the event loop.
    await asyncio.to_thread(_label_high, conv_rows, _CONVERSATION_TEXT)
    await asyncio.to_thread(_label_high, thread_rows, _THREAD_TEXT)
    evt_rows, evt_count, evt_trunc = await _export_agent_events(
        input.org,
        input.scope_type,
        input.scope_value,
        limit=input.section_limit,
        shown=shown,
    )
    aud_rows, aud_count, aud_trunc = await _export_audit_log(
        input.org,
        input.scope_type,
        input.scope_value,
        limit=input.section_limit,
        shown=shown,
    )
    await _audit_high_rows(input.org, mem_rows)
    await _audit_high_rows(input.org, conv_rows, _CONVERSATION_SUBJECT)
    await _audit_high_rows(input.org, thread_rows, _THREAD_SUBJECT)

    counts = {
        "memory": mem_count,
        "knowledge": know_count,
        "conversations": conv_count,
        "conversation_threads": thread_count,
        "agent_events": evt_count,
        "audit_log": aud_count,
    }
    await audit.emit(
        event_type="erasure.exported",
        org=input.org,
        namespace=audit_namespace(input.scope_type, input.scope_value),
        actor=writer_actor(input.requester),
        subject={
            "scope_type": input.scope_type,
            "scope_value": input.scope_value,
            "requester": input.requester,
            "counts": counts,
        },
        request_id=str(uuid.uuid4()),
    )

    return ErasureExportOutput(
        exported_at=now,
        org=input.org,
        scope_type=input.scope_type,
        scope_value=input.scope_value,
        requester=input.requester,
        counts=counts,
        memory=ErasureExportSection(rows=mem_rows, count=mem_count, truncated=mem_trunc),
        knowledge=ErasureExportSection(rows=know_rows, count=know_count, truncated=know_trunc),
        conversations=ErasureExportSection(rows=conv_rows, count=conv_count, truncated=conv_trunc),
        conversation_threads=ErasureExportSection(
            rows=thread_rows, count=thread_count, truncated=thread_trunc
        ),
        agent_events=ErasureExportSection(rows=evt_rows, count=evt_count, truncated=evt_trunc),
        audit_log=ErasureExportSection(rows=aud_rows, count=aud_count, truncated=aud_trunc),
    )


async def _authorize_status_read(row: RequestStatusRow) -> bool:
    """Whether the current principal may read this saga's status.

    The op is addressed by erasure_id alone — no org in the body for the
    transport authz to scope — so the saga row itself is the authority.
    Namespace-scoped sagas require the token to cover (org, scope_value),
    grant fallback included. A user_id saga's status names the erased user,
    and its id reaches every memory webhook of the org that takes
    `memory.erased`, so it needs the org-wide view that requesting one needs,
    held by a token of the saga's org. Other scopes need flat membership of
    the saga's org. False on refusal, so the caller answers found=False
    exactly as for an unknown id — a 403 would confirm the id exists and name
    its org. True when auth is disabled
    (LOOM_AUTH_REQUIRED=false).
    """
    auth = auth_context.current_auth()
    if auth is None:
        return True
    row_org = str(row["org"])
    if str(row["scope_type"]) == "namespace":
        namespace = str(row["scope_value"])
        # An org admin may erase a collection its grants do not open (a deleted
        # one's documents go no other way), so it sees that erasure's status.
        if sharing.is_collection(namespace) and _org_wide_admin(auth, row_org):
            return True
        return await authz.row_in_scope(row_org, namespace, Role.VIEWER)
    if str(row["scope_type"]) == "user_id":
        return auth.org in ("*", row_org) and _org_wide_admin(auth, row_org)
    # A namespace list spans namespaces, and a scoped token's authority is its
    # namespaces — org membership is a flat token's gate, never a scoped one's.
    return auth.org in ("*", row_org) and not auth.has_ns_roles()


async def handle_erasure_status(input: ErasureStatusInput) -> ErasureStatusOutput:
    row = await erasure_ledger.fetch_request_status(input.erasure_id)
    if row is None:
        return ErasureStatusOutput(found=False, erasure_id=input.erasure_id)
    if not await _authorize_status_read(row):
        return ErasureStatusOutput(found=False, erasure_id=input.erasure_id)
    return ErasureStatusOutput(
        found=True,
        erasure_id=str(row["id"]),
        org=row["org"],
        scope_type=row["scope_type"],
        scope_value=row["scope_value"],
        requester=row["requester"],
        status=cast("ErasureStatus", row["status"]),
        current_step=int(row["current_step"]),
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        failure_reason=row["failure_reason"] or None,
        residue_count=int(row.get("residue_count") or 0),
        residue=_parse_residue(row.get("residue")),
        verified_at=row.get("verified_at"),
    )


def _parse_residue(raw: str | None) -> dict[str, int]:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}


async def _list_scope(org: str) -> tuple[str, dict[str, Any]] | None:
    """The erasure.list rows the caller may see, as (WHERE fragment, params); None = none.

    The org-wide view (every scope type, user_id sagas included) is erasure.request's.
    Even that view leaves out the restricted namespaces the caller holds no grant +
    claim for. A namespace-wildcard flat token sees every namespace's sagas under the
    same restricted rule; everyone else sees the namespace sagas of the namespaces it
    may read (restricted ones only with the grant)."""
    auth = auth_context.current_auth()
    if auth is None:
        return "", {}
    admin_view = _org_wide_admin(auth, org)
    if admin_view or (not auth.has_ns_roles() and "*" in (auth.namespaces or ())):
        hidden = await auth_context.restricted_namespaces_hidden_from(org)
        if hidden == ["*"]:
            return None
        where = "" if admin_view else " AND scope_type = 'namespace'"
        params: dict[str, object] = {}
        if not (admin_view or auth.has_role(Role.PLATFORM_ADMIN)):
            # A collection's erasures show only to whom the collection shows, and
            # to an org admin, who may request them.
            where += (
                " AND (scope_type != 'namespace'"
                " OR NOT startsWith(scope_value, {col_prefix:String})"
                " OR scope_value IN {col_readable:Array(String)})"
            )
            params["col_prefix"] = sharing.COLLECTION_PREFIX
            params["col_readable"] = await sharing.readable(auth, org)
        if hidden:
            where += " AND NOT (scope_type = 'namespace' AND scope_value IN {hidden:Array(String)})"
            params["hidden"] = hidden
        return where, params
    ns_list = await auth_context.rollup_member_namespaces(org)
    if not ns_list:
        return None
    return (
        " AND scope_type = 'namespace' AND scope_value IN {nslist:Array(String)}",
        {"nslist": ns_list},
    )


async def handle_erasure_list(input: ErasureListInput) -> ErasureListOutput:
    """Audit-shaped roll-up of the org's erasures the caller may see: the
    org-wide view for an org admin, the namespace sagas of the caller's own
    namespaces otherwise."""
    scope = await _list_scope(input.org)
    if scope is None:
        return ErasureListOutput(rows=[])
    rows = await erasure_ledger.list_requests(
        input.org,
        status=input.status or None,
        limit=int(input.limit),
        scope_where=scope[0],
        scope_params=scope[1],
    )
    return ErasureListOutput(
        rows=[
            ErasureListRow(
                erasure_id=str(r["id"]),
                scope_type=str(r["scope_type"]),
                scope_value=str(r["scope_value"]),
                requester=str(r["requester"]),
                status=cast("ErasureStatus", r["status"]),
                requested_at=r["requested_at"],
                completed_at=r["completed_at"],
                failure_reason=str(r.get("failure_reason") or ""),
                steps_completed_count=int(r.get("steps_completed_count") or 0),
                reversals_count=int(r.get("reversals_count") or 0),
                residue_count=int(r.get("residue_count") or 0),
                verified_at=r.get("verified_at"),
            )
            for r in rows
        ]
    )
