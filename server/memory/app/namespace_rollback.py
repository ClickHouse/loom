"""The memory side of namespace.rollback: the version-history reads the plan is made from,
and the writes that apply it (reinstated heads re-emitted through the version-row builder,
removed keys tombstoned, one stub embedding row per touched key for the embedding worker)."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import server.memory.store as store
from server.core import pg
from server.memory.app import webhooks
from server.memory.domain import rollback as svc
from server.memory.domain.webhook_events import EventType
from server.memory.store import pg_memory

log = logging.getLogger(__name__)

_INSERT_BATCH = 500


async def wait_published(org: str, namespace: str) -> None:
    """Under Postgres, until ClickHouse holds every memory row the namespace committed
    (503 PROJECTION_BEHIND past the wait): a write answers at its commit, and the plan
    reads ClickHouse's history."""
    from server.memory.app.search import pg_validation

    await pg_validation.admit(org, [namespace], owed=pg_memory.MEMORY_ROW)


async def earliest_version(org: str, namespace: str, *, ch: Any) -> int:
    """Oldest version stamp the history holds for the namespace; 0 when none."""
    with _history_reads():
        return await store.earliest_version_in_namespace(org, namespace, ch=ch)


def _history_reads() -> contextlib.AbstractContextManager[None]:
    """Under Postgres, from a replica that has what was just published."""
    from server.core.ch import sequential_reads

    return sequential_reads() if pg.enabled() else contextlib.nullcontext()


async def change_rows(
    org: str,
    namespace: str,
    version_at: int,
    *,
    memory_keys: list[str] | None,
    limit: int,
    ch: Any,
) -> list[dict[str, Any]]:
    """Keys whose head differs from their head as of `version_at` (up to `limit + 1`)."""
    with _history_reads():
        return await store.rollback_change_rows(
            org, namespace, version_at, memory_keys=memory_keys, limit=limit, ch=ch
        )


async def apply_rollback(
    org: str,
    namespace: str,
    plan: svc.RollbackPlan,
    *,
    applied_at: datetime,
    ch: Any,
    applied: Applied,
) -> None:
    """Re-insert reinstated heads and tombstone removed ones, a batch at a time over the
    heads read just before it (`_write`), with a stub embedding row per touched key for the
    worker to drain. A row that is not embedded ('high', or a non-searchable type) gets no
    stub: its excerpt would carry the value into search. Counts in `applied` what landed,
    batch by batch: a key erased since the plan is not reinstated, and one deleted since
    is not removed again."""
    from server.embedding import get_embedder
    from server.memory.domain.excerpt import build_excerpt
    from server.memory.domain.memory_types import is_embedded
    from server.memory.domain.trust import attests

    embedder = get_embedder()
    # Empty: the insert gives it the width its coded column takes.
    stub_vector: list[float] = []

    def _stub(
        row: store.MemoryRow, *, version: int, tombstoned_at: datetime | None
    ) -> dict[str, Any]:
        serialized = row.value if isinstance(row.value, str) else json.dumps(row.value, default=str)
        return store.build_embedding_row(
            org=row.org,
            namespace=row.namespace,
            agent=row.agent,
            user_id=row.user_id,
            session_id=row.session_id,
            run_id=row.run_id or "-",
            memory_key=row.memory_key,
            memory_type=row.memory_type,
            scope="private",
            content_excerpt=build_excerpt(serialized, memory_key=row.memory_key),
            embedding_model=embedder.model_name,
            embedding_dim=0,
            embedding=stub_vector,
            tombstoned_at=tombstoned_at,
            created_at=applied_at,
            # The new memory row's version, so a concurrent high write's fence outranks it.
            version=version,
            tags=list(row.tags or []),
            entities=list(row.entities or []),
            status=str(row.status or "active"),
            confidence=float(row.confidence),
            temporal_anchor=row.temporal_anchor,
            temporal_confidence=float(row.temporal_confidence or 0.0),
            valid_at=row.valid_at,
            valid_to=row.valid_to,
            invalid_at=row.invalid_at,
            reinforcement_count=int(getattr(row, "reinforcement_count", 1) or 1),
            source_resource_id=getattr(row, "source_resource_id", "") or "",
            source_class=getattr(row, "source_class", "") or "",
            expires_at=row.expires_at,
            files=list(getattr(row, "files", None) or []),
            concepts=list(getattr(row, "concepts", None) or []),
            # The reinstated version's attestation, if it still binds its content.
            verified=int(
                attests(
                    getattr(row, "verified_by", ""),
                    getattr(row, "verified_sha256", ""),
                    getattr(row, "content_sha256", ""),
                )
            ),
        )

    # --- reinstate: the historical row becomes the head again ---------------
    for i in range(0, len(plan.reinstate), _INSERT_BATCH):
        batch: list[_Planned] = []
        for h, head in await _reinstated(
            org, namespace, plan.reinstate[i : i + _INSERT_BATCH], ch=ch
        ):
            if head is None:
                continue  # erased since the plan: a rollback never brings it back
            # Above the key's head now, a delete included: one whose writer's clock runs
            # ahead does not outrank the reinstated version.
            new_version = store.version_over(head)
            row = store.build_memory_version_row(
                h,
                version=new_version,
                updated_at=applied_at,
                # A tombstone as it was at T, a sweep's or a delete_version's marker too
                # (the builder drops them from every other version).
                **(
                    {"expire_reason": h.expire_reason, "source_trace": h.source_trace}
                    if h.tombstoned_at
                    else {}
                ),
            )
            # A version the authority holds redacted serves no search document.
            embedded = is_embedded(h.memory_type, h.sensitivity) and h.redacted_at is None
            stub = (
                _stub(h, version=new_version, tombstoned_at=h.tombstoned_at) if embedded else None
            )
            source = None if h.redacted_at else (str(h.memory_id), h.version)
            batch.append(_Planned(row, head, _reinstate_event(h, head), stub, source))
        await _write(batch, ch=ch, landed=applied.add_reinstated)

    # --- remove: keys born after as_of are tombstoned ----------------------
    for i in range(0, len(plan.remove), _INSERT_BATCH):
        # Their live heads now, read current as the reinstated ones are; one gone is left.
        part = plan.remove[i : i + _INSERT_BATCH]
        heads = await store.memory_rows_by_identity_keys(
            [(org, namespace, a.agent, a.user_id, a.session_id, a.memory_key) for a in part],
            ch=ch,
            sequential=True,
        )
        batch = []
        for head in heads.values():
            new_version = store.version_over(head)
            row = store.build_memory_version_row(
                head, version=new_version, tombstoned_at=applied_at, updated_at=applied_at
            )
            embedded = is_embedded(head.memory_type, head.sensitivity)
            stub = _stub(head, version=new_version, tombstoned_at=applied_at) if embedded else None
            batch.append(_Planned(row, head, "memory.tombstoned", stub))
        await _write(batch, ch=ch, landed=applied.add_removed)

    log.info(
        "namespace.rollback applied org=%s ns=%s reinstate=%d remove=%d",
        org,
        namespace,
        applied.reinstated,
        applied.removed,
    )


