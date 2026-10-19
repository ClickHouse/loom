"""memory.restore: bring a tombstoned or superseded row back as a new live
version, mirrored onto its embedding row."""

from __future__ import annotations

import logging
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.errors import BackendUnavailableError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import (
    MemoryRestoreInput,
    MemoryRestoreOutput,
)
from server.memory.app.provenance import writer_actor
from server.memory.domain.excerpt import build_excerpt
from server.memory.domain.temporal_sql import SERVED_STATUSES
from server.memory.store import key_write_lock
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import next_version

if TYPE_CHECKING:
    from server.memory.store import MemoryRow

log = logging.getLogger(__name__)


async def _read_restorable(input: MemoryRestoreInput) -> MemoryRow | None:
    import server.memory.store as memory_repo

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    return await memory_repo.get_by_key(
        identity,
        input.memory_key,
        include_tombstoned=True,
        fresh=True,
        strong=True,
        scope=WHOLE_NAMESPACE,
    )


def _undo_lock(input: MemoryRestoreInput) -> AbstractAsyncContextManager[None]:
    """The key's write lock for an Undo (`key_write_lock.key_lock_for`)."""
    return key_write_lock.key_lock_for(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
        what="the Undo",
        operation="memory.restore",
    )


async def _restore_superseded(input: MemoryRestoreInput, ch: Any) -> MemoryRestoreOutput:
    # A conditional revision holds the key's write lock from its read to its insert; the
    # Undo does too, or it could write the retired value back over that revision.
    async with _undo_lock(input):
        existing = await _read_restorable(input)
        if existing is None:
            return MemoryRestoreOutput(restored=False)
        if await _landed_late(input, existing):
            # A retry of an Undo whose restore landed after its 503: finish that one.
            version, status = int(existing.version), str(existing.status)
        elif (
            existing.tombstoned_at is not None
            or str(existing.status) != "superseded"
            or (input.if_superseded_by and existing.superseded_by != input.if_superseded_by)
        ):
            return MemoryRestoreOutput(
                restored=False, memory_id=str(existing.memory_id), version=int(existing.version)
            )
        else:
            version, status = await _write_restore(input, existing, ch=ch)
        await _unsupersede_embeddings(input, status, ch=ch)
    # The facts the retirement closed and the restored value still holds come back.
    await _reopen_facts(input, existing, version, ch=ch)
    return MemoryRestoreOutput(
        restored=True,
        memory_id=str(existing.memory_id),
        version=version,
        from_status="superseded",
    )


async def _landed_late(input: MemoryRestoreInput, head: MemoryRow) -> bool:
    """Whether the served `head` is an Undo's restore (behind `if_superseded_by` when
    given)."""
    if head.tombstoned_at is not None or str(head.status) == "superseded":
        return False
    return await _an_undos_restore(
        input, str(head.memory_id), int(head.version), input.if_superseded_by
    )


async def _an_undos_restore(
    input: MemoryRestoreInput, memory_id: str, version: int, superseded_by: str = ""
) -> bool:
    """Whether the row at `version` is the restore an Undo record names (behind
    `superseded_by` when given): an Undo whose answer was lost, or one already finished.
    False when the record cannot be read: the call is then a plain no-op."""
    from server.memory.store.conflict_resolver import undo_recorded

    try:
        return await undo_recorded(input.org, input.namespace, memory_id, version, superseded_by)
    except Exception:  # an unread record is no restore to finish
        log.warning("memory.restore: Undo record read failed for %s", memory_id, exc_info=True)
        return False


