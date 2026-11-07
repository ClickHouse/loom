"""memory.cross_promote and memory.copy_to: a memory copied into another
namespace as a fresh row that names its source."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.auth.context import current_auth
from server.core.datetimes import naive_utc_now
from server.identity.app.request_scope import request_scope
from server.memory.api.models_lifecycle import (
    MemoryCopyToInput,
    MemoryCopyToOutput,
    MemoryCrossPromoteInput,
    MemoryCrossPromoteOutput,
)
from server.memory.app.provenance import writer_actor
from server.memory.domain.delete_marks import carried_trace
from server.memory.store.rows import next_version

if TYPE_CHECKING:
    from server.memory.store import MemoryRow

# What a copy into another namespace resets: the row is a new memory there,
# so its lifecycle, usage, lock and expiry start over; everything else —
# facets, validity anchors, sensitivity, grounding, attestation (bound to the
# content, which is the same) — is carried from the source.
_COPY_RESETS: dict[str, Any] = {
    "user_id": "-",
    "session_id": "-",
    "run_id": "-",
    "tombstoned_at": None,
    "expires_at": None,
    "pinned": 0,
    "status": "active",
    "conflict_with": [],
    "duplicate_of": "",
    "superseded_by": "",
    "rejection_reason": "",
    "invalid_at": None,
    "redacted_at": None,
    "redaction_reason": "",
    "recall_count": 0,
    "last_recalled_at": None,
    "evidence_count": 1,
    "reinforcement_count": 1,
    "lock_mode": "",
    "lock_reason": "",
    "lock_until": None,
    "locked_by": "",
    "locked_at": None,
    "on_expire": "",
    "expire_reason": "",
    "source_class": "imported",
}


def copied_memory_row(
    src: MemoryRow,
    *,
    org: str,
    namespace: str,
    agent: str,
    memory_key: str,
    memory_id: str,
    version: int,
    value: str,
    from_memory_id: str | None,
    from_namespace: str,
    actor: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The loom.memory row memory.copy_to / memory.cross_promote write: every persisted
    column through the builder, so a copy keeps the source's `sensitivity`, validity
    anchors, grounding and attestation."""
    from server.memory.store import build_memory_version_row, uuid_or_none

    stamp = now or naive_utc_now()
    return build_memory_version_row(
        src,
        org=org,
        namespace=namespace,
        agent=agent,
        memory_key=memory_key,
        memory_id=memory_id,
        version=version,
        value=value,
        source_trace=carried_trace(getattr(src, "source_trace", "")),
        actor=actor,
        created_at=stamp,
        updated_at=stamp,
        last_reinforced_at=stamp,
        cross_promoted_from_memory_id=uuid_or_none(from_memory_id),
        cross_promoted_from_namespace=from_namespace,
        **_COPY_RESETS,
    )


async def handle_memory_cross_promote(
    input: MemoryCrossPromoteInput,
) -> MemoryCrossPromoteOutput:
    """Copy a source memory into the target namespace with cross_promoted_from_*
    lineage set. Requires admin on both source and target, and enforces the
    target's blacklist. Skips the embedding write — the target's embedding_worker
    regenerates under its own embedder.
    """
    import uuid as _uuid

    import server.memory.store as memory_repo
    from server.auth.authz import Role as _Role
    from server.auth.authz import row_in_scope
    from server.core.ch import get_client as _get_client
    from server.core.errors import AuthInsufficientError, NotFoundError
    from server.memory.app.blacklist_guard import enforce_on_insert

    ch = _get_client()
    caller = _resolve_subject_local()

    # ADMIN on both ends, by the canonical check: a scoped token's ns_roles
    # entry is its ceiling there, whatever its flat roles say.
    for ns in (input.from_namespace, input.to_namespace):
        if not await row_in_scope(input.from_org, ns, _Role.ADMIN):
            raise AuthInsufficientError(
                f"{caller!r} is not an admin of {input.from_org}/{ns}; cannot cross-promote"
            )

    src = await memory_repo.get_by_id(
        input.from_memory_id,
        org=input.from_org,
        fresh=True,
        scope=request_scope(),
    )
    # The id is org-wide; the ADMIN check above covered from_namespace only. A row
    # in any other namespace reads as not found, so the copy can't reach it and
    # the answer doesn't confirm the id exists.
    if src is None or src.namespace != input.from_namespace:
        raise NotFoundError(
            f"source memory {input.from_memory_id!r} not found in "
            f"{input.from_org}/{input.from_namespace}"
        )

    import json as _json

    target_key = input.target_key or src.memory_key
    target_memory_id = str(_uuid.uuid4())
    version = next_version()
    serialized_value = _json.dumps(src.value)

    await enforce_on_insert(
        org=input.from_org,
        namespace=input.to_namespace,
        value=serialized_value,
    )

    # No embedding row here — the target's embedding_worker picks this up.
    # A full version row through the builder: the copy keeps the source's facets and
    # attestation and resets its lifecycle.
    await memory_repo.insert_memory_versions(
        [
            copied_memory_row(
                src,
                org=input.from_org,
                namespace=input.to_namespace,
                agent=input.target_agent,
                memory_key=target_key,
                memory_id=target_memory_id,
                version=version,
                value=serialized_value,
                from_memory_id=input.from_memory_id,
                from_namespace=input.from_namespace,
                actor=writer_actor(),
            )
        ],
        ch=ch,
    )

    from server.observability import audit as _audit

    await _audit.emit(
        event_type="memory.cross_promote",
        org=input.from_org,
        namespace=input.to_namespace,
        actor=caller,
        subject={
            "from_namespace": input.from_namespace,
            "from_memory_id": input.from_memory_id,
            "to_namespace": input.to_namespace,
            "to_memory_id": target_memory_id,
            "memory_key": target_key,
            "memory_type": src.memory_type,
            "rationale": _audit.digest(input.rationale),
        },
        request_id=target_memory_id,
    )

    return MemoryCrossPromoteOutput(
        promoted=True,
        target_memory_id=target_memory_id,
        target_namespace=input.to_namespace,
        target_key=target_key,
        cross_promoted_from_memory_id=input.from_memory_id,
    )


