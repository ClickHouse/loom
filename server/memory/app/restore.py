"""memory.restore: bring a tombstoned or superseded row back as a new live
version, mirrored onto its embedding row."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import (
    MemoryRestoreInput,
    MemoryRestoreOutput,
)
from server.memory.domain.excerpt import build_excerpt
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import next_version

if TYPE_CHECKING:
    from server.memory.store import MemoryRow

log = logging.getLogger(__name__)


async def _restore_superseded(input: MemoryRestoreInput, ch: Any) -> MemoryRestoreOutput:
    import server.memory.store as memory_repo
    from server.observability import audit

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    existing = await memory_repo.get_by_key(
        identity,
        input.memory_key,
        include_tombstoned=True,
        fresh=True,
        strong=True,
        scope=WHOLE_NAMESPACE,
    )
    if (
        existing is None
        or existing.tombstoned_at is not None
        or str(existing.status) != "superseded"
        or (input.if_superseded_by and existing.superseded_by != input.if_superseded_by)
    ):
        if existing is None:
            return MemoryRestoreOutput(restored=False)
        return MemoryRestoreOutput(
            restored=False, memory_id=str(existing.memory_id), version=int(existing.version)
        )
    new_version = next_version()
    restore_row = memory_repo.build_memory_version_row(
        existing,
        version=new_version,
        updated_at=memory_repo.written_now(),
        status="active",
        superseded_by="",
        invalid_at=None,
    )
    await insert_memory_versions([restore_row], ch=ch)
    try:
        await memory_repo.unsupersede_embeddings(
            input.org,
            input.namespace,
            input.agent,
            input.user_id,
            input.session_id,
            input.memory_key,
            ch=ch,
        )
    except Exception:
        log.warning(
            "memory.restore: embeddings re-emit failed for %s",
            input.memory_key,
            exc_info=True,
        )
    # The facts the retirement closed and the restored value still holds come back.
    from server.memory.app.entity_projections import reconcile_row_facts

    await reconcile_row_facts(
        input,
        [str(existing.memory_id)],
        reopen=True,
        op="memory.restore",
        ch=ch,
        hashes={str(existing.memory_id): str(existing.content_sha256)},
    )
    await audit.emit(
        "memory.unsupersede",
        org=input.org,
        namespace=input.namespace,
        actor=getattr(input, "actor", "") or "-",
        subject={
            "memory_key": input.memory_key,
            "memory_id": str(existing.memory_id),
            "superseded_by": str(existing.superseded_by or ""),
        },
    )
    return MemoryRestoreOutput(
        restored=True,
        memory_id=str(existing.memory_id),
        version=new_version,
        from_status="superseded",
    )


def _attested(row: MemoryRow) -> bool:
    """The row's attestation still covers its content (the embeddings `verified` flag)."""
    from server.memory.domain.trust import verification_state

    state = verification_state(row.verified_by, row.verified_sha256, row.content_sha256)
    return state in ("human", "process")


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
            concepts=list(getattr(existing, "concepts", None) or []),
            status=str(existing.status or "active"),
            temporal_anchor=existing.temporal_anchor,
            temporal_confidence=getattr(existing, "temporal_confidence", 0.0),
            valid_at=existing.valid_at,
            valid_to=existing.valid_to,
            invalid_at=existing.invalid_at,
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
    """Undo a tombstone within the 14-day grace window: copy the
    tombstoned row forward with a higher version and tombstoned_at=NULL so
    RMT collapses the pair live. Mirrors the delete inverse on both
    loom.memory and loom.memory_embeddings.

    Idempotent — an already-live row returns restored=False; a tombstone
    past the 14-day TTL returns expired=True.
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
    if (
        input.if_superseded_by
        or input.only_if_superseded
        or (row.get("tombstoned_at") is None and str(row.get("status") or "") == "superseded")
    ):
        # Undo of an automatic retirement; `_restore_superseded` rechecks on the strong head.
        return await _restore_superseded(input, ch)
    if row.get("tombstoned_at") is None:
        # Already live — idempotent no-op.
        return MemoryRestoreOutput(
            restored=False,
            memory_id=str(row.get("memory_id") or ""),
            version=int(row.get("version") or 0),
        )

    # Past the retention TTL the data is gone — report expired explicitly
    # so the UI shows "outside the grace window" not a generic 404.
    tombstoned_at = row["tombstoned_at"]
    if tombstoned_at is not None and tombstoned_at.tzinfo is None:
        tombstoned_at = tombstoned_at.replace(tzinfo=UTC)
    if tombstoned_at is not None:
        age = datetime.now(UTC) - tombstoned_at
        if age.days >= 14:
            return MemoryRestoreOutput(restored=False, expired=True)

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
        expires_at=None,
    )
    await insert_memory_versions([restore_row], ch=ch)
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
                        "restored_by": getattr(input, "actor", "") or "",
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