async def _write_restore(
    input: MemoryRestoreInput, existing: MemoryRow, *, ch: Any
) -> tuple[int, str]:
    """Write the Undo's record, then its restore version; (that version, the status it
    restores). Under the key's lock."""
    import server.memory.store as memory_repo
    from server.observability import audit

    new_version = next_version()
    # The search row takes the same status: ranking reads its own copy.
    status = await _status_before_retirement(existing, ch=ch)
    restore_row = memory_repo.build_memory_version_row(
        existing,
        version=new_version,
        updated_at=memory_repo.written_now(),
        status=status,
        superseded_by="",
        invalid_at=None,
    )
    # The conflict resolver honours the Undo by this record, so it is written first,
    # under the lock, and its failure fails the Undo: no restore it would not see. A
    # record whose restore then fails names a version no row holds.
    try:
        await audit.emit_strict(
            "memory.unsupersede",
            org=input.org,
            namespace=input.namespace,
            actor=writer_actor() or "-",
            subject={
                "memory_key": input.memory_key,
                "memory_id": str(existing.memory_id),
                "superseded_by": str(existing.superseded_by or ""),
                # The value restored and the restore's version: the resolver honours
                # the Undo while the row holds that value, from that version on.
                "content_sha256": str(existing.content_sha256 or ""),
                "version": new_version,
            },
        )
    except Exception as e:  # the Undo must not land without its record
        raise BackendUnavailableError(
            "the Undo could not be recorded; nothing was restored, retry the Undo",
            operation="memory.restore",
        ) from e
    try:
        await insert_memory_versions([restore_row], ch=ch)
    except Exception as e:  # an insert whose answer was lost may have landed
        if not await _restore_landed(input, new_version):
            raise BackendUnavailableError(
                "the Undo may not have landed; retry the Undo",
                operation="memory.restore",
                outcome_unknown=True,
            ) from e
    return new_version, status


async def _unsupersede_embeddings(input: MemoryRestoreInput, status: str, *, ch: Any) -> None:
    """Serve the key's search rows again, as `status`; a failure is logged, not raised."""
    import server.memory.store as memory_repo

    try:
        await memory_repo.unsupersede_embeddings(
            input.org,
            input.namespace,
            input.agent,
            input.user_id,
            input.session_id,
            input.memory_key,
            status=status,
            ch=ch,
        )
    except Exception:  # the restore landed; the search row is best effort
        log.warning(
            "memory.restore: embeddings re-emit failed for %s",
            input.memory_key,
            exc_info=True,
        )


async def _restore_landed(input: MemoryRestoreInput, version: int) -> bool:
    """Whether the key's head is the restore's version, read again after its insert
    failed: the insert may have landed with its answer lost. False when unreadable."""
    try:
        head = await _read_restorable(input)
    except Exception:  # best effort: an unknown outcome answers the 503
        log.warning("memory.restore: re-read after a failed insert failed", exc_info=True)
        return False
    return head is not None and int(head.version) == version


async def _reopen_facts(
    input: MemoryRestoreInput, existing: MemoryRow, version: int, *, ch: Any
) -> None:
    """Open again the facts the restored row states, dated at the restore (`version`):
    while it was retired or deleted, another row's revision or delete may have closed
    them. Named by the restore, so finishing it again writes nothing new."""
    from server.memory.app.entity_projections import reconcile_row_facts

    restore = f"{existing.memory_id}:{version}"
    await reconcile_row_facts(
        input,
        [str(existing.memory_id)],
        reopen=True,
        op="memory.restore",
        ch=ch,
        hashes={str(existing.memory_id): str(existing.content_sha256)},
        # Versions are time_ns: the restore's version is when it was made, the same on
        # its first write and on every retry that finishes it.
        restored_at=datetime.fromtimestamp(version / 1e9, UTC).replace(tzinfo=None),
        restore_id=uuid.uuid5(uuid.NAMESPACE_OID, restore).hex,
    )


def _served_now(row: MemoryRow) -> bool:
    """Whether reads serve `row` now: not deleted, a served status, not ended."""
    if row.tombstoned_at is not None or row.status not in SERVED_STATUSES:
        return False
    now = naive_utc_now()
    return all(end is None or end > now for end in (row.expires_at, row.valid_to))


