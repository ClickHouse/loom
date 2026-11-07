"""Memory wire-op handlers; their wire models live in the
server.memory.api.models_<group> modules."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.naming import named
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_admin import (
    MemoryAgentsListInput,
    MemoryAgentsListOutput,
    MemoryReindexInput,
    MemoryReindexOutput,
    MemoryResolveOrphanDuplicatesInput,
    MemoryResolveOrphanDuplicatesOutput,
)
from server.memory.api.models_graph import (
    MemoryAssignCategoryInput,
    MemoryAssignCategoryOutput,
    MemoryLinkInput,
    MemoryLinkOutput,
)
from server.memory.api.models_lifecycle import (
    MemoryRejectInput,
    MemoryRejectOutput,
)
from server.memory.api.models_temporal import (
    MemoryOutcomeInput,
    MemoryOutcomeOutput,
    MemoryOutcomeRunInput,
    MemoryOutcomeRunOutput,
)
from server.memory.domain.lock import refuses_retirement
from server.memory.store import categories
from server.memory.store.lifecycle import reject_by_identity

log = logging.getLogger(__name__)


async def handle_memory_outcome(input: MemoryOutcomeInput) -> MemoryOutcomeOutput:
    """Record an outcome callback against a memory row.

    Positive appends an evidence usage event (feeds ranking + promote gate).
    Negative is audit-logged and counted; once the negative count crosses
    LOOM_MEMORY_NEGATIVE_DEMOTE_THRESHOLD (>0) the memory is auto-quarantined
    (status -> 'rejected') unless it is locked. The evidence count is read back
    from the totals table and is eventually consistent.
    """
    import server.memory.store as memory_repo
    from server.core.ch import get_client

    ch = get_client()
    # Resolve the row first, by key or by id, under the request's scope: a row
    # outside it (or in another namespace) answers like a missing one, before
    # any counter is read or written.
    if input.memory_id:
        row = await memory_repo.get_by_id(
            input.memory_id, org=input.org, fresh=True, scope=request_scope()
        )
        if row is not None and row.namespace != input.namespace:
            row = None
    elif input.memory_key:
        ident = memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent or "*",
            user_id=input.user_id or "-",
            session_id="-",
        )
        row = await memory_repo.get_by_key(
            ident, input.memory_key, fresh=True, scope=request_scope()
        )
    else:
        row = None
    if row is None:
        return MemoryOutcomeOutput(recorded=False, evidence_count_after=0)
    memory_id = str(row.memory_id)

    if input.outcome in ("positive", "used"):
        try:
            await memory_repo.record_usage_event(
                memory_id=memory_id,
                org=input.org,
                namespace=input.namespace,
                # `used` = relied on (search-side reinforcement); `evidence` =
                # positive outcome (promote gate + reinforcement).
                event_type="evidence" if input.outcome == "positive" else "used",
                event_count=int(input.weight),
                ch=ch,
            )
        except Exception:  # noqa: BLE001 — a failed usage write reports recorded=False
            return MemoryOutcomeOutput(recorded=False, evidence_count_after=0)

    quarantined = False
    if input.outcome == "negative":
        from server.settings import settings as _s

        # `harm`: the caller relied on this row and the work ended badly. It
        # is counted in the totals (memory.health) and subtracted from the
        # search-side reinforcement at LOOM_MEMORY_HARM_REINFORCEMENT_WEIGHT,
        # floored at the write-time base. Distinct from the `rejection` counter below, which
        # is the quarantine gate.
        try:
            await memory_repo.record_usage_event(
                memory_id=memory_id,
                org=input.org,
                namespace=input.namespace,
                event_type="harm",
                event_count=int(input.weight),
                ch=ch,
            )
        except Exception as e:  # noqa: BLE001 — best effort, the audit row stands
            log.warning("memory.outcome harm event failed for %s: %s", memory_id, e)

        threshold = int(_s.memory_negative_demote_threshold)
        if threshold > 0:
            # Count this rejection synchronously so the gate sees it now.
            await ch.insert(
                "loom.memory_usage_events",
                [
                    {
                        "memory_id": memory_id,
                        "org": input.org,
                        "namespace": input.namespace,
                        "event_type": "rejection",
                        "event_count": int(input.weight),
                        "agent": "",
                        "ts": naive_utc_now(),
                    }
                ],
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            )
            cnt = await ch.query_one(
                "SELECT sum(event_count) AS n FROM loom.memory_usage_events "
                "WHERE org = {org:String} AND namespace = {ns:String} "
                "AND memory_id = {mid:String} AND event_type = 'rejection'",
                params={"org": input.org, "ns": input.namespace, "mid": memory_id},
            )
            if int((cnt or {}).get("n") or 0) >= threshold:
                idr = await ch.query_one(
                    "SELECT agent, user_id, memory_key FROM loom.memory FINAL "
                    "WHERE org = {org:String} AND namespace = {ns:String} "
                    "AND memory_id = {mid:UUID} AND tombstoned_at IS NULL",
                    params={"org": input.org, "ns": input.namespace, "mid": memory_id},
                )
                # Leave a locked memory and all its copies alone; otherwise leave only the
                # locked copies.
                if idr and not refuses_retirement(row):
                    written = await reject_by_identity(
                        input.org,
                        input.namespace,
                        str(idr["agent"]),
                        str(idr["user_id"]),
                        str(idr["memory_key"]),
                        f"auto-quarantine: {int((cnt or {}).get('n') or 0)} negative outcomes",
                        skip=refuses_retirement,
                        ch=ch,
                    )
                    quarantined = written > 0

    usage = await ch.query_one(
        """
        SELECT sum(evidence_count) AS evidence_count
        FROM loom.memory_usage_totals
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_id = {mid:String}
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "mid": memory_id,
        },
    )
    evidence_count = int((usage or {}).get("evidence_count") or 0)
    from server.memory.app.recall_credit import note_closer_from_request

    note_closer_from_request(input.org)  # this consumer closes loops
    return MemoryOutcomeOutput(
        recorded=True,
        evidence_count_after=evidence_count,
        quarantined=quarantined,
    )