@dataclass
class Applied:
    """The keys a rollback reinstated and removed so far."""

    reinstated: int = 0
    removed: int = 0

    def add_reinstated(self, n: int) -> None:
        self.reinstated += n

    def add_removed(self, n: int) -> None:
        self.removed += n


@dataclass(frozen=True, slots=True)
class _Planned:
    """One key's rollback write: the row, the head it is written over, its webhook event
    and its search stub (None for a row that is not embedded, or is held redacted)."""

    row: dict[str, Any]
    head: store.MemoryRow
    event: EventType
    stub: dict[str, Any] | None
    # The revision reinstated, (memory_id, version), when it held its text as read: a
    # redaction of it since refuses the commit.
    source: tuple[str, int] | None = None


async def _write(batch: list[_Planned], *, ch: Any, landed: Callable[[int], None]) -> None:
    """Commit the batch over its heads; `landed` counts and announces it, `_follow_ups`
    retires and fences (ClickHouse) and inserts its stubs; under Postgres the publish
    retires the vectors and `fenced_once_committed` fences the batch's 'high' rows."""
    from server.memory.app.high_fence import fence_high, fenced_once_committed

    unembedded = [p.row for p in batch if p.stub is None]
    fence = [
        (store.identity_key(r), int(r["version"])) for r in unembedded if r["sensitivity"] == "high"
    ]

    async def _announce() -> None:
        landed(len(batch))
        await webhooks.announce([(p.event, p.row) for p in batch])

    async def _follow_ups() -> None:
        # The retirements and fences first: a failed stub insert leaves no 'high' key served.
        if not pg.enabled():
            retire = [(store.identity_key(r), int(r["version"])) for r in unembedded]
            try:
                await store.retire_vectors_for_keys(retire, ch=ch)
            finally:  # at the same version, the later insert wins the tie
                await fence_high(fence, ch=ch)
        stubs = [p.stub for p in batch if p.stub is not None]
        if stubs:
            await store.insert_embedding_rows(stubs, ch=ch)

    async with fenced_once_committed(lambda: fence, op="namespace.rollback", ch=ch) as answer:
        await store.insert_versions_over_heads(
            [p.row for p in batch],
            [p.head for p in batch],
            ch=ch,
            then=_follow_ups,
            landed=_announce,
            # A reinstated live row's document is owed until its stub lands.
            documented=[p.stub is not None and p.row["tombstoned_at"] is None for p in batch],
            sources=[p.source for p in batch if p.source is not None],
            on_commit=answer.mark_committed,
        )


async def _reinstated(
    org: str, namespace: str, actions: list[svc.RollbackAction], *, ch: Any
) -> list[tuple[store.MemoryRow, store.MemoryRow | None]]:
    """The historical rows `actions` reinstate, each with its key's head now, a delete
    included."""
    wanted = {(a.agent, a.user_id, a.session_id, a.memory_key, a.version_to) for a in actions}
    pairs = [(a.memory_key, a.version_to) for a in actions]
    historical = [
        h
        for h in await store.fetch_version_rows(org, namespace, pairs, ch=ch)
        if (h.agent, h.user_id, h.session_id, h.memory_key, h.version) in wanted
    ]
    if pg.enabled():
        # As the authority holds them: a redaction scrubs Postgres before ClickHouse's
        # copies, and an erasure removes them.
        held = await pg_memory.revisions_at(org, namespace, historical)
        historical = [
            held[k]
            for h in historical
            if (k := (str(h.memory_id), h.version)) in held
            and store.identity_key(held[k]) == store.identity_key(h)
        ]
    heads = await store.memory_rows_by_identity_keys(
        [store.identity_key(h) for h in historical],
        ch=ch,
        sequential=True,
        include_tombstoned=True,
    )
    return [(h, heads.get(store.identity_key(h))) for h in historical]


def _reinstate_event(historical: store.MemoryRow, head: store.MemoryRow) -> EventType:
    """What reinstating `historical` over the key's `head` is to a webhook: back to a
    tombstone, a revival of a key deleted now, or a revision."""
    if historical.tombstoned_at:
        return "memory.tombstoned"
    return "memory.restored" if head.tombstoned_at else "memory.revised"
