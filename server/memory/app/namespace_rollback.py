"""The memory side of namespace.rollback: the version-history reads the plan is made from,
and the writes that apply it (reinstated heads re-emitted through the version-row builder,
removed keys tombstoned, one stub embedding row per touched key for the embedding worker)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
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
) -> None:
    """Re-insert reinstated heads and tombstone removed ones, with a stub
    embedding row per touched key for the worker to drain. A row that is not
    embedded ('high', or a non-searchable type) gets no stub: its excerpt would
    carry the value into search. Its key's live vectors are retired instead."""
    from server.embedding import get_embedder
    from server.memory.app.high_fence import fence_high
    from server.memory.domain.memory_types import is_embedded

    embedder = get_embedder()

    memory_rows: list[dict[str, Any]] = []
    embedding_rows: list[dict[str, Any]] = []
    # (identity key, the new memory row's version) of each row that is not embedded.
    retire: list[tuple[store.EmbeddingKey, int]] = []
    # The 'high' ones among them, fenced too: a plain write still embedding lands below.
    fence: list[tuple[store.EmbeddingKey, int]] = []

    def _retire(row: store.MemoryRow, version: int) -> None:
        retire.append((store.embedding_key(row), version))
        if row.sensitivity == "high":
            fence.append((store.embedding_key(row), version))

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
            new_version = store.next_version()
            memory_rows.append(
                store.build_memory_version_row(h, version=new_version, updated_at=applied_at)
            )
            if is_embedded(h.memory_type, h.sensitivity):
                embedding_rows.append(
                    stub_embedding_row(
                        h,
                        embedder,
                        version=new_version,
                        tombstoned_at=h.tombstoned_at,
                        created_at=applied_at,
                    )
                )
            else:
                _retire(h, new_version)

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
        new_version = store.next_version()
        memory_rows.append(
            store.build_memory_version_row(
                head,
                version=new_version,
                tombstoned_at=applied_at,
                updated_at=applied_at,
            )
        )
        if is_embedded(head.memory_type, head.sensitivity):
            embedding_rows.append(
                stub_embedding_row(
                    head,
                    embedder,
                    version=new_version,
                    tombstoned_at=applied_at,
                    created_at=applied_at,
                )
            )
        else:
            _retire(head, new_version)

    for i in range(0, len(memory_rows), _INSERT_BATCH):
        await insert_memory_versions(memory_rows[i : i + _INSERT_BATCH], ch=ch)
    for i in range(0, len(embedding_rows), _INSERT_BATCH):
        await store.insert_embedding_rows(embedding_rows[i : i + _INSERT_BATCH], ch=ch)
    # After the memory insert, as memory.set does, so no reader sees the vector
    # outlive the row that retires it. Each key's own version keeps a concurrent
    # newer embedded write's vectors.
    await store.retire_vectors_for_keys(retire, ch=ch)
    await fence_high(fence, ch=ch)
    log.info(
        "namespace.rollback applied org=%s ns=%s reinstate=%d remove=%d",
        org,
        namespace,
        len(plan.reinstate),
        len(plan.remove),
    )


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
