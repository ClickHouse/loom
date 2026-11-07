"""memory.restore: bring a tombstoned or superseded row back as a new live
version, with its search document."""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core import pg
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.error_summary import error_summary
from server.core.errors import BackendUnavailableError, LoomServerError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import (
    MemoryRestoreInput,
    MemoryRestoreOutput,
)
from server.memory.app import webhooks
from server.memory.app.provenance import writer_actor
from server.memory.domain.excerpt import build_excerpt
from server.memory.domain.temporal_sql import SERVED_STATUSES
from server.memory.store import key_write_lock, pg_memory
from server.memory.store.rows import identity_key, next_version, version_over, version_time
from server.observability.budget_shape import note_budget_shape

if TYPE_CHECKING:
    from server.memory.store import MemoryRow

log = logging.getLogger(__name__)


async def _read_restorable(input: MemoryRestoreInput) -> MemoryRow | None:
    """The key's head, deleted or not, every persisted column of it (the restore copies
    them all), read strong: it follows the delete or retirement it undoes, so it must not
    miss it."""
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


class _DocumentFailed(Exception):
    """A committed restore's search document failed: the commit owes it only for an
    embedded row, which the delivery worker then builds."""


async def _mirror_landed(what: str, memory_key: str, mirror: Callable[[], Awaitable[None]]) -> None:
    """A landed restore's search document, mirrored once its row is in."""
    try:
        await mirror()
    except Exception as e:  # the row is in: raised to its commit under Postgres, else logged
        if pg.enabled():
            raise _DocumentFailed(error_summary(e)) from e
        log.warning(
            "memory.restore: %s's search document failed for %s: %s",
            what,
            memory_key,
            error_summary(e),
        )


async def _insert_restore(
    input: MemoryRestoreInput,
    row: dict[str, Any],
    existing: MemoryRow,
    *,
    ch: Any,
    what: str,
    mirror: Callable[[], Awaitable[None]],
    landed: Callable[[], Awaitable[None]] | None = None,
    on_commit: Callable[[], None] | None = None,
) -> None:
    """Write the restore's row over the head it was built from (`existing`), then its
    search document (`mirror`), under Postgres within the commit's lease; `landed` runs
    once the row is in. Under Postgres an embedded row owes its document until the
    mirror lands it, so the delivery worker builds one the mirror did not. A refusal, a
    contended head or Postgres down before the commit is final; any other failure may
    have landed, so the key is read again."""
    import server.memory.store as memory_repo

    key = str(row["memory_key"])
    owes_document = pg_memory.embedded(row)
    mirrored = functools.partial(_mirror_landed, what, key, mirror)
    try:
        await memory_repo.insert_versions_over_heads(
            [row],
            [existing],
            ch=ch,
            then=mirrored,
            landed=landed,
            documented=[owes_document],
            on_commit=on_commit,
        )
    except _DocumentFailed as failed:  # it carries its cause's summary
        outcome = "is left owed" if owes_document else "failed"
        log.warning(
            "memory.restore: %s's search document %s for %s: %s", what, outcome, key, failed
        )
    except Exception as e:  # a lost answer is checked, not assumed
        refused = isinstance(e, LoomServerError) and (
            e.status_code < 500
            or isinstance(e, pg_memory.AdmissionContendedError | pg.PostgresRefusedError)
            or (isinstance(e, pg.PostgresUnavailableError) and not pg.may_have_committed(e))
        )
        if refused:
            raise
        try:
            landed_now = await _restore_landed(input, row)
        except asyncio.CancelledError as cancelled:
            # Its commit may have landed: `fenced_once_committed` reads the mark.
            pg.mark_commit_in_flight(cancelled)
            raise
        if landed_now is None:
            raise BackendUnavailableError(
                f"{what} may not have landed; retry {what}",
                operation="memory.restore",
                outcome_unknown=True,
            ) from e
        if not landed_now:  # settled: the authority's head never took it
            raise BackendUnavailableError(
                f"{what} did not land; retry {what}", operation="memory.restore"
            ) from e
        # Landed with the answer of its insert, commit or publish lost: `landed` has not
        # run, and under Postgres an embedded row's commit owes the document.
        log.warning(
            "memory.restore: %s of %s landed with its answer lost: %s",
            what,
            key,
            error_summary(e),
        )
        if on_commit is not None:
            on_commit()
        if landed is not None:
            await landed()
        if not pg.enabled():
            await mirrored()