async def handle_memory_reject(
    input: MemoryRejectInput,
) -> MemoryRejectOutput:
    """Mark a memory's live rows 'rejected' (`reject_by_identity`)."""

    from server.core.ch import get_client

    await reject_by_identity(
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.memory_key,
        input.rejection_reason or "",
        ch=get_client(),
    )
    return MemoryRejectOutput(
        rejected=True,
        rejected_at=datetime.now(UTC),
    )


def _reindex_texts(r: dict[str, Any]) -> tuple[str, str, str]:
    """(excerpt, embed text, key-axis text) of a stored row: memory.set's own
    builder over the stored value (the mirror row carries it as
    `content_excerpt`; a procedural value is parsed back to the dict its text
    comes from)."""
    from server.memory.domain.embed_text import embedding_texts

    serialized = str(r.get("content_excerpt") or "")
    value: Any = serialized
    if str(r.get("memory_type") or "") == "procedural":
        try:
            value = json.loads(serialized)
        except ValueError:
            value = serialized
    return embedding_texts(
        str(r.get("memory_type") or ""), value, serialized, str(r.get("memory_key") or "")
    )


async def _recheck_reindexed_heads(
    rows: list[dict[str, Any]], written: list[dict[str, Any]], *, ch: Any, model: str
) -> None:
    """The heads, re-read after the insert, against what the reindex read (`rows`, each
    with the `memory_version` it read) and wrote (`written`, the same order). No
    write below takes a fresh version: each sits just above the newest version it
    read, so a 'high' write that lands later still fences above it. (The copy paths —
    touch, verify, apply, the entity tagger — never copy a vector under a head that
    is not embedded, so nothing re-emits the reindexed row under a 'high' head.)

    - A head no longer embedded ('high', unsearchable, gone): the key's vectors are
      retired two above the newest version read (the reindexed row, the head).
    - A head whose version changed with the same value and type (a touch, a verify, a
      tag): the reindexed vectors are re-emitted on the current head's columns.
    - A head whose value changed (a newer write landed between the reads, its vector
      may sit under the reindexed one): a dim-0 stub of the current head for the embed
      worker, which fills it at + 1 from the head's value."""
    import server.memory.store as memory_repo
    from server.memory.domain.memory_types import is_embedded
    from server.memory.store import read_your_writes_enabled

    sequential = read_your_writes_enabled()
    read = {memory_repo.embedding_key(r): (r, w) for r, w in zip(rows, written, strict=True)}
    heads = await memory_repo.memory_rows_by_identity_keys(list(read), ch=ch, sequential=sequential)
    gone: list[tuple[Any, int]] = []
    changed: list[Any] = []
    for key, (r, w) in read.items():
        head = heads.get(key)
        if head is None or not is_embedded(head.memory_type, head.sensitivity):
            # Two above the newest version read: a read + 1 copy of the reindexed row
            # (touch, verify, the entity tagger) inserted after this retire stays below.
            newest = max(int(w["version"]), int(head.version) if head is not None else 0)
            gone.append((key, newest + 2))
        elif int(head.version) != int(r["memory_version"]):
            changed.append(key)
    if gone:
        await memory_repo.retire_vectors_for_keys(gone, ch=ch)
    if not changed:
        return
    mirror = await memory_repo.memory_mirror_rows_for_keys(changed, ch=ch, sequential=sequential)
    out: list[dict[str, Any]] = []
    for m in mirror:
        r, w = read[memory_repo.embedding_key(m)]
        cols = {k: v for k, v in m.items() if k not in ("created_at", "version")}
        version = max(int(m["version"]), int(w["version"])) + 1
        same_text = (m["content_excerpt"], m["memory_type"]) == (
            r["content_excerpt"],
            r["memory_type"],
        )  # the embed text depends on both
        if same_text:  # keep the vectors
            vectors = {
                c: w[c]
                for c in (
                    "content_excerpt",
                    "embedding_model",
                    "embedding_dim",
                    "embedding",
                    "memory_key_embedding",
                    "memory_key_embedding_dim",
                )
            }
            out.append(
                memory_repo.build_embedding_row(
                    **(cols | vectors),
                    embed_attempts=1,
                    created_at=m["created_at"],
                    version=version,
                )
            )
        else:
            out.append(
                memory_repo.build_embedding_row(
                    **cols,
                    embedding_model=model,
                    embedding_dim=0,
                    embedding=[],  # dim 0: the worker's queue
                    tombstoned_at=None,
                    embed_attempts=0,
                    created_at=m["created_at"],
                    version=version,
                )
            )
    if out:
        await memory_repo.insert_embedding_rows(out, ch=ch)
        await memory_repo.retire_copies_under_unembedded_heads(out, ch=ch)


