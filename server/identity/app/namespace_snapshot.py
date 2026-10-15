"""Namespace snapshot and restore: a namespace's memories, types, grants and
audit rows as one payload, and that payload written back into a namespace."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from server.auth.revocation import write_grant_cutoffs
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now_seconds
from server.core.grant_sql import LIVE_GRANT
from server.identity.api.models import (
    NamespaceRestoreInput,
    NamespaceRestoreOutput,
    NamespaceSnapshotInput,
    NamespaceSnapshotOutput,
    _SnapshotAuditRow,
    _SnapshotGrantRow,
    _SnapshotMemoryRow,
    _SnapshotTypeRow,
)
from server.identity.app import grant_writes
from server.identity.app import namespace as _registry
from server.identity.store import grants as grants_repo
from server.identity.store import namespaces as namespaces_repo
from server.memory.app import snapshot_rows as memory_snapshot
from server.memory.app.provenance import writer_actor
from server.operations.base import grant_rank


def snapshot_memory_row(r: Mapping[str, Any]) -> _SnapshotMemoryRow:
    """One loom.memory row (as the memory plane reads it for a snapshot) → the payload model."""
    return _SnapshotMemoryRow(**memory_snapshot.snapshot_fields(r, _SnapshotMemoryRow.model_fields))


async def handle_namespace_snapshot(
    input: NamespaceSnapshotInput,
) -> NamespaceSnapshotOutput:
    """Read every curated row for one namespace and pack it into a
    portable payload. ADMIN-on-namespace; one row written to
    `loom.namespace_snapshots` for the audit trail."""
    import uuid as _uuid

    from server.memory.app.namespace_types import list_type_configs

    ch = get_client()
    snapshot_id = str(_uuid.uuid4())
    created_at = naive_utc_now_seconds()

    # Memories — active, non-tombstoned.
    memory_rows = await memory_snapshot.read_namespace(ch, org=input.org, namespace=input.namespace)
    memories = [snapshot_memory_row(r) for r in memory_rows]

    # Per-type configs.
    type_dicts = await list_type_configs(input.org, input.namespace)
    types = [_SnapshotTypeRow(**t) for t in type_dicts]

    # Live grants only: the payload row has no status, and a restore writes
    # each one active, so a pending join request would come back as a grant.
    grant_rows = await ch.query_all(
        """
        SELECT subject, role, granted_by
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND {live}
        """.replace("{live}", LIVE_GRANT),
        params={"org": input.org, "ns": input.namespace},
    )
    grants = [
        _SnapshotGrantRow(
            subject=g["subject"],
            role=g["role"],
            granted_by=g.get("granted_by") or "",
        )
        for g in grant_rows
    ]

    # Audit tail. Bounded by include_audit_tail_n.
    audit_tail: list[_SnapshotAuditRow] = []
    if input.include_audit_tail_n > 0:
        audit_rows = await ch.query_all(
            """
            SELECT ts, event_type, actor, subject AS subject_json
            FROM loom.audit_log
            WHERE org = {org:String} AND namespace = {ns:String}
            ORDER BY ts DESC
            LIMIT {n:UInt32}
            """,
            params={
                "org": input.org,
                "ns": input.namespace,
                "n": input.include_audit_tail_n,
            },
        )
        audit_tail = [
            _SnapshotAuditRow(
                ts=a["ts"],
                event_type=a["event_type"],
                actor=a.get("actor") or "",
                subject_json=a.get("subject_json") or "",
            )
            for a in audit_rows
        ]

    caller = _registry.resolve_subject()
    await ch.insert(
        "loom.namespace_snapshots",
        [
            {
                "snapshot_id": snapshot_id,
                "org": input.org,
                "source_namespace": input.namespace,
                "created_at": created_at,
                "created_by": caller,
                "include_embeddings": input.include_embeddings,
                "memory_count": len(memories),
                "type_count": len(types),
                "grant_count": len(grants),
                "audit_tail_count": len(audit_tail),
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )

    return NamespaceSnapshotOutput(
        snapshot_id=snapshot_id,
        org=input.org,
        source_namespace=input.namespace,
        created_at=created_at,
        memory_count=len(memories),
        type_count=len(types),
        grant_count=len(grants),
        audit_tail_count=len(audit_tail),
        include_embeddings=input.include_embeddings,
        memories=memories,
        types=types,
        grants=grants,
        audit_tail=audit_tail,
    )


async def handle_namespace_restore(
    input: NamespaceRestoreInput,
) -> NamespaceRestoreOutput:
    """Apply a snapshot's payload to a target namespace. The target is
    auto-created if missing and the caller becomes its admin. One
    provenance audit row records the source snapshot_id + namespace."""
    import time as _time

    from server.observability import audit as _audit

    ch = get_client()
    caller = _registry.resolve_subject()
    payload = input.payload

    # Existing target, active or soft-deleted (namespace.delete leaves its data in place) ⇒
    # the admin + restricted check of namespace.register, less its pass for the
    # auto-registered owner (a restore writes grants), before anything about the
    # target is read or answered. Fresh target ⇒ caller becomes admin via the auto-grant
    # below.
    target_existing = await namespaces_repo.fetch_row(input.org, input.target_namespace, ch=ch)
    if target_existing is not None:
        await _registry.require_admin_of_existing(
            target_existing,
            input.org,
            input.target_namespace,
            caller,
            visibility=None,
            owner_may_complete=False,
        )

    # Idempotency: if this snapshot_id already appears as a restore
    # event on the target namespace's audit log, skip.
    existing = await ch.query_one(
        """
        SELECT 1 AS x FROM loom.audit_log
        WHERE org = {org:String} AND namespace = {ns:String}
          AND event_type = 'namespace.restore'
          AND positionCaseInsensitive(subject, {sid:String}) > 0
        LIMIT 1
        """,
        params={
            "org": input.org,
            "ns": input.target_namespace,
            "sid": payload.snapshot_id,
        },
    )
    if existing is not None:
        return NamespaceRestoreOutput(
            restored=False,
            target_namespace=input.target_namespace,
            snapshot_id=payload.snapshot_id,
            counts={
                "memories": payload.memory_count,
                "types": payload.type_count,
                "grants": payload.grant_count,
            },
        )

    # The cutoffs for the grants this restore lowers come before any row, as
    # in every other grant change: a failure then writes nothing.
    lowered: list[str] = []
    kept: list[str] = []
    if payload.grants and target_existing is not None:
        lowered, kept = await _end_lowered_claims(
            input.org, input.target_namespace, payload.grants, caller, ch
        )

    now = naive_utc_now_seconds()

    # Only CREATE the namespace row when absent — an RMT insert over an
    # existing namespace would reset its config to defaults. Restoring
    # into an existing namespace touches only the rows below, except a
    # soft-deleted one, which is reactivated with every other column
    # carried forward. Reuses the probe above; nothing between them
    # mutates loom.namespaces.
    if target_existing is not None and not bool(target_existing.get("active", False)):
        await ch.insert("loom.namespaces", [{**target_existing, "active": True, "updated_at": now}])
    elif target_existing is None:
        await ch.insert(
            "loom.namespaces",
            [
                {
                    "org": input.org,
                    "namespace": input.target_namespace,
                    "owner_email": caller,
                    "description": (f"Restored from {payload.org}/{payload.source_namespace}"),
                    "tags": [],
                    "active": True,
                    "conflict_threshold": None,
                    "retention_days": 0,
                    "warm_days": 0,
                    "visibility": "private",
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
    await _registry.insert_admin_grant(ch, input.org, input.target_namespace, caller)

    # Bump versions off a single base so restored rows win against any
    # pre-existing row with the same identity.
    version_base = _time.time_ns()
    if payload.memories:
        # Every persisted column, through the builder; the typed insert refuses a row that is
        # not a full version row.
        rows = memory_snapshot.restore_rows(
            payload.memories,
            org=input.org,
            target_namespace=input.target_namespace,
            rebind_agent=input.rebind_agent,
            version_base=version_base,
            actor=writer_actor(),
        )
        await memory_snapshot.insert_version_rows(rows, ch=ch)

    # Per-type configs.
    if payload.types:
        await ch.insert(
            "loom.namespace_types",
            [
                {
                    "org": input.org,
                    "namespace": input.target_namespace,
                    "memory_type": t.memory_type,
                    "retention_days": t.retention_days,
                    "dedup_threshold": t.dedup_threshold,
                    "hot_quota": t.hot_quota,
                    "conflict_policy": t.conflict_policy or "",
                    "created_at": now,
                    "updated_at": now,
                }
                for t in payload.types
            ],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )

    # Grants. granted_by = caller so the restore is traceable; the
    # original granted_by stays in the snapshot payload.
    if payload.grants:
        # Of a subject's rows, the last is the one that lands.
        last = {g.subject: g for g in payload.grants}
        rows = [
            {
                "org": input.org,
                "namespace": input.target_namespace,
                "subject": g.subject,
                "role": g.role,
                "granted_at": now,
                "granted_by": caller,
                "revoked_at": None,
                "version": version_base + i,
            }
            for i, g in enumerate(last.values())
        ]
        cut = set(lowered)
        await grant_writes.finish_lowering(
            input.org, [r for r in rows if r["subject"] in cut], revoked_by=caller, ch=ch
        )
        await grant_writes.write_rows([r for r in rows if r["subject"] not in cut], ch=ch)
        if target_existing is not None:
            # A subject the restore leaves as it is may be a retry's, whose lowered
            # grant landed without its second cutoff.
            await grant_writes.recheck(
                input.org,
                [(input.target_namespace, s) for s in kept],
            )

    # Provenance audit row — carries the snapshot_id in the subject for
    # the idempotency lookup on re-restores.
    await _audit.emit(
        event_type="namespace.restore",
        org=input.org,
        namespace=input.target_namespace,
        actor=caller,
        subject={
            "snapshot_id": payload.snapshot_id,
            "source_org": payload.org,
            "source_namespace": payload.source_namespace,
            "snapshot_created_at": payload.created_at.isoformat(),
            "counts": {
                "memories": payload.memory_count,
                "types": payload.type_count,
                "grants": payload.grant_count,
            },
        },
        request_id=payload.snapshot_id,
    )

    return NamespaceRestoreOutput(
        restored=True,
        target_namespace=input.target_namespace,
        snapshot_id=payload.snapshot_id,
        counts={
            "memories": payload.memory_count,
            "types": payload.type_count,
            "grants": payload.grant_count,
        },
    )


async def _end_lowered_claims(
    org: str, namespace: str, grants: Sequence[Any], caller: str, ch: Any
) -> tuple[list[str], list[str]]:
    """A restore writes its grants over the live ones. A subject it lowers
    loses the claims of older tokens there, as any other downgrade does. The
    caller counts as admin: the restore makes them one before its grants.
    Of a subject's rows, the last is the one that lands. Returns the subjects
    it lowers and those it leaves at their live role."""
    restored = {g.subject: g.role for g in grants}
    live = await grants_repo.active_roles(org, namespace, list(restored), ch=ch)
    live[caller] = "admin"
    lowered = [
        subject
        for subject, role in restored.items()
        if subject in live and grant_rank(live[subject]) > grant_rank(role)
    ]
    kept = sorted(s for s, role in restored.items() if s in live and live[s] == role)
    if lowered:
        await write_grant_cutoffs(
            org, [namespace], lowered, reason="namespace.restore", revoked_by=caller
        )
    return lowered, kept