async def _key_head(input: MemoryRestoreInput, ch: Any) -> dict[str, Any] | None:
    """The key's head, deleted or not, that routes a restore, from the authority: under
    Postgres a delete not yet delivered to ClickHouse is restored."""
    if pg.enabled():
        head = await _read_restorable(input)
        return None if head is None else head.model_dump()
    row: dict[str, Any] | None = await ch.query_one(
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
    return row


def _key_lock(input: MemoryRestoreInput, what: str) -> AbstractAsyncContextManager[None]:
    """The key's write lock for a restore (`key_write_lock.key_lock_for`); `what` names it
    in the answer when the lock is busy."""
    return key_write_lock.key_lock_for(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_key,
        what=what,
        operation="memory.restore",
    )


async def _restore_superseded(input: MemoryRestoreInput, ch: Any) -> MemoryRestoreOutput:
    # A conditional revision holds the key's write lock from its read to its insert; the
    # Undo does too, or it could write the retired value back over that revision.
    async with _key_lock(input, "the Undo"):
        existing = await _read_restorable(input)
        if existing is None:
            return MemoryRestoreOutput(restored=False)
        if await _landed_late(input, existing):
            # A retry of an Undo whose restore landed after its 503: finish that one.
            version, status = int(existing.version), str(existing.status)
            if not pg.enabled():  # under Postgres the commit owes an embedded row's document
                await _mirror_landed(
                    "the Undo",
                    input.memory_key,
                    functools.partial(_unsupersede, input, status, ch=ch),
                )
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
    given). A record that cannot be read is a 503: the Undo may have landed."""
    if head.tombstoned_at is not None or str(head.status) == "superseded":
        return False
    try:
        return await _an_undos_restore(
            input, str(head.memory_id), int(head.version), input.if_superseded_by
        )
    except Exception as e:  # unknown: neither a no-op nor a finish is the true answer
        raise BackendUnavailableError(
            "could not tell whether the Undo landed; retry it",
            operation="memory.restore",
            outcome_unknown=True,
        ) from e


async def _an_undos_restore(
    input: MemoryRestoreInput, memory_id: str, version: int, superseded_by: str = ""
) -> bool:
    """Whether the row at `version` is the restore an Undo record names (behind
    `superseded_by` when given): an Undo whose answer was lost, or one already finished."""
    from server.memory.store.conflict_resolver import undo_recorded

    return await undo_recorded(input.org, input.namespace, memory_id, version, superseded_by)


async def _plain_restore_finishes_an_undo(
    input: MemoryRestoreInput, memory_id: str, version: int
) -> bool:
    """`_an_undos_restore` for an unguarded restore of a live row, before any lock: a
    record that cannot be read leaves it a plain no-op."""
    try:
        return await _an_undos_restore(input, memory_id, version)
    except Exception as e:  # noqa: BLE001 — an unread record is no restore to finish
        log.warning(
            "memory.restore: Undo record read failed for %s: %s", memory_id, error_summary(e)
        )
        return False


async def _restore_live(
    input: MemoryRestoreInput, row: dict[str, Any], *, ch: Any
) -> MemoryRestoreOutput:
    """A restore of a live row with no Undo to finish: a no-op, unless an undelete whose
    answer and re-read were both lost left its search row deleted, which comes back now
    as the undelete would have brought it (the mirror is idempotent). Under Postgres the
    commit of an embedded row's undelete owes its search document, so the delivery worker
    brings it back instead."""
    memory_id, version = str(row.get("memory_id") or ""), int(row.get("version") or 0)
    if not pg.enabled() and await _undelete_left_unmirrored(input, ch=ch):
        existing = await _read_restorable(input)
        # A high or redacted row has no search row to bring back.
        if (
            existing is not None
            and existing.tombstoned_at is None
            and existing.sensitivity != "high"
            and existing.redacted_at is None
        ):
            await _restore_embedding_mirror(
                ch, input, existing, row["memory_type"], version=existing.version
            )
            return MemoryRestoreOutput(
                restored=True,
                memory_id=memory_id,
                version=int(existing.version),
                from_status="tombstoned",
            )
    return MemoryRestoreOutput(restored=False, memory_id=memory_id, version=version)


async def _undelete_left_unmirrored(input: MemoryRestoreInput, *, ch: Any) -> bool:
    """Whether the key's newest version is an undelete (the one before it deleted) while
    its newest search row is still deleted."""
    params = {
        "org": input.org,
        "ns": input.namespace,
        "a": input.agent,
        "u": input.user_id,
        "s": input.session_id,
        "k": input.memory_key,
    }
    identity = """org = {org:String} AND namespace = {ns:String} AND agent = {a:String}
          AND user_id = {u:String} AND session_id = {s:String} AND memory_key = {k:String}"""
    found = await ch.query_all(
        f"""
        SELECT
            (SELECT arraySort(x -> -x.1, groupArray((version, tombstoned_at IS NOT NULL)))
             FROM loom.memory_versions WHERE {identity}) AS history,
            (SELECT argMax(tombstoned_at IS NOT NULL, version)
             FROM loom.memory_embeddings WHERE {identity}) AS search_deleted
        SETTINGS select_sequential_consistency = 1
        """,
        params,
    )
    if not found:
        return False
    history = found[0]["history"] or []
    undeleted = len(history) >= 2 and not history[0][1] and bool(history[1][1])
    return undeleted and bool(found[0]["search_deleted"])


async def _write_restore(
    input: MemoryRestoreInput, existing: MemoryRow, *, ch: Any
) -> tuple[int, str]:
    """Write the Undo's record, then its restore version; (that version, the status it
    restores). Under the key's lock."""
    import server.memory.store as memory_repo
    from server.observability import audit

    new_version = version_over(existing)
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
    note_budget_shape("undo")
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
    mirror = functools.partial(_unsupersede, input, status, ch=ch)
    await _insert_restore(input, restore_row, existing, ch=ch, what="the Undo", mirror=mirror)
    return new_version, status