async def handle_memory_reindex(input: MemoryReindexInput) -> MemoryReindexOutput:
    """Re-embed a namespace's memories whose vectors were made by another embedding model (or all of
    them with `force`)."""
    import server.memory.store as memory_repo
    from server.core.ch import get_client
    from server.embedding import get_embedder

    embedder = get_embedder()
    ch = get_client()
    target_model = embedder.model_name
    rows = await memory_repo.find_memories_to_reindex(
        input.org,
        input.namespace,
        embedding_model=target_model,
        force=input.force,
        limit=input.limit,
        ch=ch,
    )

    if not rows:
        return MemoryReindexOutput(
            processed=0,
            skipped_already_current=0,
            target_model=target_model,
            target_dim=embedder.dim,
        )

    texts = [_reindex_texts(r) for r in rows]
    # One batch: every value text, then the key texts that exist (memory.set
    # embeds both), strictly paired back.
    keyed = [i for i, (_e, _t, key) in enumerate(texts) if key]
    batch = [t for _e, t, _k in texts] + [texts[i][2] for i in keyed]
    if hasattr(embedder, "embed_many"):
        vectors = await embedder.embed_many(batch)
    else:
        vectors = [await embedder.embed(t) for t in batch]
    if len(vectors) != len(batch):
        raise ValueError(f"embed_many returned {len(vectors)} vectors for {len(batch)} inputs")
    key_vecs: dict[int, list[float]] = {i: vectors[len(rows) + j] for j, i in enumerate(keyed)}

    # The embedding row mirrors its memory row (status, validity, provenance,
    # facets): a row built from defaults would serve a superseded fact as
    # active and drop its attestation under FINAL. Its excerpt and vectors are
    # memory.set's, so a reindexed row sits where the same row written fresh
    # would; a key without axis text gets memory.set's zero vector at dim 0.
    embedding_rows = []
    for i, r in enumerate(rows):
        key_vec = key_vecs.get(i, [])
        embedding_rows.append(
            memory_repo.build_embedding_row(
                **{k: v for k, v in r.items() if k not in ("memory_version", "content_excerpt")},
                content_excerpt=texts[i][0],
                embedding_model=target_model,
                embedding_dim=embedder.dim,
                embedding=vectors[i],
                memory_key_embedding=list(key_vec) or [0.0] * embedder.dim,
                memory_key_embedding_dim=embedder.dim if key_vec else 0,
                embed_attempts=1,
            )
        )
    await memory_repo.insert_embedding_rows(embedding_rows, ch=ch)
    await _recheck_reindexed_heads(rows, embedding_rows, ch=ch, model=target_model)
    # One above versions read before the embed call: under the BM25 watermark.
    await memory_repo.index_late_rows(embedding_rows, ch=ch)

    return MemoryReindexOutput(
        processed=len(embedding_rows),
        skipped_already_current=0,
        target_model=target_model,
        target_dim=embedder.dim,
    )


