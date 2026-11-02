"""memory.verify — a person (or a sanctioned process) attests to one row.

Reinforce with provenance. `memory.pin` protects a fact from conflict
eviction; `memory.verify` records WHO looked at it and against WHICH content.
The attestation is bound to the canonical content hash, so a later edit does
not erase it — it makes it stale, visibly, until someone re-verifies.

Rules, all enforced here rather than trusted from the body:

  * Under auth the actor is derived from the token: `human:<subject>`, or
    `process:<subject>` for a service account. The body's `actor` is ignored.
    An agent therefore cannot mark its own write `human:`.
  * Without auth (dev mode) the body must supply a well-formed actor.
  * The caller must hold WRITER on the row's namespace (`row_in_scope`;
    memory_id addressing bypasses the transport's namespace check, so it is
    re-done), and a row it cannot reach by id reads as not found.
  * A new ReplacingMergeTree version row is written through
    `build_memory_version_row`, carrying every column forward; the three
    attestation columns and `content_sha256` are the only deltas. The
    embeddings' `verified` ranking mirror follows after the answer; the orphan
    reconcile pass heals it when that write was lost.
  * Idempotent: re-verifying a live attestation by the same actor, or
    revoking a row that has none, writes nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException

from server.auth.authz import row_in_scope
from server.auth.context import current_auth
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import served_content_sha256
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import MemoryVerifyInput, MemoryVerifyOutput
from server.memory.domain import trust
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import next_version
from server.operations.base import Role

if TYPE_CHECKING:
    from server.memory.store import MemoryRow

log = logging.getLogger(__name__)


def _actor_for_caller(requested: str) -> str:
    auth = current_auth()
    if auth is not None and auth.subject:
        return trust.actor_for(auth.subject, is_service_account=bool(auth.is_service_account))
    actor = (requested or "").strip()
    if not trust.is_valid_actor(actor):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION",
                "message": (
                    "memory.verify needs an authenticated caller, or (dev mode) an "
                    "explicit actor of the form 'human:<id>' or 'process:<id>'"
                ),
            },
        )
    return actor


async def _resolve_row(input: MemoryVerifyInput) -> MemoryRow | None:
    import server.memory.store as memory_repo

    if input.memory_id:
        return await memory_repo.get_by_id(
            input.memory_id, org=input.org, fresh=True, scope=WHOLE_NAMESPACE
        )
    if not (input.namespace and input.memory_key):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION",
                "message": (
                    "memory.verify takes memory_id, or namespace + memory_key "
                    "(+ agent/user_id/session_id)"
                ),
            },
        )
    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent or "-",
        user_id=input.user_id or "-",
        session_id=input.session_id or "-",
    )
    # fresh: verify follows set in the same breath; a replica-lag miss must not
    # answer "not found" for a row the caller was just handed a memory_id for.
    return await memory_repo.get_by_key(
        identity, input.memory_key, fresh=True, scope=WHOLE_NAMESPACE
    )


def current_content_sha256(row: MemoryRow) -> str:
    """The row's canonical content hash, computed from the value it carries.

    The stored `content_sha256` can hold the column DEFAULT; the value
    itself is always present, so derive from that.
    """
    return served_content_sha256(row.value)


async def handle_memory_verify(input: MemoryVerifyInput) -> MemoryVerifyOutput:
    import server.memory.store as memory_repo

    # An attestation is a write: WRITER on the row's namespace, by the canonical
    # check (grants count; a scoped token's ns_roles entry is its ceiling).
    # Addressed by (namespace, key): refuse before the lookup, so the answer is
    # the same whether or not the key exists there.
    if (
        not input.memory_id
        and input.namespace
        and not await row_in_scope(input.org, input.namespace, Role.WRITER)
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": f"no access to namespace {input.namespace!r}"},
        )
    row = await _resolve_row(input)
    if row is None:
        return MemoryVerifyOutput(found=False)
    # Addressed by an org-wide id: a row the caller cannot reach reads as not
    # found — a 403 would confirm the id exists.
    if not await row_in_scope(input.org, row.namespace, Role.WRITER):
        return MemoryVerifyOutput(found=False)

    sha = current_content_sha256(row)
    before = trust.verification_state(row.verified_by, row.verified_sha256, sha)

    if input.revoke:
        actor = ""
        verified_at: datetime | None = None
        verified_sha = ""
        no_op = before == "none"
    else:
        actor = _actor_for_caller(input.actor)
        verified_at = naive_utc_now()
        verified_sha = sha
        no_op = before in ("human", "process") and row.verified_by == actor

    if no_op:
        return MemoryVerifyOutput(
            found=True,
            changed=False,
            memory_id=str(row.memory_id),
            memory_key=row.memory_key,
            namespace=row.namespace,
            verified_by=row.verified_by,
            verified_at=row.verified_at,
            verification=before,
            version=row.version,
            content_sha256=sha,
        )

    version = next_version()
    new_row = memory_repo.build_memory_version_row(
        row,
        version=version,
        verified_by=actor,
        verified_at=verified_at,
        verified_sha256=verified_sha,
        # Populate the stored hash on the way past so the row's own column
        # agrees with the attestation (a row holding the DEFAULT reads '0').
        content_sha256=sha,
    )
    await insert_memory_versions([new_row], ch=get_client())
    # The caller waits for the durable attestation, not for the ranking signal. The
    # embeddings mirror (a FINAL read of the row's vector plus a second committed insert,
    # best-effort by contract) runs after this answer on the request's loop, and the next
    # embedding write covers a loss. Three chained round trips (~11 s p50) become one.
    _schedule_mirror(row, verified=not input.revoke)

    after = trust.verification_state(actor, verified_sha, sha)
    log.info(
        "memory.verify %s/%s %s by %s (%s -> %s)",
        row.namespace,
        row.memory_key,
        "revoked" if input.revoke else "attested",
        actor or "-",
        before,
        after,
    )
    return MemoryVerifyOutput(
        found=True,
        changed=True,
        memory_id=str(row.memory_id),
        memory_key=row.memory_key,
        namespace=row.namespace,
        verified_by=actor,
        verified_at=verified_at,
        verification=after,
        version=version,
        content_sha256=sha,
    )


def _schedule_mirror(row: MemoryRow, *, verified: bool) -> None:
    """Run the embeddings mirror after the response (drained at shutdown)."""
    from server.memory.app import after_write

    after_write.schedule(
        _mirror_verified_to_embeddings(row, verified=verified), name="memory.verify.mirror"
    )


async def _mirror_verified_to_embeddings(row: MemoryRow, *, verified: bool) -> None:
    """Re-emit the key's live embedding rows with `verified` set so trust-aware ranking
    sees the attestation: every column and the stored vectors carried (no re-embed).
    Best-effort: the attestation on loom.memory is the source of truth; a miss here
    (a crash, a failed insert) is healed by orphan_vector_reconcile's drift step."""
    import server.memory.store as memory_repo
    from server.memory.domain.memory_types import is_embedded

    if not is_embedded(row.memory_type, row.sensitivity):  # a 'high' head keeps no vector
        return
    try:
        await memory_repo.reemit_embedding_rows_for_key(
            row.org,
            row.namespace,
            row.agent,
            row.user_id,
            row.session_id,
            row.memory_key,
            ch=get_client(),
            verified=1 if verified else 0,
        )
    except Exception as e:  # noqa: BLE001 — the ranking mirror is best-effort
        log.warning("memory.verify: embedding mirror failed (non-fatal): %s", e)
