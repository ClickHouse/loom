"""The memory side of namespace.rollback: the version-history reads the plan is made from,
and the writes that apply it (reinstated heads re-emitted through the version-row builder,
removed keys tombstoned, one stub embedding row per touched key for the embedding worker)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import server.memory.store as store
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain import rollback as svc
from server.memory.store.inserts import insert_memory_versions

log = logging.getLogger(__name__)

_INSERT_BATCH = 500


async def earliest_version(org: str, namespace: str, *, ch: Any) -> int:
    """Oldest version stamp the history holds for the namespace; 0 when none."""
    return await store.earliest_version_in_namespace(org, namespace, ch=ch)


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
) -> tuple[int, int]:
    """Re-insert reinstated heads and tombstone removed ones; (reinstated, removed)
    as the publisher accepted them. A reinstated value is a revision of the present:
    it holds again from `applied_at` (a transition), whatever span it held before,
    so a dated value the key has since moved past becomes current again. Only an
    accepted row gets its projection: a stub embedding row for the worker to drain,
    or, for a row that is not embedded ('high', a non-searchable type, whose excerpt
    would carry the value into search), its key's live vectors retired."""
    from server.memory.app.temporal_projection import _STUB_COLS
    from server.memory.domain.temporal_capture import WriteIntent, row_key, write_intent

    # (the version row, the row it was built from) of each change.
    reinstated: list[tuple[dict[str, Any], store.MemoryRow]] = []
    removed: list[tuple[dict[str, Any], store.MemoryRow]] = []

    # --- reinstate: the historical row becomes the head again ---------------
    pairs = [(a.memory_key, a.version_to) for a in plan.reinstate]
    wanted = {
        (a.agent, a.user_id, a.session_id, a.memory_key, a.version_to) for a in plan.reinstate
    }
    for i in range(0, len(pairs), _INSERT_BATCH):
        historical = await store.fetch_version_rows(
            org, namespace, pairs[i : i + _INSERT_BATCH], ch=ch
        )
        for h in historical:
            if (h.agent, h.user_id, h.session_id, h.memory_key, h.version) not in wanted:
                continue
            row = store.build_memory_version_row(
                h,
                version=store.next_version(),
                updated_at=applied_at,
                valid_at=applied_at,
                valid_to=None,
            )
            reinstated.append((row, h))

    # --- remove: keys born after as_of are tombstoned ----------------------
    for a in plan.remove:
        head = await store.get_by_key(
            store.Identity(
                org=org,
                namespace=namespace,
                agent=a.agent,
                user_id=a.user_id,
                session_id=a.session_id,
            ),
            a.memory_key,
            fresh=True,
            scope=WHOLE_NAMESPACE,
        )
        if head is None or head.tombstoned_at is not None:
            continue
        row = store.build_memory_version_row(
            head,
            version=store.next_version(),
            tombstoned_at=applied_at,
            updated_at=applied_at,
        )
        removed.append((row, head))

    changes = reinstated + removed
    # The rows the publisher kept, by identity: it may restamp a row's version.
    kept: dict[tuple[str, ...], dict[str, Any]] = {}

    async def _keep(rows: Any) -> None:
        kept.update({row_key(r): r for r in rows})

    with write_intent(
        WriteIntent(change_kind="transition", effective_at=applied_at.replace(tzinfo=UTC))
    ):
        for i in range(0, len(changes), _INSERT_BATCH):
            await insert_memory_versions(
                [r for r, _ in changes[i : i + _INSERT_BATCH]], ch=ch, alongside=[_keep]
            )
    accepted = [(kept[k], base) for r, base in changes if (k := row_key(r)) in kept]
    await _project(
        [(r, base.model_copy(update={c: r[c] for c in _STUB_COLS})) for r, base in accepted],
        applied_at,
        ch,
    )
    counts = (
        sum(row_key(r) in kept for r, _ in reinstated),
        sum(row_key(r) in kept for r, _ in removed),
    )
    log.info(
        "namespace.rollback applied org=%s ns=%s reinstate=%d/%d remove=%d/%d",
        org,
        namespace,
        counts[0],
        len(plan.reinstate),
        counts[1],
        len(plan.remove),
    )
    return counts


async def _project(
    accepted: list[tuple[dict[str, Any], store.MemoryRow]], applied_at: datetime, ch: Any
) -> None:
    """Each accepted row's search projection: a stub embedding row, or its key's live
    vectors retired (and a 'high' row fenced, so a plain write still embedding lands
    below). After the memory insert, as memory.set does, so no reader sees the vector
    outlive the row that retires it; each key's own version keeps a concurrent newer
    embedded write's vectors."""
    from server.embedding import get_embedder
    from server.memory.app.high_fence import fence_high
    from server.memory.domain.memory_types import is_embedded

    embedder = get_embedder()
    stubs: list[dict[str, Any]] = []
    retire: list[tuple[store.EmbeddingKey, int]] = []
    fence: list[tuple[store.EmbeddingKey, int]] = []
    for row, shown in accepted:
        version = int(row["version"])
        if is_embedded(shown.memory_type, shown.sensitivity):
            stubs.append(
                stub_embedding_row(
                    shown,
                    embedder,
                    version=version,
                    tombstoned_at=row["tombstoned_at"],
                    created_at=applied_at,
                )
            )
            continue
        retire.append((store.embedding_key(shown), version))
        if shown.sensitivity == "high":
            fence.append((store.embedding_key(shown), version))
    for i in range(0, len(stubs), _INSERT_BATCH):
        await store.insert_embedding_rows(stubs[i : i + _INSERT_BATCH], ch=ch)
    await store.retire_vectors_for_keys(retire, ch=ch)
    await fence_high(fence, ch=ch)


def stub_embedding_row(
    row: store.MemoryRow,
    embedder: Any,
    *,
    version: int,
    tombstoned_at: datetime | None,
    created_at: datetime,
) -> dict[str, Any]:
    """A dimensionless embedding row for a re-emitted version row: the embedding
    worker fills its vector. `version` is the new memory row's, so a concurrent
    'high' write's fence outranks it."""
    from server.memory.domain.excerpt import build_excerpt
    from server.memory.domain.trust import attests

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
        embedding=[0.0] * int(embedder.dim),
        tombstoned_at=tombstoned_at,
        created_at=created_at,
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
        # The version's attestation, if it still binds its content.
        verified=int(
            attests(
                getattr(row, "verified_by", ""),
                getattr(row, "verified_sha256", ""),
                getattr(row, "content_sha256", ""),
            )
        ),
    )
