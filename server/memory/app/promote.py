"""memory.promote + memory.demote — private → namespace-shared lifecycle."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now
from server.embedding import get_embedder
from server.identity.app.request_scope import request_scope
from server.memory.api.models_lifecycle import (
    MemoryDemoteInput,
    MemoryDemoteOutput,
    MemoryPromoteInput,
    MemoryPromoteOutput,
)
from server.memory.domain.memory_types import is_searchable_type
from server.memory.domain.trust import verification_state
from server.memory.store.rows import next_version

log = logging.getLogger(__name__)


async def handle_memory_promote(input: MemoryPromoteInput) -> MemoryPromoteOutput:
    # Lazy imports break the import cycle with memory/app/handlers.py.
    import server.memory.store as memory_repo
    from server.memory.domain.excerpt import build_excerpt
    from server.memory.store import shared as shared_repo

    src = await memory_repo.get_by_key(
        memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
        ),
        input.memory_key,
        fresh=True,  # promote follows set; a replica-lag miss is not 'not found'
        scope=request_scope(),
    )
    if src is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RESOURCE_NOT_FOUND",
                "message": f"source memory not found: {input.memory_key!r}",
            },
        )

    # Block rows are per-agent working memory; loom.shared has no char_limit,
    # so promoting one would silently drop its overflow cap. Refuse.
    if src.memory_type == "profile":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BLOCK_PROMOTION_REFUSED",
                "message": (
                    "memory.promote refuses block rows — blocks are "
                    "per-agent working memory and loom.shared doesn't "
                    "carry char_limit. If you need a team-wide persona, "
                    "have each agent write its own block; cross-agent "
                    "shared blocks is a separate primitive that doesn't "
                    "exist yet."
                ),
            },
        )

    # A 'high' row is kept out of every search leg by having no vector; loom.shared has
    # no sensitivity column, so a promoted copy would be embedded and served to the
    # whole namespace. Refuse.
    if (getattr(src, "sensitivity", "") or "") == "high":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "HIGH_SENSITIVITY_PROMOTION_REFUSED",
                "message": (
                    "memory.promote refuses rows labelled sensitivity='high': "
                    "shared memory carries no sensitivity label and is searchable "
                    "namespace-wide."
                ),
            },
        )

    target_key = input.target_key or input.memory_key
    promotion_event = str(uuid.uuid4())
    now_naive = naive_utc_now()
    reversible_until = now_naive + timedelta(days=input.reversible_for_days)
    # The row read, not the request: an `agent="*"` lookup names no identity, and erasure
    # finds a copy by its source's.
    promoted_from_repr = (
        f"{src.org}/{src.namespace}/{src.agent}/{src.user_id}/{src.session_id}/{src.memory_key}"
    )
    promoted_by = input.approver or "explicit"
    src_value_json = json.dumps(src.value, default=str)

    # Embed once: reused for the conflict check and the shared embedding insert.
    # On embed failure we skip the check and write a stub; embedding_worker reconciles.
    embedding: list[float] = []
    excerpt = build_excerpt(src_value_json, memory_key=src.memory_key)
    embedder = get_embedder() if is_searchable_type(src.memory_type) else None
    if embedder is not None:
        try:
            embedding = await embedder.embed(excerpt)
        except Exception as e:  # noqa: BLE001 — embed failure writes a stub the worker reconciles
            log.warning(
                "memory.promote: inline embedding failed for "
                "(%s, %s, %s) — writing stub row; embedding_worker "
                "will reconcile. error=%s",
                input.org,
                input.namespace,
                target_key,
                e,
            )
            embedding = []

        if embedding:
            # A close shared row under a *different* key means promoting this
            # value would contradict the namespace's agreed knowledge — refuse.
            from server.memory.app import conflicts

            peers = await conflicts.check_against_shared(
                org=input.org,
                namespace=input.namespace,
                target_key=target_key,
                memory_type=src.memory_type,
                embedding=embedding,
            )
            if peers:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "SHARED_CONFLICT",
                        "message": (
                            f"promotion conflicts with existing shared memory "
                            f"under different memory_key(s) in namespace "
                            f"{input.namespace!r}; refusing to corrupt the "
                            f"shared knowledge"
                        ),
                        "conflicting_keys": [p["memory_key"] for p in peers],
                    },
                )

    # promoted_from_candidate_id is set only on the learnings.review path;
    # other callers pass None and lineage falls back to the time-window match.
    await shared_repo.insert_shared_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "memory_key": target_key,
            "value": src_value_json,
            "memory_type": src.memory_type,
            "confidence": src.confidence,
            "promoted_from": [promoted_from_repr],
            "promotion_event": promotion_event,
            "promoted_from_candidate_id": input.promoted_from_candidate_id,
            "promoted_by": promoted_by,
            "rationale": input.rationale,
            "promoted_at": now_naive,
            "reversible_until": reversible_until,
            "superseded_by": None,
            "tombstoned_at": None,
            "version": next_version(),
        }
    )

    # Make the shared memory searchable via a scope='shared' embedding,
    # reusing the vector above (or a stub the embedding_worker reconciles).
    if embedder is not None:
        await shared_repo.insert_shared_embedding(
            {
                "org": input.org,
                "namespace": input.namespace,
                "agent": shared_repo.SHARED_AGENT,
                "user_id": "-",
                "session_id": "-",
                "memory_key": target_key,
                "memory_type": src.memory_type,
                "scope": "shared",
                "content_excerpt": excerpt,
                "embedding_model": embedder.model_name,
                "embedding_dim": embedder.dim if embedding else 0,
                "embedding": embedding,  # [] when embed failed
                "confidence": float(src.confidence),
                "tombstoned_at": None,
                "created_at": now_naive,
                "valid_at": src.valid_at,
                # Provenance of the promoted content, for the ranking term and
                # memory.ask's unverified-external filter; the attestation
                # counts only while it binds the promoted content's hash.
                "source_class": src.source_class,
                "verified": int(
                    verification_state(src.verified_by, src.verified_sha256, src.content_sha256)
                    in ("human", "process")
                ),
            }
        )

    return MemoryPromoteOutput(
        shared_memory_key=target_key,
        promotion_event=promotion_event,
        reversible_until=reversible_until,
    )


async def handle_memory_demote(input: MemoryDemoteInput) -> MemoryDemoteOutput:
    from server.memory.store import shared as shared_repo

    row = await shared_repo.fetch_shared(
        input.org,
        input.namespace,
        input.memory_key,
    )
    if row is None or row["tombstoned_at"] is not None:
        # No live shared row → idempotent no-op
        return MemoryDemoteOutput(demoted=False, was_reversible=False)

    now_naive = naive_utc_now()
    was_reversible = bool(row["reversible_until"]) and row["reversible_until"] >= now_naive

    # Tombstone via higher-version INSERT, mirroring every live column except
    # tombstoned_at + version (omitting any column zeros it for FINAL readers).
    version = next_version()  # the tombstone's, which the vector retire carries
    await shared_repo.insert_shared_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "memory_key": input.memory_key,
            "value": row["value"],
            "memory_type": row["memory_type"],
            "confidence": row["confidence"],
            "promoted_from": list(row["promoted_from"]),
            "promotion_event": row["promotion_event"],
            "promoted_from_candidate_id": row["promoted_from_candidate_id"],
            "promoted_by": row["promoted_by"],
            "rationale": input.rationale or row["rationale"],
            "promoted_at": row["promoted_at"],
            "reversible_until": row["reversible_until"],
            "superseded_by": None,
            "tombstoned_at": now_naive,
            "version": version,
        }
    )

    # Tombstone the shared embedding so memory.search(scope='shared') stops returning it
    await shared_repo.tombstone_shared_embedding(
        input.org,
        input.namespace,
        input.memory_key,
        now_naive,
        version=version,
    )

    return MemoryDemoteOutput(
        demoted=True,
        was_reversible=was_reversible,
        tombstoned_at=now_naive,
    )