async def _status_before_retirement(row: MemoryRow, *, ch: Any) -> str:
    """`conflicted` for a row retired while under review when a peer is still
    conflicted and lists it back; `duplicate` for a retired duplicate whose
    original is still served; `active` otherwise."""
    import server.memory.store as memory_repo

    found = await ch.query_all(
        """
        SELECT argMax(status, version) AS was FROM loom.memory_versions
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_id = {id:UUID}
          AND version < {version:UInt64} AND status != 'superseded'
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": row.org,
            "ns": row.namespace,
            "id": str(row.memory_id),
            "version": int(row.version),
        },
    )
    was = str(found[0]["was"]) if found else ""
    if was == "duplicate" and row.duplicate_of:
        # Still hidden behind the row it repeats, unless that one is gone.
        original = await memory_repo.get_by_id(
            row.duplicate_of, org=row.org, ch=ch, scope=WHOLE_NAMESPACE, fresh=True, strong=True
        )
        # Reads only stand the original in for it while it is served and not ended.
        return "duplicate" if original is not None and _served_now(original) else "active"
    if was != "conflicted":
        return "active"
    # A peer deleted or resolved while this row was retired leaves nothing to review.
    for peer_id in row.conflict_with:
        peer = await memory_repo.get_by_id(
            peer_id, org=row.org, ch=ch, scope=WHOLE_NAMESPACE, fresh=True, strong=True
        )
        if peer is None or peer.status != "conflicted":
            continue
        if str(row.memory_id) in peer.conflict_with:
            return "conflicted"
    return "active"


def _attested(row: MemoryRow) -> bool:
    """The row's attestation still covers its content (the embeddings `verified` flag)."""
    from server.memory.domain.trust import verification_state

    state = verification_state(row.verified_by, row.verified_sha256, row.content_sha256)
    return state in ("human", "process")


def _invalid_at_after_restore(existing: MemoryRow) -> datetime | None:
    """A tombstone that also stamped invalid_at (a summary that lost its last source)
    stamped both at once: undoing it clears both. A retirement (`superseded`) keeps
    its stamp, even one deleted in the same second."""
    retired = existing.status == "superseded"
    if not retired and existing.invalid_at == existing.tombstoned_at:
        return None
    return existing.invalid_at


async def _restore_embedding_mirror(
    ch: Any, input: Any, existing: MemoryRow, memory_type: str
) -> None:
    """Propagate a restore to memory_embeddings, for every memory type (delete's mirror).
    A 'high' row gets none: its key's last rows are a retired vector or a fence, and
    reviving either would serve it."""
    import server.memory.store as memory_repo

    if existing.sensitivity == "high":
        return

    # Vectors live only on the embeddings table (re-read here);
    # other columns mirror from `existing`. argMax keys on version
    # (the RMT merge key). `key_embedding_dim` is not named after its column: that
    # alias would be substituted into the argMaxIf conditions and nest an aggregate
    # (ILLEGAL_AGGREGATION, every restore). The key-vector pair uses argMaxIf to skip
    # rows holding the schema default ([], dim 0), which would
    # otherwise win as the latest version.
    existing_emb_rows = await ch.query_all(
        """
        SELECT
            argMax(embedding_model, version) AS embedding_model,
            argMax(embedding_dim, version)   AS embedding_dim,
            argMax(embedding, version)       AS embedding,
            argMax(content_excerpt, version) AS content_excerpt,
            argMaxIf(memory_key_embedding, version,
                     memory_key_embedding_dim != 0)
                AS memory_key_embedding,
            argMaxIf(memory_key_embedding_dim, version,
                     memory_key_embedding_dim != 0)
                AS key_embedding_dim
        FROM loom.memory_embeddings
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND user_id = {uid:String}
          AND session_id = {sid:String}
          AND memory_key = {k:String}
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "agent": input.agent,
            "uid": input.user_id,
            "sid": input.session_id,
            "k": input.memory_key,
        },
    )
    if existing_emb_rows:  # a dim-0 stub is restored like a vector
        emb = existing_emb_rows[0]
        # This row wins FINAL, so it must carry every persisted
        # column or the schema default shadows the prior value.
        restored_excerpt = str(emb.get("content_excerpt") or "") or build_excerpt(
            memory_repo.stored_value_text(existing), memory_key=input.memory_key
        )
        restore_emb_row = memory_repo.build_embedding_row(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            run_id=existing.run_id,
            memory_key=input.memory_key,
            memory_type=memory_type,
            scope="private",
            content_excerpt=restored_excerpt,
            embedding_model=emb["embedding_model"],
            embedding_dim=int(emb["embedding_dim"] or 0),
            embedding=list(emb["embedding"]),
            memory_key_embedding=list(emb.get("memory_key_embedding") or []),
            memory_key_embedding_dim=int(emb.get("key_embedding_dim") or 0),
            confidence=float(existing.confidence),
            tombstoned_at=None,
            created_at=naive_utc_now(),
            tags=list(existing.tags or []),
            entities=list(existing.entities or []),
            # Carried forward so restore doesn't zero them under FINAL.
            files=list(getattr(existing, "files", None) or []),
            content_sha256=str(getattr(existing, "content_sha256", "") or ""),
            concepts=list(getattr(existing, "concepts", None) or []),
            status=str(existing.status or "active"),
            temporal_anchor=existing.temporal_anchor,
            temporal_confidence=getattr(existing, "temporal_confidence", 0.0),
            valid_at=existing.valid_at,
            valid_to=existing.valid_to,
            invalid_at=_invalid_at_after_restore(existing),
            source_class=existing.source_class,
            verified=1 if _attested(existing) else 0,
            # Must match the loom.memory restore row's cleared
            # expiry or the row vanishes from search at the stale one.
            expires_at=None,
            reinforcement_count=existing.reinforcement_count,
            last_reinforced_at=existing.last_reinforced_at,
            mentioned_at=existing.mentioned_at,
            source_resource_id=existing.source_resource_id or "",
        )
        await ch.insert(
            "loom.memory_embeddings",
            [restore_emb_row],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )


async def handle_memory_restore(input: MemoryRestoreInput) -> MemoryRestoreOutput:
    """Undo a delete, or an automatic retirement.

    A delete within the 14-day grace window: copy the tombstoned row forward with a
    higher version and tombstoned_at=NULL so RMT collapses the pair live, on both
    loom.memory and loom.memory_embeddings. A tombstone past the 14-day TTL returns
    expired=True.

    A retired (`superseded`) row is served again by `_restore_superseded`, whose two
    preconditions hold it to the retirement on screen: `if_superseded_by` (still
    retired behind that memory) and `only_if_superseded` (still retired, never a delete
    since). Either one routes here.

    Idempotent: an already-live row returns restored=False, unless it is an Undo's restore
    (whose answer may have been lost): the call finishes it, again if need be, and answers
    restored.
    """
    import server.memory.store as memory_repo

    ch = get_client()
    row = await ch.query_one(
        """
        SELECT memory_key, memory_type, value, confidence, source_trace,
               memory_id, entities, tags, valid_at, char_limit, pinned,
               tombstoned_at, version, status, superseded_by
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND user_id = {uid:String}
          AND session_id = {sid:String}
          AND memory_key = {k:String}
        LIMIT 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "agent": input.agent,
            "uid": input.user_id,
            "sid": input.session_id,
            "k": input.memory_key,
        },
    )
    if row is None:
        return MemoryRestoreOutput(restored=False)
    live = row.get("tombstoned_at") is None
    memory_id, version = str(row.get("memory_id") or ""), int(row.get("version") or 0)
    guarded = input.if_superseded_by or input.only_if_superseded
    retired = live and str(row.get("status") or "") == "superseded"
    if guarded or retired or (live and await _an_undos_restore(input, memory_id, version)):
        # Undo of an automatic retirement, or the finish of one whose answer was lost:
        # `_restore_superseded` rechecks on the strong head, under the key's lock.
        return await _restore_superseded(input, ch)
    if live:
        # Already live, and no Undo's restore: an idempotent no-op.
        return MemoryRestoreOutput(restored=False, memory_id=memory_id, version=version)

    # Past the retention TTL the data is gone — report expired explicitly
    # so the UI shows "outside the grace window" not a generic 404.
    tombstoned_at = row["tombstoned_at"]
    if tombstoned_at is not None and tombstoned_at.tzinfo is None:
        tombstoned_at = tombstoned_at.replace(tzinfo=UTC)
    if tombstoned_at is not None:
        age = datetime.now(UTC) - tombstoned_at
        if age.days >= 14:
            return MemoryRestoreOutput(restored=False, expired=True)

    # The key's write lock, as the Undo of a retirement takes it: a conditional revision
    # holds it from its read to its insert, and this must not write over that revision.
    async with _undo_lock(input):
        new_version = next_version()
        # Full-column get_by_key so the restore mirrors every persisted column.
        restore_identity = memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
        )
        existing = await memory_repo.get_by_key(
            restore_identity,
            input.memory_key,
            include_tombstoned=True,
            fresh=True,
            strong=True,  # restore follows delete; read the latest head (no miss)
            scope=WHOLE_NAMESPACE,
        )
        if existing is None or existing.tombstoned_at is None:
            # Race: tombstone vanished between the SELECT above and here.
            return MemoryRestoreOutput(
                restored=False,
                memory_id=str(row.get("memory_id") or ""),
                version=int(row.get("version") or 0),
            )
        restore_row = memory_repo.build_memory_version_row(
            existing,
            version=new_version,
            updated_at=memory_repo.written_now(),
            tombstoned_at=None,
            invalid_at=_invalid_at_after_restore(existing),
            expires_at=None,
        )
        try:
            await insert_memory_versions([restore_row], ch=ch)
        except Exception as e:  # an insert whose answer was lost may have landed
            if not await _restore_landed(input, new_version):
                raise BackendUnavailableError(
                    "the restore may not have landed; retry it",
                    operation="memory.restore",
                    outcome_unknown=True,
                ) from e
        if existing.sensitivity == "high":
            # The key is high again: a plain write still embedding lands below the fence.
            from server.memory.app.high_fence import fence_high

            await fence_high([(memory_repo.embedding_key(existing), new_version)], ch=ch)

        # Propagate restore to memory_embeddings — mirror delete's logic, for every memory type.
        try:
            await _restore_embedding_mirror(ch, input, existing, row["memory_type"])
        except Exception as e:  # noqa: BLE001 — the memory row restore already landed; mirror is best-effort
            logging.getLogger(__name__).warning(
                "memory_embeddings restore failed (non-fatal): %s",
                e,
            )

    # A fact the row states may have closed while it was deleted.
    await _reopen_facts(input, existing, new_version, ch=ch)

    # If this row was tombstoned inside an erasure's scope + time window,
    # record the restore as a reversal so the audit log stops claiming
    # "completed". Match is generous (scope overlap) — better to over-count
    # than to silently lie in the audit trail.
    try:
        scope_match = (
            "(scope_type = 'namespace' AND scope_value = {ns:String})"
            " OR (scope_type = 'user_id' AND scope_value = {uid:String})"
        )
        matching = await ch.query_all(
            f"""
            SELECT id AS erasure_id, scope_type, scope_value
            FROM loom.erasure_requests FINAL
            WHERE org = {{org:String}}
              AND ({scope_match})
              AND requested_at <= {{tombstoned_at:DateTime}}
              AND (completed_at IS NULL
                   OR completed_at >= {{tombstoned_at:DateTime}} - INTERVAL 1 HOUR)
            ORDER BY requested_at DESC
            LIMIT 5
            """,
            params={
                "org": input.org,
                "ns": input.namespace,
                "uid": input.user_id,
                "tombstoned_at": (tombstoned_at.replace(tzinfo=None) if tombstoned_at else None),
            },
        )
        if matching:
            await ch.insert(
                "loom.erasure_reversals",
                [
                    {
                        "erasure_id": str(m["erasure_id"]),
                        "org": input.org,
                        "scope_type": str(m["scope_type"]),
                        "scope_value": str(m["scope_value"]),
                        "memory_id": str(row.get("memory_id") or ""),
                        "memory_key": input.memory_key,
                        "restored_by": writer_actor(),
                    }
                    for m in matching
                ],
            )
    except Exception as e:  # noqa: BLE001 — audit insert failure must not undo the restore
        logging.getLogger(__name__).warning(
            "erasure-reversal audit insert failed (non-fatal): %s",
            e,
        )

    return MemoryRestoreOutput(
        restored=True,
        from_status="tombstoned",
        memory_id=str(row.get("memory_id") or ""),
        version=new_version,
    )
