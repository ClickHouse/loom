"""memory.promote + memory.demote — private → namespace-shared lifecycle."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.errors import (
    DefinitionCeilingError,
    DefinitionNameTakenError,
    DefinitionPromotionRefusedError,
    PreconditionFailedError,
    SensitiveDefinitionRefusedError,
    ValidationError,
)
from server.core.hashing import served_content_sha256
from server.embedding import get_embedder
from server.identity.app.request_scope import request_scope
from server.memory.api.models_lifecycle import (
    MemoryDemoteInput,
    MemoryDemoteOutput,
    MemoryPromoteInput,
    MemoryPromoteOutput,
)
from server.memory.app.provenance import writer_actor
from server.memory.app.sensitivity_level import decoded_text, detect_level
from server.memory.domain.definitions import DEFINITION_KEY_PREFIX, definition_names
from server.memory.domain.memory_types import (
    DEFINITION,
    is_namespace_definition,
    is_searchable_type,
)
from server.memory.domain.sensitivity import HIGH, MEDIUM, max_sensitivity
from server.memory.domain.trust import verification_state
from server.memory.store.rows import MemoryRow, next_version
from server.settings import settings

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
    is_definition_promotion = src.memory_type == DEFINITION or target_key.startswith(
        DEFINITION_KEY_PREFIX
    )
    ignored: list[str] = []
    if is_definition_promotion:
        await _check_definition_promotion(input, src, target_key)
        ignored = [f for f in ("approver", "promoted_from_candidate_id") if getattr(input, f)]
    elif input.approved_sha256:
        ignored = ["approved_sha256"]
    promotion_event = str(uuid.uuid4())
    now_naive = naive_utc_now()
    reversible_until = now_naive + timedelta(days=input.reversible_for_days)
    # The row read, not the request: an `agent="*"` lookup names no identity, and erasure
    # finds a copy by its source's.
    promoted_from_repr = (
        f"{src.org}/{src.namespace}/{src.agent}/{src.user_id}/{src.session_id}/{src.memory_key}"
    )
    # A definition's promoter is the token's actor: the admin who answers for it.
    promoted_by = (
        writer_actor("explicit") if is_definition_promotion else input.approver or "explicit"
    )
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

        if embedding and not is_definition_promotion:
            # A close shared row under a *different* key means promoting this
            # value would contradict the namespace's agreed knowledge — refuse. A
            # definition's names decide instead (`_check_definition_promotion`).
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

    # Only a non-definition learnings.review promotion records its candidate.
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
            "promoted_from_candidate_id": None
            if is_definition_promotion
            else input.promoted_from_candidate_id,
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
        ignored_args=ignored,
    )


def _live(row: MemoryRow) -> bool:
    """Whether the row is one the term leg would serve: active and not past its
    `expires_at` or `valid_to` (a deleted row is not read at all)."""
    now = naive_utc_now()
    ended = [t for t in (row.expires_at, row.valid_to) if t is not None]
    return row.status == "active" and all(to_naive_utc(t) > now for t in ended)


async def _check_definition_promotion(
    input: MemoryPromoteInput, src: MemoryRow, target_key: str
) -> None:
    """Refuse a definition promotion the shared layer cannot govern
    (docs/concepts/identities-and-namespaces.md)."""
    from server.memory.store import shared as shared_repo

    if not (
        is_namespace_definition(src)
        and src.memory_key.startswith(DEFINITION_KEY_PREFIX)
        and target_key == src.memory_key
        and _live(src)
    ):
        raise DefinitionPromotionRefusedError(
            "a definition is promoted from its live namespace-wide row (agent, "
            "user and session '-') of type definition under a definition/ key, to that key"
        )
    if not input.approved_sha256:
        raise ValidationError("a definition promotion names the hash it approves (approved_sha256)")
    current = served_content_sha256(src.value)
    if current != input.approved_sha256:
        raise PreconditionFailedError(
            "the definition's text changed since it was approved", current_sha256=current
        )
    names = definition_names(src.value)
    if names is None:
        raise ValidationError(
            "a definition's value is a JSON object with a string term and, optionally, a list "
            "of string aliases"
        )
    # The value itself is graded, whatever its label or the autodetect setting: an import or
    # a restore leaves the label empty.
    graded = await detect_level(await decoded_text(src.value))
    if max_sensitivity([src.sensitivity, graded]) in (MEDIUM, HIGH):
        raise SensitiveDefinitionRefusedError(
            "a definition is served to every reader of the namespace: one that "
            "reads as medium or high sensitivity is not promoted"
        )
    ceiling = settings.memory_term_leg_max_definitions
    # At most the ceiling plus one: within the ceiling the read holds every live definition,
    # so it says whether this key holds one already (a revision).
    rows = await shared_repo.shared_definitions(input.org, input.namespace, ceiling + 1)
    revision = any(r["memory_key"] == target_key for r in rows)
    if len(rows) > ceiling or (len(rows) == ceiling and not revision):
        raise DefinitionCeilingError(
            f"the ceiling is {ceiling} shared definitions: demote one to make room"
        )
    taken = sorted(
        r["memory_key"]
        for r in rows
        if r["memory_key"] != target_key and names & (definition_names(r["value"]) or set())
    )
    if taken:
        raise DefinitionNameTakenError(
            "another shared definition holds this term or an alias", conflicting_keys=taken
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