async def _unsupersede(input: MemoryRestoreInput, status: str, *, ch: Any) -> None:
    """Serve the key's search rows again, as `status`."""
    import server.memory.store as memory_repo

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


async def _restore_landed(input: MemoryRestoreInput, row: dict[str, Any]) -> bool | None:
    """Whether the key's head is the restore's `row`, read again after its insert failed:
    the insert may have landed with its answer lost. None when that is unknown. Under
    Postgres the read waits for a commit still holding the head (`settled_head_versions`),
    and a head below the row settles that it did not land: heads only move up. One above
    it may be a later write over a restore that landed. ClickHouse never settles a miss."""
    version = int(row["version"])
    try:
        if pg.enabled():
            key = identity_key(row)
            head = (await pg_memory.settled_head_versions([key])).get(key)
            if head is None or head > version:
                return None
            return head == version
        found = await _read_restorable(input)
    except Exception as e:  # noqa: BLE001 — best effort: an unknown outcome answers the 503
        log.warning("memory.restore: re-read after a failed insert failed: %s", error_summary(e))
        return None
    if found is not None and int(found.version) == version:
        return True
    return None


async def _reopen_facts(
    input: MemoryRestoreInput, existing: MemoryRow, version: int, *, ch: Any
) -> None:
    """Open again the facts the restored row states, dated at the restore (`version`):
    while it was retired or deleted, another row's revision or delete may have closed
    them. Named by the restore, so finishing it again writes nothing new."""
    from server.memory.app.triple_projections import reconcile_row_facts

    restore = f"{existing.memory_id}:{version}"
    # No failure payload names a 'high' row's content: its reopen reads the row as it is.
    hashes = (
        {}
        if existing.sensitivity == "high"
        else {str(existing.memory_id): str(existing.content_sha256)}
    )
    await reconcile_row_facts(
        input,
        [str(existing.memory_id)],
        reopen=True,
        op="memory.restore",
        ch=ch,
        hashes=hashes,
        # Versions are time_ns: the restore's version is when it was made (or just above a
        # head from a clock ahead), the same on its first write and every retry.
        restored_at=version_time(version),
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

    was = await memory_repo.status_before(row, ch=ch)
    if was == "duplicate" and row.duplicate_of:
        # Still hidden behind the row it repeats, unless that one is gone.
        original = await memory_repo.get_by_id(
            row.duplicate_of,
            org=row.org,
            namespace=row.namespace,
            ch=ch,
            scope=WHOLE_NAMESPACE,
            fresh=True,
            strong=True,
        )
        # Reads only stand the original in for it while it is served and not ended.
        return "duplicate" if original is not None and _served_now(original) else "active"
    if was != "conflicted":
        return "active"
    # A peer deleted or resolved while this row was retired leaves nothing to review.
    for peer_id in row.conflict_with:
        peer = await memory_repo.get_by_id(
            peer_id,
            org=row.org,
            namespace=row.namespace,
            ch=ch,
            scope=WHOLE_NAMESPACE,
            fresh=True,
            strong=True,
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
    ch: Any, input: Any, existing: MemoryRow, memory_type: str, *, version: int
) -> None:
    """Propagate a restore to memory_embeddings, for every memory type (delete's mirror),
    at the restore's `version`, which is above the delete's search tombstone.
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
            version=version,
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
    row = await _key_head(input, ch)
    if row is None:
        return MemoryRestoreOutput(restored=False)
    live = row.get("tombstoned_at") is None
    memory_id, version = str(row.get("memory_id") or ""), int(row.get("version") or 0)
    guarded = input.if_superseded_by or input.only_if_superseded
    retired = live and str(row.get("status") or "") == "superseded"
    if (
        guarded
        or retired
        or (live and await _plain_restore_finishes_an_undo(input, memory_id, version))
    ):
        # Undo of an automatic retirement, or the finish of one whose answer was lost:
        # `_restore_superseded` rechecks on the strong head, under the key's lock.
        return await _restore_superseded(input, ch)
    if live:
        return await _restore_live(input, row, ch=ch)

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
    async with _key_lock(input, "the restore"):
        # Drawn before the read and lifted above the head read below (`version_over`): a
        # write drawing a version above the restore's outranks it.
        drawn = next_version()
        existing = await _read_restorable(input)
        if existing is None or existing.tombstoned_at is None:
            # Race: tombstone vanished between the SELECT above and here.
            return MemoryRestoreOutput(
                restored=False,
                memory_id=str(row.get("memory_id") or ""),
                version=int(row.get("version") or 0),
            )
        if existing.redacted_at is not None:
            # Deleted, then redacted: a restore would bring back only the placeholder.
            return MemoryRestoreOutput(
                restored=False,
                memory_id=str(existing.memory_id),
                version=int(existing.version),
                redacted=True,
            )
        new_version = version_over(existing, drawn)
        restore_row = memory_repo.build_memory_version_row(
            existing,
            version=new_version,
            updated_at=memory_repo.written_now(),
            tombstoned_at=None,
            invalid_at=_invalid_at_after_restore(existing),
            expires_at=None,
        )
        # Its search document mirrors the delete's logic, for every memory type.
        mirror = functools.partial(
            _restore_embedding_mirror, ch, input, existing, row["memory_type"], version=new_version
        )

        # Announced once the row is live, before anything else can fail: a retry finds
        # the key live and restores (and announces) nothing.
        async def _announce() -> None:
            if await webhooks.announce([("memory.restored", restore_row)]):
                note_budget_shape("webhook")

        from server.memory.app.high_fence import fence_high, fenced_once_committed

        # The key is high again: a plain write still embedding lands below the fence.
        fence = [(identity_key(existing), new_version)] if existing.sensitivity == "high" else []
        async with fenced_once_committed(lambda: fence, op="memory.restore", ch=ch) as answer:
            await _insert_restore(
                input,
                restore_row,
                existing,
                ch=ch,
                what="the restore",
                mirror=mirror,
                landed=_announce,
                on_commit=answer.mark_committed,
            )
        if fence and not pg.enabled():
            await fence_high(fence, ch=ch)

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
        log.warning("erasure-reversal audit insert failed (non-fatal): %s", error_summary(e))

    return MemoryRestoreOutput(
        restored=True,
        from_status="tombstoned",
        memory_id=str(row.get("memory_id") or ""),
        version=new_version,
    )