async def handle_memory_resolve_orphan_duplicates(
    input: MemoryResolveOrphanDuplicatesInput,
) -> MemoryResolveOrphanDuplicatesOutput:
    """Re-activate orphaned duplicate rows in (org, namespace).

    Scans for active-head rows stuck at status='duplicate' whose canonical is
    gone (duplicate_of empty, or pointing at a memory that's no longer a live
    active head), then re-emits each with status='active' — carrying every
    column forward via the RMT version-re-emit path (conflicts_repo.
    mark_active_by_ids → memory_repo.write_versions_by_ids). Reused rather than
    a bespoke writer because the need is identical to the conflicts 'contract'/
    'allow' status flip: re-emit the same rows active, nothing else changes.

    `dry_run=True` returns the candidates without writing. Idempotent: a
    re-activated row is no longer status='duplicate', so a re-run finds nothing.
    """
    import server.memory.store as memory_repo
    from server.memory.store import conflicts as conflicts_repo

    rows = await memory_repo.find_orphan_duplicates(
        input.org,
        input.namespace,
        batch_size=input.limit,
    )
    memory_ids = [r.memory_id for r in rows if r.memory_id]

    if input.dry_run:
        return MemoryResolveOrphanDuplicatesOutput(
            candidates=len(memory_ids),
            reactivated=0,
            dry_run=True,
            memory_ids=memory_ids,
        )

    await conflicts_repo.mark_active_by_ids(input.org, memory_ids)
    return MemoryResolveOrphanDuplicatesOutput(
        candidates=len(memory_ids),
        reactivated=len(memory_ids),
        dry_run=False,
        memory_ids=memory_ids,
    )


