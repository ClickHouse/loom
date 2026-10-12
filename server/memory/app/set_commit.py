"""The commit half of memory.set: embed the row, take the conflict verdict,
then merge, answer a dry run, or insert (plain or with its embedding row),
and run what follows a write. `_SetWrite` carries one call's state through
the phases; `_handle_memory_set` in server/memory/app/write.py orders them."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now
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
from server.memory.app import conflict_deferred as _cd
from server.memory.app.drops import record_drop
from server.memory.domain.embed_text import embedding_texts
from server.memory.domain.trust import attests
from server.memory.store.inserts import insert_memory_versions

log = logging.getLogger(__name__)


def _count_write(namespace: str, outcome: str) -> None:
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


def _peer_id_in_read_scope(conflict_result: Any, memory_id: str) -> str:
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


def _set_peers(conflict_result: Any) -> list[Any]:
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


def _set_next(verdict: str, peers: list[Any], *, repeated_merge: bool = False) -> str:
    return set_next_hint(verdict, peers, repeated_merge=repeated_merge)


async def _project_set_entities_fail_open(
    input: MemorySetInput, *, wait_visible: bool = False
) -> None:
    """Deterministic LLM-free entity-graph projection for a committed
    `memory.set`. A point write carries no `entities` column, so without
    this it stays invisible to the graph until a bulk path or the
    entity_extractor worker re-extracts it. Fail-open: any error logs a
    warning and the write still succeeds. `wait_visible` (the projection
    beside the response) returns only once its edges are readable.
    """
    try:
        from server.memory.app.extract.handler import project_memory_set_entities

        await project_memory_set_entities(
            input,
            memory_key=input.memory_key,
            value=input.value,
            wait_visible=wait_visible,
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
    writer_actor: str = ""
    source_class: str = ""
    memory_row: dict[str, Any] = field(default_factory=dict)
    embedder: Any = None
    excerpt: str = ""
    embedding: list[float] = field(default_factory=list)
    key_embedding: list[float] = field(default_factory=list)
    key_text: str = ""


async def _set_plain_insert(w: _SetWrite, ch: Any) -> MemorySetOutput:
    """Phase 7 of memory.set: the non-embedded write — one loom.memory row, no peers.

    Taken for a non-searchable type or a 'high' sensitivity row. A dry run
    answers without writing; otherwise insert, project entities, count.
    """
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
    with traced("ch.insert.memory", table="loom.memory"):
        await insert_memory_versions([memory_row], ch=ch)
    await _project_set_entities_fail_open(input)
    # Non-searchable types skip the conflict check entirely, so there is no
    # verdict to map: the insert always happened.
    _count_write(input.namespace, "updated" if had_existing_row else "created")
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
    #   pending  → the judge is deferred: insert active, settle after
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
            defer_judge=_cd.wanted(input),
            **_cfg.check_kwargs(),
        )
    # The rows the verdict was reached against ride the response so the caller can act on
    # the verdict without a second read.
    peers_out = _set_peers(conflict_result)
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
                next=_set_next("rejected", peers_out),
                noop=True,
                dry_run=True,
            ),
        )
    if conflict_result.verdict == "rejected":
        # Counted at the handler boundary via _WRITE_OUTCOME_BY_ERROR_CODE.
        # A blocker outside the read scope still blocks but is not named.
        blocker = _peer_id_in_read_scope(conflict_result, conflict_result.pinned_blocker_id or "")
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
            next=_set_next("merge", merge_peers, repeated_merge=repeated_merge),
            noop=True,
            dry_run=True,
        )
    # Auto-reinforce: bump evidence_count on the existing row when the
    # namespace policy is "auto_reinforce". Fire-and-forget.
    if merged_into_id:  # never a row outside the read scope
        await _cf.auto_reinforce_peer(input, merged_into_id, ch=ch, cfg=_cfg)
    # Canonical-form match: the caller restated a memory Loom already had.
    # No row was inserted, so this is the outcome that separates "still
    # ingesting" from "only ever hearing the same things again".
    _count_write(input.namespace, "merged")
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
        next=_set_next("merge", merge_peers, repeated_merge=repeated_merge),
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
        duplicate_of=_peer_id_in_read_scope(conflict_result, duplicate_of_id),
        namespace=input.namespace,
        memory_key=input.memory_key,
        degraded=_degradations.current(),
        conflict_verdict=conflict_result.verdict,
        peers=peers_out,
        next=_set_next(conflict_result.verdict, peers_out),
        noop=True,
        dry_run=True,
    )


async def _set_dual_write(w: _SetWrite, ch: Any, _cfg: Any, conflict_result: Any) -> None:
    """Phase 12 of memory.set: the version row and its embedding row, then the peer links.

    Builds the full-column embedding row, inserts both rows concurrently,
    schedules the deferred settle, then flags `conflict` peers or applies a
    `refines` supersession — after the insert, so the new id exists.
    """
    from server.memory.app.conflicts import mark_peers_conflicted
    from server.observability.otel import traced

    input, memory_id, memory_row, expires_at = w.input, w.memory_id, w.memory_row, w.expires_at
    source_class = w.source_class
    reinforcement_count_for_insert = w.reinforcement_count
    last_reinforced_at_for_insert = w.last_reinforced_at
    embedder, excerpt, embedding = w.embedder, w.excerpt, w.embedding
    key_embedding, key_text = w.key_embedding, w.key_text
    embedding_dim_for_insert = embedder.dim

    import server.memory.store as memory_repo

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
        await asyncio.gather(_insert_memory(), _insert_embedding())
    _cd.schedule_settle(input, conflict_result, memory_id=memory_id, cfg=_cfg)

    # Post-insert (so the new row's UUID exists for the cross-link): flag
    # both the new row and every close-but-different peer as conflicted.
    if conflict_result.verdict == "conflict":
        with traced("conflicts.mark_peers", peers=len(conflict_result.peers)):
            await mark_peers_conflicted(
                org=input.org,
                new_memory_id=memory_id,
                peer_ids=[p.memory_id for p in conflict_result.peers],
            )
    elif conflict_result.verdict == "refines" and conflict_result.refined_peers:
        # A judged elaboration supersedes the rows it refines and links to them; nothing is
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
                relation=conflict_result.refine_relation,
            )


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
    )

    # A long value is also written as verbatim chunks under the key, after the row is in and
    # never as a condition of it.
    from server.memory.app import set_chunks as _set_chunks

    chunks_written = await _set_chunks.write_set_chunks_fail_open(input, ch=ch, embedder=embedder)
    # The insert happened either way; the verdict says what kind of row it is.
    _count_write(
        input.namespace,
        _set_write_outcome(duplicate_of_id, conflict_result.verdict, had_existing_row),
    )
    return MemorySetOutput(
        retyped_from=retyped_from,
        version=version,
        pinned=input.pinned,
        memory_id=memory_id,
        content_sha256=new_sha,
        duplicate_of=_peer_id_in_read_scope(conflict_result, duplicate_of_id),
        namespace=input.namespace,
        memory_key=input.memory_key,
        degraded=_degradations.current(),
        conflict_verdict=conflict_result.verdict,
        peers=peers_out,
        next=_set_next(conflict_result.verdict, peers_out),
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
) -> None:
    """What a committed point write does after its row is in.

    Same-key supersession changes which rows a read serves, and the
    chained audit row is the write's receipt: both stay awaited, in ONE round trip, not two.
    The entity-graph projection is a fail-open side effect nothing on this path reads (the
    entity_extractor worker re-projects a row it missed): it is written beside the response
    instead of in it, because its canonical read + insert + edge inserts would be the serial
    tail of every set (5–6 s when inserts are slow)."""
    from server.memory.app import after_write
    from server.memory.app.key_supersession import supersede_same_key_rows
    from server.observability import audit as _audit_mod
    from server.observability.otel import traced

    async def _supersession() -> None:
        batch = _same_key_batch(input, memory_id)
        await supersede_same_key_rows(
            org=input.org, namespace=input.namespace, new_rows=batch, ch=ch, policy=cfg.policy
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

    tail = [_supersession(), _audit()]
    # Nothing waits on this task but a read of the same namespace (`after_write.settle`),
    # so its edges wait for their flush: once it is done, they are readable.
    after_write.schedule(
        _project_set_entities_fail_open(input, wait_visible=True),
        name="loom-set-entities",
        scope=(input.org, input.namespace),
    )
    with traced("memory.set.post_insert", concurrent=len(tail)):
        await asyncio.gather(*tail)


def _merged_into_key(existing: Any, input: Any) -> str:
    """The key a `merge` folded this write into, "" if it is
    the caller's own key (or unknown)."""
    key = str(getattr(existing, "memory_key", "") or "")
    return key if key and key != input.memory_key else ""
