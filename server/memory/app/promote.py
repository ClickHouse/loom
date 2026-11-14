"""memory.promote + memory.demote — private → namespace-shared lifecycle."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from server.core import leases
from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.errors import (
    DefinitionCeilingError,
    DefinitionNameTakenError,
    DefinitionPromotionRefusedError,
    OverloadedError,
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
from server.memory.domain.definitions import (
    DEFINITION_KEY_PREFIX,
    definition_names,
    names_of,
)
from server.memory.domain.memory_types import (
    is_definition,
    is_namespace_definition,
    is_searchable_type,
)
from server.memory.domain.sensitivity import HIGH, MEDIUM, max_sensitivity
from server.memory.domain.trust import verification_state
from server.memory.store import shared as shared_repo
from server.memory.store.rows import MemoryRow, next_version
from server.observability.budget_shape import note_budget_shape
from server.settings import settings

log = logging.getLogger(__name__)


async def handle_memory_promote(input: MemoryPromoteInput) -> MemoryPromoteOutput:
    # Lazy imports break the import cycle with memory/app/handlers.py.
    import server.memory.store as memory_repo
    from server.memory.domain.excerpt import build_excerpt

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
    is_definition_promotion = is_definition(src) or target_key.startswith(DEFINITION_KEY_PREFIX)
    names: set[str] = set()
    if is_definition_promotion:
        note_budget_shape("definition")
        names = await _approved_definition_names(src, target_key, input.approved_sha256)
        sent = {
            "approver": input.approver,
            "promoted_from_candidate_id": input.promoted_from_candidate_id,
        }
        ignored = [f for f, v in sent.items() if v is not None]
        # The token's actor answers for a definition, which records no candidate.
        promoted_by = writer_actor("explicit")
        candidate_id = None
    else:
        ignored = ["approved_sha256"] if input.approved_sha256 is not None else []
        promoted_by = input.approver or "explicit"
        candidate_id = input.promoted_from_candidate_id
    promotion_event = str(uuid.uuid4())
    now_naive = naive_utc_now()
    reversible_until = now_naive + timedelta(days=input.reversible_for_days)
    # The row read, not the request: an `agent="*"` lookup names no identity, and erasure
    # finds a copy by its source's.
    promoted_from_repr = (
        f"{src.org}/{src.namespace}/{src.agent}/{src.user_id}/{src.session_id}/{src.memory_key}"
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
            # definition's names decide instead (`_check_shared_definitions`).
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

    row = {
        "org": input.org,
        "namespace": input.namespace,
        "memory_key": target_key,
        "value": src_value_json,
        "memory_type": src.memory_type,
        "confidence": src.confidence,
        "promoted_from": [promoted_from_repr],
        "promotion_event": promotion_event,
        "promoted_from_candidate_id": candidate_id,
        "promoted_by": promoted_by,
        "rationale": input.rationale,
        "promoted_at": now_naive,
        "reversible_until": reversible_until,
        "superseded_by": None,
        "tombstoned_at": None,
        "version": next_version(),
    }
    if is_definition_promotion:
        await _insert_definition(input.org, input.namespace, names, row)
    else:
        await shared_repo.insert_shared_row(row)

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


async def _approved_definition_names(
    src: MemoryRow, target_key: str, approved_sha256: str | None
) -> set[str]:
    """The names the definition's value holds, refusing a definition the shared layer
    cannot govern (docs/concepts/identities-and-namespaces.md)."""
    if not is_definition(src):
        raise DefinitionPromotionRefusedError(
            "only a row of type definition is promoted under a definition/ key"
        )
    if not is_namespace_definition(src):
        raise DefinitionPromotionRefusedError(
            "a definition is promoted from its namespace-wide row: agent, user and session '-'"
        )
    if not src.memory_key.startswith(DEFINITION_KEY_PREFIX) or target_key != src.memory_key:
        raise DefinitionPromotionRefusedError(
            "a definition is promoted under its own definition/ key (no other target_key)"
        )
    if not _live(src):
        raise DefinitionPromotionRefusedError(
            "the definition's row is not live (not active, or past its expires_at or valid_to)"
        )
    if not approved_sha256:
        raise ValidationError("a definition promotion names the hash it approves (approved_sha256)")
    current = served_content_sha256(src.value)
    if current != approved_sha256:
        raise PreconditionFailedError(
            "the definition's text changed since it was approved", current_sha256=current
        )
    names = definition_names(src.value)
    if names is None:
        raise ValidationError(
            "a definition's value is a JSON object with a non-empty string term and, optionally, "
            "a list of string aliases"
        )
    # The value itself is graded, whatever its label or the autodetect setting: an import or
    # a restore leaves the label empty.
    graded = await detect_level(await decoded_text(src.value))
    if max_sensitivity([src.sensitivity, graded]) in (MEDIUM, HIGH):
        raise SensitiveDefinitionRefusedError(
            "a definition is served to every reader of the namespace: one that "
            "reads as medium or high sensitivity is not promoted"
        )
    return names


async def _insert_definition(
    org: str, namespace: str, names: set[str], row: dict[str, Any]
) -> None:
    """Write the definition's shared row unless the namespace is at its ceiling or another
    shared definition holds one of its names, under a claim on the namespace so that two
    promotions cannot both pass."""
    claim = f"definition.promote/{org}/{namespace}"
    # The concepts page quotes the 10 s wait.
    async with leases.holding(claim, ttl_s=30, wait_s=10) as held:
        if not held:
            raise OverloadedError("another definition promotion in this namespace is running")
        await _check_shared_definitions(org, namespace, names, row["memory_key"])
        await shared_repo.insert_shared_row(row)


async def _check_shared_definitions(
    org: str, namespace: str, names: set[str], target_key: str
) -> None:
    """Refuse a definition past the namespace's ceiling or under a name another holds."""
    ceiling = settings.memory_term_leg_max_definitions
    # At most the ceiling plus one: within the ceiling the read holds every live definition,
    # so it says whether this key holds one already (a revision).
    rows = await shared_repo.shared_definitions(org, namespace, ceiling + 1)
    revision = any(r["memory_key"] == target_key for r in rows)
    if len(rows) > ceiling:
        raise DefinitionCeilingError(
            f"the namespace holds more than {ceiling} shared definitions: demote until at "
            f"most {ceiling} remain"
        )
    if len(rows) == ceiling and not revision:
        raise DefinitionCeilingError(
            f"the namespace holds {ceiling} shared definitions: demote one to add a new key"
        )
    taken: dict[str, set[str]] = {}
    for r in rows:
        if r["memory_key"] != target_key and (clash := names & names_of(r["term"], r["aliases"])):
            taken[r["memory_key"]] = clash
    if taken:
        raise DefinitionNameTakenError(
            "another shared definition holds this term or an alias",
            conflicting_keys=sorted(taken),
            names=sorted(set().union(*taken.values())),
        )


async def handle_memory_demote(input: MemoryDemoteInput) -> MemoryDemoteOutput:
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