async def handle_memory_agents_list(
    input: MemoryAgentsListInput,
) -> MemoryAgentsListOutput:
    from server.core.ch import get_client

    ch = get_client()
    rows = await ch.query_all(
        """
        SELECT DISTINCT agent
        FROM loom.memory_embeddings
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND tombstoned_at IS NULL
        ORDER BY agent
        """,
        params={"org": input.org, "ns": input.namespace},
    )
    return MemoryAgentsListOutput(
        agents=[r["agent"] for r in rows if r.get("agent")],
    )


_EDGE_EVIDENCE_ITEM_LIMIT = 500


async def _open_conflict_for_edge(org: str, namespace: str, from_key: str, to_key: str) -> bool:
    """An agent-asserted `contradicts` edge between two LIVE memories flags
    both rows conflicted (conflict_with populated) so the contradiction
    reaches conflicts.list next to the ones loom detects on write. Entity
    endpoints (not memory keys) open nothing. Best-effort: the edge is
    written regardless."""
    import server.memory.store as memory_repo
    from server.memory.app.conflicts import mark_peers_conflicted

    ident = memory_repo.Identity(
        org=org, namespace=namespace, agent="*", user_id="-", session_id="-"
    )
    try:
        a = await memory_repo.get_by_key(
            ident, from_key, fresh=True, strong=True, scope=WHOLE_NAMESPACE
        )
        b = await memory_repo.get_by_key(
            ident, to_key, fresh=True, strong=True, scope=WHOLE_NAMESPACE
        )
    except Exception as e:  # noqa: BLE001 — the edge is kept
        log.warning("memory.link contradicts: lookup failed (%s); edge kept", e)
        return False
    if a is None or b is None or not getattr(a, "memory_id", "") or not getattr(b, "memory_id", ""):
        return False
    if str(a.memory_id) == str(b.memory_id):
        return False
    try:
        # A superseded or retired endpoint is not flagged: no conflict opened then.
        verdict = await mark_peers_conflicted(
            org=org, new_memory_id=str(a.memory_id), peer_ids=[str(b.memory_id)]
        )
    except Exception as e:  # noqa: BLE001 — the edge is kept
        log.warning("memory.link contradicts: mark_peers_conflicted failed: %s", e)
        return False
    return verdict == "conflict"


async def handle_memory_link(input: MemoryLinkInput) -> MemoryLinkOutput:
    """Insert one row into loom.operator_edges, carrying its justification
    (explanation / evidence / confidence / run_id) and a canonical verb; a
    `contradicts` edge between two memories also opens a conflict."""

    from server.auth.context import current_auth
    from server.core.ch import get_client
    from server.memory.domain.edge_relations import canonical_edge

    ch = get_client()
    author = ""
    try:
        auth = current_auth()
        author = (auth.subject if auth else "") or ""
    except Exception:  # noqa: BLE001 — background callers have no auth context
        # Background callers (workers, CLI) may not have an auth context.
        pass

    # An edge without a verb is a co-occurrence, which loom.entity_edges already records, and
    # most authored edges arrive without one. Store the honest fallback and say so on the
    # output, so the row can guide a read and the ledger can count the habit.
    from_key, to_key, relation, relation_defaulted = canonical_edge(
        input.from_key, input.to_key, input.relation
    )
    evidence = [
        str(e).strip()[:_EDGE_EVIDENCE_ITEM_LIMIT] for e in (input.evidence or []) if str(e).strip()
    ]

    now = naive_utc_now()
    row = {
        "org": input.org,
        "namespace": input.namespace,
        "from_key": from_key,
        "to_key": to_key,
        "relation": relation,
        "label": input.label,
        "explanation": input.explanation.strip(),
        "evidence": evidence,
        "confidence": float(input.confidence),
        "weight": float(input.weight),
        "author": author,
        "run_id": (input.run_id or "-").strip() or "-",
        "created_at": now,
        "tombstoned_at": now if input.tombstone else None,
    }
    await ch.insert("loom.operator_edges", [row])

    conflict_opened = False
    if relation == "contradicts" and not input.tombstone and from_key != to_key:
        conflict_opened = await _open_conflict_for_edge(
            input.org, input.namespace, from_key, to_key
        )
    return MemoryLinkOutput(
        linked=not input.tombstone,
        from_key=from_key,
        to_key=to_key,
        relation=relation,
        confidence=float(input.confidence),
        conflict_opened=conflict_opened,
        relation_defaulted=relation_defaulted,
    )