async def handle_memory_copy_to(
    input: MemoryCopyToInput,
) -> MemoryCopyToOutput:
    """Self-service copy of a memory into another namespace.

    Unlike memory.cross_promote (operator-only: ADMIN on BOTH ends, addressed by
    UUID), this lets any user copy a memory they can READ into any namespace they
    can WRITE — no ADMIN required — and is addressed by (namespace, memory_key)
    the way the UI holds memories. Non-destructive: the source row stays; the
    target row records cross_promoted_from_* lineage and is re-embedded by the
    target namespace's embedding_worker under its own embedder.
    """
    import json as _json
    import uuid as _uuid

    import server.memory.store as memory_repo
    from server.auth.authz import Role as _Role
    from server.auth.authz import row_in_scope
    from server.core.ch import get_client as _get_client
    from server.core.errors import AuthInsufficientError, NotFoundError
    from server.memory.app.blacklist_guard import enforce_on_insert
    from server.observability import audit as _audit

    ch = _get_client()
    caller = _resolve_subject_local()

    # READ on the source, WRITE on the target (the guard that keeps a user
    # from injecting into a namespace they can't write), by the canonical
    # check: a scoped token's ns_roles entry is its ceiling there.
    if not await row_in_scope(input.from_org, input.from_namespace, _Role.VIEWER):
        raise AuthInsufficientError(
            f"{caller!r} cannot read {input.from_org}/{input.from_namespace}; cannot copy from it"
        )
    if not await row_in_scope(input.from_org, input.to_namespace, _Role.WRITER):
        raise AuthInsufficientError(
            f"{caller!r} is not a writer of "
            f"{input.from_org}/{input.to_namespace}; cannot copy into it"
        )

    identity = memory_repo.Identity(
        org=input.from_org,
        namespace=input.from_namespace,
        agent=input.from_agent,
        user_id=input.from_user_id,
        session_id=input.from_session_id,
    )
    src = await memory_repo.get_by_key(
        identity, input.memory_key, fresh=True, scope=request_scope()
    )
    if src is None:
        raise NotFoundError(
            f"source memory {input.memory_key!r} not found in "
            f"{input.from_org}/{input.from_namespace}"
        )

    target_key = input.target_key or src.memory_key
    target_memory_id = str(_uuid.uuid4())
    version = next_version()
    serialized_value = _json.dumps(src.value)

    await enforce_on_insert(
        org=input.from_org,
        namespace=input.to_namespace,
        value=serialized_value,
    )

    # No embedding row — the target's embedding_worker regenerates it under its
    # own embedder. A full version row through the builder.
    await memory_repo.insert_memory_versions(
        [
            copied_memory_row(
                src,
                org=input.from_org,
                namespace=input.to_namespace,
                agent=input.target_agent,
                memory_key=target_key,
                memory_id=target_memory_id,
                version=version,
                value=serialized_value,
                from_memory_id=getattr(src, "memory_id", None),
                from_namespace=input.from_namespace,
                actor=writer_actor(),
            )
        ],
        ch=ch,
    )

    await _audit.emit(
        event_type="memory.copy_to",
        org=input.from_org,
        namespace=input.to_namespace,
        actor=caller,
        subject={
            "from_namespace": input.from_namespace,
            "from_memory_key": input.memory_key,
            "to_namespace": input.to_namespace,
            "to_memory_id": target_memory_id,
            "memory_key": target_key,
            "memory_type": src.memory_type,
            "rationale": _audit.digest(input.rationale),
        },
        request_id=target_memory_id,
    )

    return MemoryCopyToOutput(
        copied=True,
        target_memory_id=target_memory_id,
        target_namespace=input.to_namespace,
        target_key=target_key,
    )


def _resolve_subject_local() -> str:
    """Return the caller's subject from the auth ContextVar, or raise if absent."""
    from server.core.errors import ValidationError

    auth = current_auth()
    if auth is not None and auth.subject:
        return auth.subject
    raise ValidationError(
        "cross_promote requires an authenticated caller; enable LOOM_AUTH_REQUIRED=true"
    )
