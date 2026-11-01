"""The commit half of memory.set: embed the row, take the conflict verdict,
then merge, answer a dry run, or insert (plain or with its embedding row),
and run what follows a write. `_SetWrite` carries one call's state through
the phases; `_handle_memory_set` in server/memory/app/write.py orders them."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.hashing import sha256_value_json as _content_sha256_of_stored
from server.embedding import get_embedder
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import (
    WHOLE_NAMESPACE,
    row_in_read_scope,
)
from server.memory.api.models_common import (
    SET_PEERS_MAX,
    MemorySetPeer,
    set_next_hint,
    set_peer_excerpt,
)
from server.memory.api.models_crud import (
    MemorySetInput,
    MemorySetOutput,
)
from server.memory.app import after_write, webhooks
from server.memory.app.auto_resolution import Follow, follow_beside_response
from server.memory.app.drops import record_drop
from server.memory.app.edge_links import drop_undeclared_edges_beside, owes_undeclared_drop
from server.memory.app.high_fence import fence_high
from server.memory.app.set_entities import declared_entities
from server.memory.app.triple_projections import project_stated_relations
from server.memory.app.write_fence import lift_above_deletes, lift_after_insert
from server.memory.domain.embed_text import embedding_texts
from server.memory.domain.trust import attests
from server.memory.domain.webhook_events import EventType
from server.memory.store.inserts import insert_memory_versions

log = logging.getLogger(__name__)


def count_attribute_tags(namespace: str, tags: list[str]) -> None:
    """Record one attribute-tag tick per `attribute:<class>` label a write added."""
    try:
        from server.observability.metrics import record_attribute_tagged

        for tag in tags:
            record_attribute_tagged(namespace=namespace, attribute=tag.split(":", 1)[1])
    except Exception:  # noqa: BLE001 — metrics never fail a write
        pass


def count_write(namespace: str, outcome: str) -> None:
    """Record one `loom.memory.writes` tick for a terminal write outcome.

    `memory.set` has four terminal shapes and they are not interchangeable:
    a `merged` write means the caller restated something Loom already knew
    (no row inserted), a `duplicate` means it inserted a near-copy the
    conflict checker could not canonicalize, and `rejected` means a pinned
    row blocked it. Counting only requests — which is all `loom.requests`
    does — makes all four look like the same successful POST, so a namespace
    that has stopped learning anything new is indistinguishable from one
    that is ingesting steadily.

    Outcome is a bounded enum:

      served    created / updated / merged / duplicate / conflicted
      refused   rejected / blacklisted / too_large / precondition_failed
      failed    error            (infrastructure; see handle_memory_set)
      bulk-only unchanged        (byte-identical restatement)
                unknown          (the existing-key lookup failed, so
                                  created-vs-updated is not knowable)

    Keep this list in step with `_WRITE_OUTCOME_BY_ERROR_CODE`, the instrument
    description in `observability/metrics`, and the table in
    `deploy/hyperdx_dashboards.md` — a reader building a dashboard filter from
    a stale list silently drops whole series. See the cardinality rule in
    `observability/metrics`. Never raises.
    """
    try:
        from server.observability import metrics as _metrics

        _metrics.record_memory_write(namespace=namespace, outcome=outcome)
    except Exception:  # noqa: BLE001  — telemetry must never fail a write
        pass


def _peer_in_read_scope(peer: Any) -> bool:
    """Whether the writer may see `peer`: a conflict verdict never names a row
    outside the caller's read scope, and a scope the request cannot resolve
    shows none."""
    from server.core.errors import AuthInsufficientError, IdentityIncompleteError

    try:
        scope = request_scope()
    except (AuthInsufficientError, IdentityIncompleteError):
        return False
    return row_in_read_scope(scope, getattr(peer, "user_id", ""), getattr(peer, "session_id", ""))


def peer_id_in_read_scope(conflict_result: Any, memory_id: str) -> str:
    """`memory_id` when the verdict peer it names is inside the read scope,
    else "". An id that names no peer is shown only to an unbounded read."""
    peer = next(
        (
            p
            for p in [
                *list(getattr(conflict_result, "refined_peers", None) or []),
                *list(getattr(conflict_result, "peers", None) or []),
            ]
            if str(getattr(p, "memory_id", "") or "") == memory_id
        ),
        None,
    )
    return memory_id if memory_id and _peer_in_read_scope(peer) else ""


def set_peers(conflict_result: Any) -> list[Any]:
    """The rows a verdict was reached against, as the wire shape memory.set
    returns: closest first, at most SET_PEERS_MAX, the value
    clipped to an excerpt, only those inside the read scope. `refines` lists
    the refined peers ahead of the rest. Never raises on an odd peer."""
    seen: set[str] = set()
    ordered: list[Any] = []
    for p in [
        *list(getattr(conflict_result, "refined_peers", None) or []),
        *list(getattr(conflict_result, "peers", None) or []),
    ]:
        mid = str(getattr(p, "memory_id", "") or "")
        if mid in seen or not _peer_in_read_scope(p):
            continue
        seen.add(mid)
        ordered.append(p)
    ordered.sort(key=lambda p: -float(getattr(p, "similarity", 0.0) or 0.0))
    out: list[Any] = []
    for p in ordered[:SET_PEERS_MAX]:
        try:
            out.append(
                MemorySetPeer(
                    memory_id=str(getattr(p, "memory_id", "") or ""),
                    memory_key=str(getattr(p, "memory_key", "") or ""),
                    excerpt=set_peer_excerpt(str(getattr(p, "value", "") or "")),
                    valid_at=getattr(p, "valid_at", None),
                    similarity=round(float(getattr(p, "similarity", 0.0) or 0.0), 4),
                )
            )
        except Exception:  # noqa: BLE001 — a malformed peer must not fail the write
            continue
    return out


def set_next(
    verdict: str,
    peers: list[Any],
    *,
    repeated_merge: bool = False,
    relations_unlinked: bool = False,
) -> str:
    return set_next_hint(
        verdict, peers, repeated_merge=repeated_merge, relations_unlinked=relations_unlinked
    )


async def _project_set_entities_fail_open(
    input: MemorySetInput,
    *,
    memory_id: str,
    wait_visible: bool = False,
    ents: list[str] | None = None,
    content_sha256: str,
    high: bool = False,
) -> None:
    """Deterministic LLM-free entity-graph projection for a committed
    `memory.set`. A point write carries no `entities` column, so without
    this it stays invisible to the graph until a bulk path or the
    entity_extractor worker re-extracts it. Fail-open: any error logs a
    warning and the write still succeeds. `wait_visible` (the projection
    beside the response) returns only once its edges are readable. `ents`: the
    entities the caller already extracted. `content_sha256`: the value projected, so
    a projection landing after a revision of the row leaves no edge.
    """
    try:
        from server.memory.app.edge_links import project_memory_set_entities

        await project_memory_set_entities(
            input,
            memory_key=input.memory_key,
            value=input.value,
            wait_visible=wait_visible,
            memory_id=memory_id,
            ents=ents,
            content_sha256=content_sha256,
            high=high,
        )
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("memory.set entity projection failed (non-fatal): %s", e)


def _set_write_outcome(duplicate_of_id: str, verdict: str, had_existing_row: bool) -> str:
    """The write-outcome label of a memory.set that inserted a row. `duplicate`
    takes precedence over `conflict` because only one of the two verdicts can
    be set on a given result."""
    if duplicate_of_id:
        return "duplicate"
    if verdict == "conflict":
        return "conflicted"
    return "updated" if had_existing_row else "created"


@dataclass(slots=True)
class _SetWrite:
    """One memory.set's request-scoped state, filled by its phases in order.

    Only values that flow between two or more phases live here; everything
    else stays local to the phase that computes it. `input` is rebound (via
    `model_copy`) by the phases that add tags, so later phases read it here.
    """

    input: MemorySetInput
    version: int
    retyped_from: str
    existing: Any
    expires_at: datetime | None
    serialized_value: str = ""
    new_sha: str = ""
    reinforcement_count: int = 1
    last_reinforced_at: datetime | None = None
    mentioned_at: datetime | None = None
    effective_sensitivity: str = ""
    on_expire: str = ""
    memory_id: str = ""
    had_existing_row: bool = False
    restated: bool = False
    writer_actor: str = ""
    source_class: str = ""
    memory_row: dict[str, Any] = field(default_factory=dict)
    embedder: Any = None
    excerpt: str = ""
    embedding: list[float] = field(default_factory=list)
    key_embedding: list[float] = field(default_factory=list)
    key_text: str = ""
    # The (loser, winner) pairs this write retired, followed once its relations are in.
    follows: list[Follow] = field(default_factory=list)


async def _lift_above_deletes(w: _SetWrite, ch: Any) -> None:
    """Move the row's version just above a delete of the key that committed while it was
    in flight (`write_fence`); the embedding row is built from it afterwards. A write with
    `if_match_sha256` checked the row the delete removed, so it is refused."""
    await lift_above_deletes(
        w.input.org,
        w.input.namespace,
        [w.memory_row],
        [],
        conditional=bool(w.input.if_match_sha256),
        ch=ch,
    )
    w.version = int(w.memory_row["version"])


async def _lift_after_insert(
    w: _SetWrite, ch: Any, embedding_rows: list[dict[str, Any]] | None = None
) -> int:
    """The row's version once it is in: written again above a memory.delete_version
    tombstone that landed after the fence read (`write_fence`). A conditional write holds
    the key lock, which the delete takes too, so it never needs this."""
    if not w.input.if_match_sha256:
        await lift_after_insert(
            w.input.org, w.input.namespace, [w.memory_row], embedding_rows or [], ch=ch
        )
        w.version = int(w.memory_row["version"])
    return w.version


def _whole_seconds(dt: datetime | None) -> datetime | None:
    """`dt` as the DateTime column stores it: naive UTC, whole seconds."""
    dt = to_naive_utc(dt)
    return dt.replace(microsecond=0) if dt else None


def _only_reinforces(w: _SetWrite) -> bool:
    """A restatement of the live row with every column below unchanged, each compared at its
    stored precision (Float32 confidence, whole-second DateTime); a restated rejected,
    superseded or conflicted row is served again, so it is a change."""
    if not w.restated:
        return False
    row, existing = w.memory_row, w.existing
    return (
        str(row.get("status") or "active") == str(existing.status or "active")
        and row.get("memory_type") == existing.memory_type
        and sorted(row.get("tags") or []) == sorted(existing.tags or [])
        and _whole_seconds(row.get("expires_at")) == _whole_seconds(existing.expires_at)
        and math.isclose(float(row.get("confidence") or 0.0), existing.confidence, rel_tol=1e-6)
        and _whole_seconds(row.get("valid_at")) == _whole_seconds(existing.valid_at)
        and _whole_seconds(row.get("valid_to")) == _whole_seconds(existing.valid_to)
        and str(row.get("sensitivity") or "") == str(existing.sensitivity or "")
        and sorted(row.get("files") or []) == sorted(existing.files or [])
        and str(row.get("source_resource_id") or "") == str(existing.source_resource_id or "")
        and str(row.get("source_class") or "") == str(existing.source_class or "")
    )


async def _announce_set(w: _SetWrite) -> None:
    """Announce the inserted row: `memory.written` for a new key, `memory.revised` for
    any change to a live key, nothing for a pure reinforcement or a proposal. Called
    once the row is in, whatever fails after it."""
    if _only_reinforces(w) or w.memory_row.get("status") == "proposed":
        return
    event: EventType = "memory.revised" if w.had_existing_row else "memory.written"
    await webhooks.announce([(event, w.memory_row)])


async def _set_plain_insert(w: _SetWrite, ch: Any) -> MemorySetOutput:
    """Phase 7 of memory.set: the non-embedded write — one loom.memory row, no peers.

    Taken for a non-searchable type or a 'high' sensitivity row. A dry run
    answers without writing; otherwise insert, project entities, count.
    """
    import server.memory.store as memory_repo
    from server.observability import degradations as _degradations
    from server.observability.otel import traced

    input, retyped_from, version = w.input, w.retyped_from, w.version
    memory_id, new_sha, memory_row = w.memory_id, w.new_sha, w.memory_row
    had_existing_row = w.had_existing_row

    if input.dry_run:
        # Nothing to check (no embedding, no peers) and nothing written: the answer is "this
        # would be a plain insert".
        return MemorySetOutput(
            retyped_from=retyped_from,
            version=0,
            pinned=input.pinned,
            memory_id=memory_id,
            content_sha256=new_sha,
            namespace=input.namespace,
            memory_key=input.memory_key,
            degraded=_degradations.current(),
            noop=True,
            dry_run=True,
        )
    await _lift_above_deletes(w, ch)
    with traced("ch.insert.memory", table="loom.memory"):
        await insert_memory_versions([memory_row], ch=ch)
    try:
        version = await _lift_after_insert(w, ch)
    finally:
        await _announce_set(w)  # the row is in, whatever failed after it
    # The version this replaces may have been embedded (re-set as 'high', or retyped).
    try:
        await memory_repo.tombstone_vectors_of(w.existing, version=memory_row["version"], ch=ch)
    finally:  # the row is in: a failed retire must not also skip the fence
        if memory_row.get("sensitivity") == "high":
            await fence_high(
                [(memory_repo.embedding_key(memory_row), int(memory_row["version"]))], ch=ch
            )
    # A high row's value is never mined into the graph; the entities its writer declared
    # still project. Fenced on its value as `_set_post_insert` projects it: a revision
    # awaits it, a first write leaves it running beside the response.
    high = memory_row.get("sensitivity") == "high"
    declared = declared_entities(input.entities) if high else None
    if not high or declared:
        await asyncio.gather(
            *_entity_projection_tail(
                input,
                memory_id=memory_id,
                content_sha256=new_sha,
                revised=had_existing_row,
                ents=declared,
                high=high,
            )
        )
    if high and had_existing_row and owes_undeclared_drop(w.existing, declared or []):
        # Made high over a value it held: what its text gave the graph goes.
        await drop_undeclared_edges_beside(
            input, {memory_id: int(memory_row["version"])}, op="memory.set"
        )
    await project_stated_relations(
        memory_row,
        input.value,
        input.relations,
        revised=had_existing_row,
        op="memory.set",
        ch=ch,
    )
    # Non-searchable types skip the conflict check entirely, so there is no
    # verdict to map: the insert always happened.
    count_write(input.namespace, "updated" if had_existing_row else "created")
    return MemorySetOutput(
        retyped_from=retyped_from,
        version=version,
        pinned=input.pinned,
        memory_id=memory_id,
        content_sha256=new_sha,
        namespace=input.namespace,
        memory_key=input.memory_key,
        degraded=_degradations.current(),
    )


async def _set_embed(w: _SetWrite) -> None:
    """Phase 8 of memory.set: the excerpt and both vectors (value axis, key axis).

    Sets embedder, excerpt, embedding, key_embedding and key_text ("" when the
    key has no axis text, in which case key_embedding is all zeros).
    """
    from server.observability.otel import traced

    input, serialized_value = w.input, w.serialized_value

    embedder = get_embedder()
    # Procedural rows carry their graph, counters and trace log in the value; embedding that
    # JSON would let statistics churn drift the situation vector on every evolve. Embed and
    # excerpt the procedure's text only (situation, name, steps); the verbatim value column
    # is untouched.
    excerpt, embed_text, key_text = embedding_texts(
        input.memory_type, input.value, serialized_value, input.memory_key
    )
    with traced("embed.compute", model=embedder.model_name, excerpt_len=len(excerpt)):
        if not key_text:
            embedding = await embedder.embed(embed_text)
            key_embedding = [0.0] * embedder.dim
        else:
            # One request for both axes when the embedder can batch. The two
            # `embed()` calls already ran concurrently, so this is not a latency
            # win — it halves outbound embedding requests per write, which is
            # rate-limit headroom rather than speed. `embed_many` is not on the
            # Embedder protocol (the local embedder lacks it), hence the probe.
            embed_many = getattr(embedder, "embed_many", None)
            if embed_many is not None:
                vectors = await embed_many([embed_text, key_text])
                if len(vectors) != 2:
                    raise RuntimeError(f"embed_many returned {len(vectors)} vectors for 2 inputs")
                embedding, key_embedding = vectors[0], vectors[1]
            else:
                embedding, key_embedding = await asyncio.gather(
                    embedder.embed(embed_text),
                    embedder.embed(key_text),
                )
    w.embedder = embedder
    w.excerpt = excerpt
    w.embedding = embedding
    w.key_embedding = key_embedding
    w.key_text = key_text


async def _set_conflict_verdict(
    w: _SetWrite, _cfg_task: Any
) -> tuple[Any, Any, list[Any], MemorySetOutput | None]:
    """Phase 9 of memory.set: the semantic conflict verdict against the peers.

    Returns (the awaited write config, the conflict result, the peers for the
    response, a dry run's "rejected" answer or None). A real write that hits
    a pinned peer raises 409 PINNED_CONFLICT here.
    """
    from server.observability import degradations as _degradations
    from server.observability.otel import traced

    input, retyped_from, memory_id, new_sha = w.input, w.retyped_from, w.memory_id, w.new_sha
    serialized_value, embedding = w.serialized_value, w.embedding

    # Semantic conflict check (same scope as the pending insert). Verdicts:
    #   clear    → normal insert
    #   merge    → canonical-form match; return peer's id, no INSERT
    #   conflict → close-but-different peers; insert + flag both conflicted
    #   rejected → close-but-different pinned peer; raise 409
    from server.memory.app.conflicts import check_on_commit

    with traced("conflicts.check_on_commit", memory_type=input.memory_type):
        _cfg = await _cfg_task
        conflict_result = await check_on_commit(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            memory_type=input.memory_type,
            memory_key=input.memory_key,
            new_value=serialized_value,
            new_embedding=embedding,
            new_run_id=input.run_id,
            new_valid_at=getattr(input, "valid_at", None),
            **_cfg.check_kwargs(),
        )
    # The rows the verdict was reached against ride the response so the caller can act on
    # the verdict without a second read.
    peers_out = set_peers(conflict_result)
    if conflict_result.verdict == "rejected" and input.dry_run:
        # A dry run answers the question it was asked — "what would happen?"
        # — as a verdict, not as the 409 the real write would raise. Nothing
        # is written or counted.
        return (
            _cfg,
            conflict_result,
            peers_out,
            MemorySetOutput(
                retyped_from=retyped_from,
                version=0,
                pinned=input.pinned,
                memory_id=memory_id,
                content_sha256=new_sha,
                namespace=input.namespace,
                memory_key=input.memory_key,
                degraded=_degradations.current(),
                conflict_verdict="rejected",
                peers=peers_out,
                next=set_next("rejected", peers_out),
                noop=True,
                dry_run=True,
            ),
        )
    if conflict_result.verdict == "rejected":
        # Counted at the handler boundary via _WRITE_OUTCOME_BY_ERROR_CODE.
        # A blocker outside the read scope still blocks but is not named.
        blocker = peer_id_in_read_scope(conflict_result, conflict_result.pinned_blocker_id or "")
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PINNED_CONFLICT",
                "message": (
                    f"write conflicts with a pinned memory{f' ({blocker!r})' if blocker else ''}; "
                    "update the pinned row explicitly via memory.update_by_id or unpin it first"
                ),
                "pinned_blocker_id": blocker,
                "peer_ids": [p.memory_id for p in peers_out],
            },
        )
    return _cfg, conflict_result, peers_out, None


def _writers_own(row: Any, input: MemorySetInput) -> bool:
    """The same agent and user: another session of theirs is still theirs."""
    return (row.agent, row.user_id) == (input.agent, input.user_id)


async def _set_merge(
    w: _SetWrite, ch: Any, _cfg: Any, conflict_result: Any, peers_out: list[Any]
) -> MemorySetOutput:
    """Phase 10 of memory.set: the `merge` verdict — the store already held the fact.

    No row is inserted: count the drop, read the row merged into, answer
    (dry run: without side effects) or reinforce the peer and count the merge.
    """
    from server.memory.app import conflicts as _cf
    from server.observability import degradations as _degradations

    input, retyped_from, version, new_sha = w.input, w.retyped_from, w.version, w.new_sha

    # Caller is restating an existing memory: return the existing id, no INSERT.
    # content_sha256 reflects the live row. A merge is a write that left no row: count it by
    # scope so a cross-run merge is visible the hour it happens. Fail-open.
    if not input.dry_run:
        record_drop(
            input.org,
            input.namespace,
            "conflict.merge",
            conflict_result.merge_scope or "unscoped",
            item=input.memory_key,
        )
    import server.memory.store as memory_repo

    existing = await memory_repo.get_by_id(
        conflict_result.existing_memory_id or "",
        org=input.org,
        fresh=True,
        strong=True,  # the conflict check just saw it; read the latest head
        scope=WHOLE_NAMESPACE,
    )
    # A row outside the caller's read scope (or one a bounded read could not
    # check) still absorbs the write, but the answer names no id, hash or key of it.
    merged_into_id = conflict_result.existing_memory_id or ""
    if not _peer_in_read_scope(existing):
        existing, merged_into_id = None, ""
    existing_sha = (
        _content_sha256_of_stored(json.dumps(existing.value, default=str))
        if existing is not None and existing.value is not None
        else new_sha
    )
    # A merge the store has seen before: the row was already reinforced — by a same-key
    # restatement or by the usage fold carrying an earlier merge's evidence event onto it.
    # The fold runs on its own cadence, so this can lag one pass.
    repeated_merge = bool(
        existing is not None
        and (
            int(getattr(existing, "evidence_count", 1) or 1) > 1
            or int(getattr(existing, "reinforcement_count", 1) or 1) > 1
        )
    )
    merge_peers = peers_out
    if not merge_peers and existing is not None:
        # The canonical match may not be among the cosine peers list (a same-content match
        # under another identity); name the row the write merged into.
        from server.memory.api.models_common import MemorySetPeer, set_peer_excerpt

        merge_peers = [
            MemorySetPeer(
                memory_id=str(getattr(existing, "memory_id", "") or ""),
                memory_key=str(getattr(existing, "memory_key", "") or ""),
                excerpt=set_peer_excerpt(json.dumps(existing.value, default=str)),
                valid_at=getattr(existing, "valid_at", None),
                similarity=1.0,
            )
        ]
    # The facts named are the merged row's only when it is the writer's own: another's
    # row would hold them open for as long as it lives.
    own_row = existing if existing is not None and _writers_own(existing, input) else None
    hint = set_next(
        "merge",
        merge_peers,
        repeated_merge=repeated_merge,
        relations_unlinked=bool(input.relations) and own_row is None,
    )
    if input.dry_run:
        # The verdict without the side effects: no evidence event, no
        # drop counter, no write tick.
        return MemorySetOutput(
            retyped_from=retyped_from,
            version=0,
            pinned=input.pinned,
            memory_id=merged_into_id,
            content_sha256=existing_sha,
            namespace=input.namespace,
            memory_key=input.memory_key,
            merged_into_key=_merged_into_key(existing, input),
            degraded=_degradations.current(),
            conflict_verdict="merge",
            peers=merge_peers,
            next=hint,
            noop=True,
            dry_run=True,
        )
    # Auto-reinforce: bump evidence_count on the existing row when the
    # namespace policy is "auto_reinforce". Fire-and-forget.
    if merged_into_id:  # never a row outside the read scope
        await _cf.auto_reinforce_peer(input, merged_into_id, ch=ch, cfg=_cfg)
    if input.relations and own_row is not None:
        # The row keeps its value, so it dropped no fact: nothing to reconcile.
        await project_stated_relations(
            own_row.model_dump(),
            own_row.value,
            input.relations,
            revised=False,
            op="memory.set",
            ch=ch,
        )
    # Canonical-form match: the caller restated a memory Loom already had.
    # No row was inserted, so this is the outcome that separates "still
    # ingesting" from "only ever hearing the same things again".
    count_write(input.namespace, "merged")
    return MemorySetOutput(
        retyped_from=retyped_from,
        version=version,
        pinned=input.pinned,
        memory_id=merged_into_id,
        content_sha256=existing_sha,
        namespace=input.namespace,
        memory_key=input.memory_key,
        merged_into_key=_merged_into_key(existing, input),
        degraded=_degradations.current(),
        conflict_verdict="merge",
        peers=merge_peers,
        next=hint,
        # The first-class noop: the store already held the fact; the row above was reinforced
        # and nothing was written.
        noop=True,
    )


def _set_dry_run_answer(
    w: _SetWrite, conflict_result: Any, peers_out: list[Any], duplicate_of_id: str
) -> MemorySetOutput:
    """Phase 11 of memory.set: a dry run's answer for an insertable verdict (nothing written)."""
    from server.observability import degradations as _degradations

    input, retyped_from, memory_id, new_sha = w.input, w.retyped_from, w.memory_id, w.new_sha

    return MemorySetOutput(
        retyped_from=retyped_from,
        version=0,
        pinned=input.pinned,
        memory_id=memory_id,
        content_sha256=new_sha,
        duplicate_of=peer_id_in_read_scope(conflict_result, duplicate_of_id),
        namespace=input.namespace,
        memory_key=input.memory_key,
        degraded=_degradations.current(),
        conflict_verdict=conflict_result.verdict,
        peers=peers_out,
        next=set_next(conflict_result.verdict, peers_out),
        noop=True,
        dry_run=True,
    )


async def _set_dual_write(w: _SetWrite, ch: Any, _cfg: Any, conflict_result: Any) -> Any:
    """Phase 12 of memory.set: the version row and its embedding row, then the peer links.

    Builds the full-column embedding row, inserts both rows concurrently,
    then flags `conflict` peers or applies a `refines` supersession — after the
    insert, so the new id exists. Returns the verdict that landed: `clear` when no
    peer was left live to flag.
    """
    from server.memory.app.conflicts import mark_peers_conflicted, same_key_mark
    from server.observability.otel import traced

    input, memory_id, memory_row, expires_at = w.input, w.memory_id, w.memory_row, w.expires_at
    source_class = w.source_class
    reinforcement_count_for_insert = w.reinforcement_count
    last_reinforced_at_for_insert = w.last_reinforced_at
    embedder, excerpt, embedding = w.embedder, w.excerpt, w.embedding
    key_embedding, key_text = w.key_embedding, w.key_text
    embedding_dim_for_insert = embedder.dim
    # Owed a judgement when the check asks the conflict resolver to look, or another
    # session holds this key.
    mark = await same_key_mark(
        conflict_result,
        org=input.org,
        namespace=input.namespace,
        memory_row=memory_row,
        policy=_cfg.policy,
    )

    import server.memory.store as memory_repo

    await _lift_above_deletes(w, ch)
    # build_embedding_row fills every persisted column so a later RMT version
    # can't resurrect a default over a real value. Denormalized columns let
    # memory.search read them without a join back to loom.memory.
    embedding_row = memory_repo.build_embedding_row(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        memory_key=input.memory_key,
        memory_type=input.memory_type,
        scope="private",
        content_excerpt=excerpt,
        source_class=source_class,
        embedding_model=embedder.model_name,
        embedding_dim=embedding_dim_for_insert,
        embedding=embedding,
        memory_key_embedding=key_embedding,
        memory_key_embedding_dim=embedding_dim_for_insert if key_text else 0,
        confidence=float(input.confidence),
        tombstoned_at=None,
        created_at=naive_utc_now(),
        tags=list(input.tags or []),
        files=memory_row["files"],
        entities=memory_row["entities"],
        source_resource_id=input.source_resource_id,
        valid_at=input.valid_at,
        valid_to=input.valid_to,
        expires_at=expires_at,
        reinforcement_count=reinforcement_count_for_insert,
        last_reinforced_at=last_reinforced_at_for_insert,
        # The mirror carries the row's lifecycle status from the first version: if it
        # defaulted to `active` while loom.memory holds a `duplicate` verdict, the search
        # pool would keep serving the duplicate.
        status=str(memory_row.get("status") or "active"),
        conflict_checked_at=mark,
        content_sha256=str(memory_row.get("content_sha256") or ""),
        # The memory row's version, not one drawn now: a newer write's retire or fence
        # outranks this vector even when its embedding arrives late.
        version=int(memory_row["version"]),
        # A revision of unchanged content keeps its attestation; the mirror says so.
        verified=int(
            attests(
                memory_row.get("verified_by"),
                memory_row.get("verified_sha256"),
                memory_row.get("content_sha256"),
            )
        ),
    )

    async def _insert_memory() -> None:
        with traced("ch.insert.memory", table="loom.memory"):
            await insert_memory_versions([memory_row], ch=ch)

    async def _insert_embedding() -> None:
        with traced(
            "ch.insert.memory_embeddings",
            table="loom.memory_embeddings",
            embedding_dim=embedding_dim_for_insert,
        ):
            import server.memory.store as memory_repo

            await memory_repo.insert_embedding_rows([embedding_row], ch=ch)

    with traced("ch.insert.dual_write", concurrent=2):
        memory_done, embedding_done = await asyncio.gather(
            _insert_memory(), _insert_embedding(), return_exceptions=True
        )
    if isinstance(memory_done, BaseException):
        raise memory_done
    try:
        if isinstance(embedding_done, BaseException):
            raise embedding_done
        await _lift_after_insert(w, ch, [embedding_row])
    finally:
        await _announce_set(w)  # the row is in, whatever failed after it
    # Its version was drawn when the write began: a slow write indexes its own BM25 stats.
    after_write.index_late_rows([embedding_row], ch=ch)

    # Post-insert (so the new row's UUID exists for the cross-link): flag
    # both the new row and every close-but-different peer as conflicted.
    if conflict_result.verdict == "conflict":
        with traced("conflicts.mark_peers", peers=len(conflict_result.peers)):
            landed = await mark_peers_conflicted(
                org=input.org,
                new_memory_id=memory_id,
                peer_ids=[p.memory_id for p in conflict_result.peers],
            )
        if landed != "conflict":
            # Every peer, or the new row itself, retired before the mark:
            # nothing was flagged.
            conflict_result = replace(conflict_result, verdict="clear", peers=[])
    elif conflict_result.verdict == "refines" and conflict_result.refined_peers:
        # A templated substitution supersedes the rows it replaces; nothing is
        # flagged, nothing is deleted.
        from server.memory.app.conflicts import apply_refines

        with traced("conflicts.apply_refines", peers=len(conflict_result.refined_peers)):
            await apply_refines(
                org=input.org,
                namespace=input.namespace,
                new_memory_id=memory_id,
                new_memory_key=input.memory_key,
                refined_peers=conflict_result.refined_peers,
                ch=ch,
                follows=w.follows,
            )
    return conflict_result


async def _set_after_write(
    w: _SetWrite,
    ch: Any,
    _cfg: Any,
    conflict_result: Any,
    duplicate_of_id: str,
    peers_out: list[Any],
) -> MemorySetOutput:
    """Phase 13 of memory.set: after-write work on the inserted row, then the answer.

    Post-insert projections (audit, supersession, entities — _set_post_insert),
    the verbatim chunks of a long value, the write tick, the response.
    """
    from server.observability import degradations as _degradations

    input, retyped_from, version, memory_id = w.input, w.retyped_from, w.version, w.memory_id
    new_sha, source_class, writer_actor = w.new_sha, w.source_class, w.writer_actor
    embedder, had_existing_row = w.embedder, w.had_existing_row

    await _set_post_insert(
        input,
        ch=ch,
        cfg=_cfg,
        memory_id=memory_id,
        content_sha256=new_sha,
        source_class=source_class,
        verdict="duplicate" if duplicate_of_id else conflict_result.verdict,
        actor=writer_actor or "-",
        revised=had_existing_row,
        follows=w.follows,
    )
    await project_stated_relations(
        w.memory_row,
        input.value,
        input.relations,
        revised=had_existing_row,
        op="memory.set",
        ch=ch,
    )
    # After the write's own relations, so the follow sees the successor they state.
    follow_beside_response(
        org=input.org,
        namespace=input.namespace,
        pairs=w.follows,
        op="memory.set",
        agent=input.agent,
        user_id=input.user_id,
    )

    # A long value is also written as verbatim chunks under the key, after the row is in and
    # never as a condition of it. A duplicate writes none, as its canonical answers for it:
    # it retires only the chunks of the value this row held, as a long re-set would.
    from server.memory.app import set_chunks as _set_chunks

    if duplicate_of_id:
        chunks_written = 0
        if w.existing is not None:
            await _set_chunks.tombstone_row_chunks_fail_open(w.existing, ch=ch, only_its_own=True)
    else:
        chunks_written = await _set_chunks.write_set_chunks_fail_open(
            input, ch=ch, embedder=embedder
        )
    # The insert happened either way; the verdict says what kind of row it is.
    count_write(
        input.namespace,
        _set_write_outcome(duplicate_of_id, conflict_result.verdict, had_existing_row),
    )
    return MemorySetOutput(
        retyped_from=retyped_from,
        version=version,
        pinned=input.pinned,
        memory_id=memory_id,
        content_sha256=new_sha,
        duplicate_of=peer_id_in_read_scope(conflict_result, duplicate_of_id),
        namespace=input.namespace,
        memory_key=input.memory_key,
        degraded=_degradations.current(),
        conflict_verdict=conflict_result.verdict,
        peers=peers_out,
        next=set_next(conflict_result.verdict, peers_out),
        chunks_written=chunks_written,
        status=str(w.memory_row.get("status") or "active"),
    )


def _same_key_batch(input: MemorySetInput, memory_id: str) -> list[dict[str, Any]]:
    """The one-row batch same-key supersession takes, with the serialized value (the form
    the row stores)."""
    stored = json.dumps(input.value, default=str) if input.value is not None else ""
    return [{"memory_id": memory_id, "memory_key": input.memory_key, "value": stored}]


async def _set_post_insert(
    input: MemorySetInput,
    *,
    ch: Any,
    cfg: Any,
    memory_id: str,
    content_sha256: str,
    source_class: str,
    verdict: str,
    actor: str,
    revised: bool = False,
    follows: list[Follow] | None = None,
) -> None:
    """What a committed point write does after its row is in.

    Same-key supersession changes which rows a read serves (its record awaited, its
    graph follow left in `follows` for after the write's relations), and the chained
    audit row is the write's receipt: both stay awaited, in ONE round trip, not two.
    The entity-graph projection is a fail-open side effect nothing on this path reads (the
    entity_extractor worker re-projects a row it missed): it is written beside the response
    instead of in it, because its canonical read + insert + edge inserts would be the serial
    tail of every set (5–6 s when inserts are slow). A revision (`revised`) awaits it with
    the tail: the close of what it dropped runs next, and keeps an edge open only for a
    value whose edge links are in."""
    from server.memory.app.key_supersession import supersede_same_key_rows
    from server.observability import audit as _audit_mod
    from server.observability.otel import traced

    async def _supersession() -> None:
        batch = _same_key_batch(input, memory_id)
        await supersede_same_key_rows(
            org=input.org,
            namespace=input.namespace,
            new_rows=batch,
            ch=ch,
            follows=[] if follows is None else follows,
            policy=cfg.policy,
            agent=input.agent,
        )

    # Every point write leaves a hash in the chained audit log: key, id, content hash,
    # writer, class, verdict — never the value.
    async def _audit() -> None:
        try:
            await _audit_mod.emit(
                event_type="memory.set",
                org=input.org,
                namespace=input.namespace,
                actor=actor,
                subject={
                    "memory_key": input.memory_key,
                    "memory_id": memory_id,
                    "content_sha256": content_sha256,
                    "source_class": source_class,
                    "verdict": verdict,
                    "pinned": bool(input.pinned),
                },
            )
        except Exception as e:  # noqa: BLE001 — the audit row never fails the write
            log.warning("memory.set: audit emit failed (non-fatal): %s", e)

    tail = [
        _supersession(),
        _audit(),
        *_entity_projection_tail(
            input, memory_id=memory_id, content_sha256=content_sha256, revised=revised
        ),
    ]
    with traced("memory.set.post_insert", concurrent=len(tail)):
        await asyncio.gather(*tail)


def _entity_projection_tail(
    input: MemorySetInput,
    *,
    memory_id: str,
    content_sha256: str,
    revised: bool,
    ents: list[str] | None = None,
    high: bool = False,
) -> list[Awaitable[Any]]:
    """The entity-graph projection of a committed point write, as what its tail awaits.

    Every one is fenced on the value it projects (`content_sha256`): it writes only
    while the row still holds that value, and ends its edges if a revision overtook
    them. A write with fewer than two entities projects no edge: no task, no mark."""
    from server.memory.app import graph_write_marks
    from server.memory.app.set_entities import set_entities_for_projection

    if ents is None:
        ents = set_entities_for_projection(input, input.value)
    if len(ents) < 2:
        return []
    projection = _project_set_entities_fail_open(
        input,
        memory_id=memory_id,
        wait_visible=True,
        ents=ents,
        content_sha256=content_sha256,
        high=high,
    )
    if revised:
        # Its edges flushed before the close runs and before the write answers, so a walk
        # on any replica reads them and a failed flush is reported, not dropped.
        return [projection]
    # A first write does not wait. Nothing waits on this task but a read of the same
    # namespace (`after_write.settle`), so its edges wait for their flush: once it is
    # done, they are readable and the fence can see them.
    task = after_write.schedule(
        projection, name="loom-set-entities", scope=(input.org, input.namespace)
    )
    # In flight for every process before this write answers: a walk on another
    # process waits for these edges.
    return [graph_write_marks.track(input.org, input.namespace, task)]


def _merged_into_key(existing: Any, input: Any) -> str:
    """The key a `merge` folded this write into, "" if it is
    the caller's own key (or unknown)."""
    key = str(getattr(existing, "memory_key", "") or "")
    return key if key and key != input.memory_key else ""