async def handle_memory_assign_category(
    input: MemoryAssignCategoryInput,
) -> MemoryAssignCategoryOutput:
    """Link the key to the named category, or rescind the link; a name the namespace
    has not registered is a 404. memory.import links through the same statements."""
    ids = await categories.registered_ids(input.org, input.namespace, [input.category_name])
    cat_id = ids.get(input.category_name)
    if cat_id is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404,
            detail={
                "code": "CATEGORY_NOT_FOUND",
                "message": (
                    f"Category {input.category_name!r} is not registered "
                    f"in {input.org}/{input.namespace}. "
                    "Call namespace.register_category first."
                ),
            },
        )
    await categories.link(
        input.org, input.namespace, [(input.memory_key, cat_id)], tombstone=input.tombstone
    )
    return MemoryAssignCategoryOutput(
        assigned=not input.tombstone,
        category_id=cat_id,
    )


async def handle_memory_outcome_run(input: MemoryOutcomeRunInput) -> MemoryOutcomeRunOutput:
    """Apply an outcome to every memory recalled under a run_id, in one call.

    The run→recalled-keys mapping comes from loom.search_trace_log (returned_keys
    per run_id), so this requires LOOM_SEARCH_TRACE_LOG_ENABLED. Positive records
    an evidence event per resolved memory (feeds ranking via usage_fold);
    negative records a harm event per resolved memory (the memories a failed run relied on
    lose earned reinforcement, floored at
    their write-time base, never their cosine rank), matching memory.outcome.
    """
    import server.memory.store as memory_repo
    from server.core.ch import get_client

    ch = get_client()
    # Prefer the explicit key set the caller recalled; fall back to the run's
    # recorded keys in the trace log when only a run_id is given.
    keys = [str(k) for k in (getattr(input, "memory_keys", None) or []) if k]
    if not keys and named(input.run_id):
        key_rows = await ch.query_all(
            """
            SELECT DISTINCT arrayJoin(returned_keys) AS memory_key
            FROM loom.search_trace_log
            WHERE org = {org:String} AND namespace = {ns:String}
              AND run_id = {rid:String}
            LIMIT {lim:UInt32}
            """,
            params={
                "org": input.org,
                "ns": input.namespace,
                "rid": input.run_id,
                "lim": int(input.max_keys),
            },
        )
        keys = [str(r["memory_key"]) for r in key_rows if r.get("memory_key")]
    keys = keys[: int(input.max_keys)]
    if not keys:
        return MemoryOutcomeRunOutput(applied=0, keys_matched=0, run_id=input.run_id)

    id_rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS memory_id, memory_key
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
        """,
        params={"org": input.org, "ns": input.namespace, "keys": keys},
    )
    applied = 0
    event_type = {"positive": "evidence", "used": "used", "negative": "harm"}[input.outcome]
    for r in id_rows:
        try:
            await memory_repo.record_usage_event(
                memory_id=r["memory_id"],
                org=input.org,
                namespace=input.namespace,
                event_type=event_type,
                event_count=int(input.weight),
                ch=ch,
            )
            applied += 1
        except Exception:  # noqa: BLE001 - best-effort, per-row
            continue
    from server.memory.app.recall_credit import note_closer_from_request

    note_closer_from_request(input.org)  # this consumer closes loops
    return MemoryOutcomeRunOutput(applied=applied, keys_matched=len(keys), run_id=input.run_id)
