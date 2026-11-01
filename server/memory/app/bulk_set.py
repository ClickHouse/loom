"""memory.bulk_set, the batch write path set_from_messages and memory.import write
through: the live-row lookup, identity dedup, entity resolution, conflict verdicts,
the insert, and the projections and audit that follow it."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import hash_text
from server.core.hashing import sha256_value as _content_sha256
from server.core.vectors import codes_state
from server.embedding import get_embedder
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.llm import customer
from server.memory.api.models_bulk import (
    MemoryBulkSetInput,
    MemoryBulkSetOutput,
)
from server.memory.app import after_write, webhooks
from server.memory.app.bulk_rows import (
    _BULK_CARRIED_COLS,
    Outcome,
    _bulk_build_rows,
    _bulk_embed_rows,
    _BulkWriteState,
    settle_steps,
    write_step_results,
)
from server.memory.app.bulk_verdicts import (
    BulkVerdictPlan,
    apply_bulk_verdicts,
)
from server.memory.app.edge_links import drop_undeclared_edges_beside, owes_undeclared_drop
from server.memory.app.edge_repair import EdgeClose, end_overtaken_edges, record_edges_closes
from server.memory.app.graph_holders import edge_holders
from server.memory.app.high_fence import fence_high
from server.memory.app.projection_errors import (
    bulk_projection_payload,
    bulk_source_resource_id,
    failed_projection,
    record_input_projection_failure,
    restated_repair_payload,
)
from server.memory.app.projection_failures import FOLLOW_ROWS
from server.memory.app.projections import (
    edge_nodes,
    value_sha256,
    write_entity_edges,
    write_entity_triples,
    write_entity_vectors,
)
from server.memory.app.provenance import writer_actor
from server.memory.app.set_commit import set_next
from server.memory.app.set_entities import declared_entities
from server.memory.app.triple_projections import reconcile_row_facts, write_derived_facts
from server.memory.app.write_fence import lift_above_deletes, lift_after_insert
from server.memory.domain.conflicts import kept_versions
from server.memory.domain.memory_types import is_embedded
from server.memory.domain.temporal_sql import LIVE_STATUSES
from server.memory.domain.webhook_events import EventType
from server.observability.budget_shape import note_budget_shape

if TYPE_CHECKING:
    from server.memory.app.auto_resolution import Follow
    from server.memory.app.search.clock import StageClock

log = logging.getLogger(__name__)


# Per-request override of settings.memory_extract_value_updates, set by
# handle_memory_set_from_messages from `input.detect_updates`; the bulk write the
# extraction pipeline makes reads it. The count of links that bulk write made rides back
# the same way (reset per request, so a path that writes nothing reports 0).
detect_updates_override: ContextVar[bool | None] = ContextVar(
    "detect_updates_override", default=None
)
last_value_updates_linked: ContextVar[int] = ContextVar("last_value_updates_linked", default=0)


def value_updates_enabled(request_value: bool | None = None) -> bool:
    if request_value is not None:
        return bool(request_value)
    ov = detect_updates_override.get()
    if ov is not None:
        return bool(ov)
    from server.settings import settings as _s

    return bool(_s.memory_extract_value_updates)


async def _bulk_existing_lookup(ch: Any, input: MemoryBulkSetInput, state: _BulkWriteState) -> None:
    """Phase 1: one batched read of the live rows under this session's keys —
    id, content hash, pin, creation time and the carried columns per key — into `state`.
    Fail-open: on an error `state.existing_lookup_ok` stays False."""
    try:
        keys = [entry.memory_key for entry in input.entries]
        if keys:
            import server.memory.store as memory_repo

            # Full rows (a revision carries every column forward), with sequential
            # consistency: the answer decides skip, pin and version per entry.
            live = await memory_repo.memory_rows_by_identity_keys(
                [
                    (input.org, input.namespace, input.agent, input.user_id, input.session_id, k)
                    for k in keys
                ],
                ch=ch,
                sequential=True,
            )
            rows = {ident[5]: r for ident, r in live.items()}
            state.existing_rows = rows
            state.existing_ids = {k: str(r.memory_id) for k, r in rows.items() if r.memory_id}
            state.existing_sha = {
                k: hash_text(r.content_sha256) for k, r in rows.items() if r.content_sha256
            }
            state.existing_pinned = {k: bool(r.pinned) for k, r in rows.items()}
            state.existing_created = {k: r.created_at for k, r in rows.items() if r.created_at}
            state.existing_carried = {
                k: {c: getattr(r, c, None) for c in _BULK_CARRIED_COLS} for k, r in rows.items()
            }
            state.existing_lookup_ok = True
    except Exception as e:  # noqa: BLE001 — writes without prior state
        log.warning("bulk_set existing-id lookup failed (non-fatal): %s", e)


async def _bulk_identity_dupes(ch: Any, input: MemoryBulkSetInput) -> dict[str, tuple[str, str]]:
    """Phase 2: content hash -> (keeper key, keeper memory_id) of the live rows
    under this identity (any session, any key) that already hold an entry's
    exact bytes. Empty when the scope is not `identity` or the read failed."""
    # Identity-scoped exact dedup. The gate above is session-scoped, so the same fact
    # re-observed in a later session, or extracted under a key variant, would land as a new
    # active row (~9% of active rows in real namespaces were exact copies). A live active row
    # in (org, namespace, agent, user_id) with the same content_sha256, under any key, IS the
    # fact: the entry reinforces it instead of duplicating it. content_sha256 carries a
    # bloom-filter index, so this is one cheap read. Both lookups feed a write, so they read
    # with sequential consistency: on ClickHouse Cloud a row written a second ago by another
    # replica is not yet visible to a plain read. The keeper may be `conflicted` or
    # `duplicate` (the reader keeps those live): the same bytes under any live status are
    # still the same fact. Fail-open: a lookup error writes the entry normally.
    identity_dupes: dict[str, tuple[str, str]] = {}
    from server.settings import settings as _settings_dedup

    _dedup_scope = str(_settings_dedup.memory_bulk_dedup_scope)
    if _dedup_scope == "identity" and input.entries:
        try:
            _shas = sorted({_content_sha256(entry.value) for entry in input.entries})
            params: dict[str, Any] = {
                "org": input.org,
                "ns": input.namespace,
                "agent": input.agent,
                "uid": input.user_id,
                "shas": _shas,
                "live": list(LIVE_STATUSES),
            }
            # Under own_session the keeper must be in the caller's session too.
            in_scope = scope_and(request_scope(), params)
            _rows = await ch.query_all(
                f"""
                SELECT content_sha256, memory_key,
                       toString(argMax(memory_id, version)) AS memory_id
                FROM loom.memory
                WHERE org = {{org:String}}
                  AND namespace = {{ns:String}}
                  AND agent = {{agent:String}}
                  AND user_id = {{uid:String}}
                  AND content_sha256 IN {{shas:Array(String)}}{in_scope}
                GROUP BY content_sha256, memory_key
                HAVING argMax(tombstoned_at IS NULL, version) = 1
                   AND argMax(status, version) IN {{live:Array(String)}}
                ORDER BY content_sha256, memory_key
                SETTINGS select_sequential_consistency = 1
                """,
                params=params,
            )
            for _r in _rows:
                identity_dupes.setdefault(
                    hash_text(_r["content_sha256"]), (str(_r["memory_key"]), str(_r["memory_id"]))
                )
        except Exception as e:  # noqa: BLE001 — a dedup read never blocks an ingest
            log.warning("bulk_set identity-dedup lookup failed (non-fatal): %s", e)
    return identity_dupes


async def _bulk_resolve_entities(ch: Any, input: MemoryBulkSetInput) -> dict[str, str]:
    """Phase 3: surface -> canonical entity for every entity and relation end
    the batch names, in one namespace-local resolution. Empty on failure."""
    entity_resolution: dict[str, str] = {}
    try:
        from server.memory.app.entity_resolution import resolve_entities_for_write

        entity_surfaces: list[str] = []
        for entry in input.entries:
            entity_surfaces.extend(str(e) for e in (entry.entities or []) if e)
            for rel in list(entry.relations or []):
                if isinstance(rel, (list, tuple)) and len(rel) == 3:
                    entity_surfaces.extend([str(rel[0]), str(rel[2])])
        entity_resolution = await resolve_entities_for_write(
            ch,
            org=input.org,
            namespace=input.namespace,
            entity_texts=entity_surfaces,
        )
    except Exception as e:  # noqa: BLE001 — surface forms stay unresolved
        log.warning("entity canonical resolution failed (non-fatal): %s", e)
    return entity_resolution


def _bulk_log_skips(state: _BulkWriteState) -> None:
    """Phase 8a, after the verdicts: one log line per kind of entry the batch did not write."""
    if state.skipped_unchanged:
        log.info(
            "bulk_set: re-ingest skip-gate skipped %d unchanged entr%s",
            state.skipped_unchanged,
            "y" if state.skipped_unchanged == 1 else "ies",
        )
    if state.skipped_blacklisted:
        log.warning(
            "bulk_set: Refuse dropped %d blacklisted entr%s",
            len(state.skipped_blacklisted),
            "y" if len(state.skipped_blacklisted) == 1 else "ies",
        )
    if state.skipped_pinned:
        log.warning(
            "bulk_set: skipped %d entr%s targeting pinned key(s): %s",
            len(state.skipped_pinned),
            "y" if len(state.skipped_pinned) == 1 else "ies",
            state.skipped_pinned,
        )
    if state.skipped_locked:
        log.warning(
            "bulk_set: skipped %d entr%s targeting read_only-locked key(s): %s",
            len(state.skipped_locked),
            "y" if len(state.skipped_locked) == 1 else "ies",
            state.skipped_locked,
        )


async def _bulk_verdicts(
    input: MemoryBulkSetInput,
    state: _BulkWriteState,
    emb_idx_of: list[int],
    value_vectors: list[list[float]],
    clock: StageClock,
) -> BulkVerdictPlan:
    """Phase 8: the conflict verdicts, folded into the rows before the insert;
    `state.memory_rows` / `state.embedding_rows` become the rows that land."""
    # Conflict surfacing, the same four verdicts memory.set produces. Runs before the insert
    # so a merge or a pinned rejection never lands a row; duplicate / conflicted are stamped
    # on the rows.
    verdicts = await _bulk_conflict_verdicts(
        input,
        state.memory_rows,
        state.entry_metas,
        state.pending,
        value_vectors,
        cap=state.conflict_check_cap,
    )
    clock.mark("verdicts")
    plan = _apply_verdicts(state, emb_idx_of, verdicts)
    state.key_of = {
        str(r.get("memory_id") or ""): str(r.get("memory_key") or "") for r in state.memory_rows
    }
    return plan


def _apply_verdicts(
    state: _BulkWriteState, emb_idx_of: list[int], verdicts: dict[int, Any]
) -> BulkVerdictPlan:
    """Replay the batch now that the verdicts are known (a row an earlier entry's landed
    pin refuses, or one its key already holds, is dropped), fold the verdicts into the
    rows that land or that a verdict keeps out, then write every entry's result."""
    built = [s for s in state.steps if s.repeats is None]  # one per row, in row order
    for i, step in enumerate(built):
        step.verdict = verdicts.get(i)
    for pos, i in enumerate(emb_idx_of):
        built[i].embedding = state.embedding_rows[pos]
    settle_steps(state)
    # In row order: a repeat that lands an earlier row stands next to it.
    position = {step: i for i, step in enumerate(built)}
    judged = sorted(
        (s for s in state.steps if s.outcome in (Outcome.LAND, Outcome.RELABEL, Outcome.KEPT_OUT)),
        key=lambda s: position[s.repeats or s],
    )
    plan = apply_bulk_verdicts(
        [s.row for s in judged],
        [s.embedding for s in judged if s.embedding is not None],
        [i for i, s in enumerate(judged) if s.embedding is not None],
        [s.result for s in judged],
        # A repeat landing an earlier row has no verdict of its own: it answers clear.
        {i: s.verdict for i, s in enumerate(judged) if s.verdict is not None},
        stale_rows={id(s.row) for s in judged if s.stale},
        landed_rows={id(s.row) for s in judged if s.outcome in (Outcome.LAND, Outcome.RELABEL)},
    )
    write_step_results(state, plan)
    state.memory_rows = plan.memory_rows
    state.embedding_rows = plan.embedding_rows
    return plan


async def _bulk_mark_same_key(ch: Any, input: MemoryBulkSetInput, state: _BulkWriteState) -> None:
    """Phase 8b: with the conflict resolver on, a row is owed a judgement when an
    earlier row of the batch of its type sits at the resolver floor (the verdicts
    never compare siblings), or when another session of the same subject holds its
    key live (outside `newest_wins`, which retires the older row itself after the
    insert; `keys_held_elsewhere`). Not under `append_only`. Fail-open."""
    from server.memory.app.conflicts import namespace_conflict_policy
    from server.memory.app.key_supersession import keys_held_elsewhere
    from server.memory.domain.conflict_resolution import later_close_siblings
    from server.memory.store.conflict_resolver import OWED_MARK
    from server.settings import settings

    if not settings.memory_conflict_resolver_enabled or not state.embedding_rows:
        return
    try:
        policy = await namespace_conflict_policy(input.org, input.namespace, ch=ch)
        if policy == "append_only":
            return
        rows = state.embedding_rows
        for i in later_close_siblings(
            [list(r.get("embedding") or []) for r in rows],
            [str(r.get("memory_type") or "") for r in rows],
            float(settings.memory_conflict_resolver_min_similarity),
        ):
            rows[i]["conflict_checked_at"] = OWED_MARK
        if policy == "newest_wins":
            return
        held = await keys_held_elsewhere(
            org=input.org, namespace=input.namespace, memory_rows=state.memory_rows, ch=ch
        )
    except Exception as e:  # noqa: BLE001 — the rows land unmarked; the write stands
        log.warning("bulk_set: same-key mark skipped (non-fatal): %s", e)
        return
    for row in state.embedding_rows:
        if str(row.get("memory_key") or "") in held:
            row["conflict_checked_at"] = OWED_MARK


async def _bulk_intra_batch_pairs(
    ch: Any, input: MemoryBulkSetInput, state: _BulkWriteState
) -> list[tuple[str, str]]:
    """Phase 9: under `newest_wins`, stamp the earlier row of each templated
    pair inside the batch as superseded (before the insert); returns the
    (older, newer) id pairs for the triple plane's follow step."""
    # Two statements of one attribute inside the same write ("The capital of Soviet Union is
    # Moscow" and "… is Russellville" in one chunk) would both land active: the verdicts
    # compare a row with the store, not with its batch siblings, and the value-update linker
    # skips same-day pairs. Under `newest_wins` the later row in the batch supersedes the
    # earlier before the insert; the pair also feeds the triple plane's follow step below.
    _intra_pairs: list[tuple[str, str]] = []
    if state.memory_rows:
        try:
            from server.memory.app.conflicts import namespace_conflict_policy

            if await namespace_conflict_policy(input.org, input.namespace, ch=ch) == "newest_wins":
                from server.memory.domain.conflicts import (
                    intra_batch_supersessions,
                    stamp_intra_batch_supersessions,
                )

                # Only versions FINAL keeps pair: one the batch replaced says nothing now.
                rows = state.memory_rows
                kept = {id(r) for r in kept_versions(rows).values()}
                _ib = [
                    (i, j)
                    for i, j in intra_batch_supersessions(
                        [str(r.get("value") or "") for r in rows],
                        [str(r.get("memory_type") or "") for r in rows],
                    )
                    if id(rows[i]) in kept and id(rows[j]) in kept
                ]
                _intra_pairs = stamp_intra_batch_supersessions(
                    state.memory_rows, state.embedding_rows, _ib, naive_utc_now()
                )
                if _intra_pairs:
                    log.info(
                        "bulk_set: %d templated pair(s) inside the batch; the earlier row of each "
                        "is superseded before the insert",
                        len(_intra_pairs),
                    )
        except Exception as e:  # noqa: BLE001 — the rows still land; the pair goes to review
            log.warning("bulk_set: intra-batch supersession skipped (non-fatal): %s", e)
    return _intra_pairs


def _settle_results(state: _BulkWriteState, plan: BulkVerdictPlan) -> None:
    """Bring each result and the plan's lists in line with the marks that landed: a
    key left without a conflict mark or refine answers `clear`."""
    key_of = state.key_of
    still_marked = {key_of.get(n, "") for n, _p in plan.conflict_marks}
    still_refined = {key_of.get(r[0], "") for r in plan.refines}
    unmarked = {k for k in plan.conflicted if k not in still_marked}
    unrefined = {k for k in plan.refined if k not in still_refined}
    plan.conflicted = [k for k in plan.conflicted if k not in unmarked]
    plan.refined = [k for k in plan.refined if k not in unrefined]
    for res in state.results:
        cleared = (res.conflict_verdict == "conflict" and res.memory_key in unmarked) or (
            res.conflict_verdict == "refines" and res.memory_key in unrefined
        )
        if cleared:
            res.conflict_verdict = "clear"
            res.next = set_next(res.conflict_verdict, res.peers)


async def _bulk_insert(ch: Any, state: _BulkWriteState) -> None:
    """Phase 10: the two core writes — the memory rows and their embedding
    rows — gathered, each a committed insert. Once the memory rows are in, the
    batch is announced even when the embedding insert or the tail after it fails:
    a retry of the same values writes nothing and would announce nothing."""
    import server.memory.store as memory_repo

    if state.memory_rows:
        first = state.memory_rows[0]
        lifted = await lift_above_deletes(
            first["org"], first["namespace"], state.memory_rows, state.embedding_rows, ch=ch
        )
        for res in state.results:
            res.version = lifted.get((res.memory_key, res.version), res.version)

    # The two core writes are independent rows of two tables and each is a ~260 ms committed
    # insert on ClickHouse Cloud, so they are gathered, as on the point path (`memory.set`).
    async def _insert_memory_rows() -> None:
        # Full version rows (revision or first version), so the typed insert checks them.
        await memory_repo.insert_memory_versions(state.memory_rows, ch=ch)

    retire = _vectors_to_retire(state)
    # A key whose winning row is unembedded keeps no vector, not even one of this batch.
    state.embedding_rows = [
        r for r in state.embedding_rows if str(r.get("memory_key")) not in retire
    ]
    memory_done, embedding_done = await asyncio.gather(
        _insert_memory_rows(),
        memory_repo.insert_embedding_rows(state.embedding_rows, ch=ch),
        return_exceptions=True,
    )
    if isinstance(memory_done, BaseException):
        raise memory_done
    try:
        if isinstance(embedding_done, BaseException):
            raise embedding_done
        if state.memory_rows:
            # A memory.delete_version that landed after the fence read: the rows go above it.
            lifted = await lift_after_insert(
                first["org"], first["namespace"], state.memory_rows, state.embedding_rows, ch=ch
            )
            if lifted:
                for res in state.results:
                    res.version = lifted.get((res.memory_key, res.version), res.version)
                retire = _vectors_to_retire(state)
        # The entries' versions were drawn before the batch embedded: a slow batch indexes
        # its own BM25 stats.
        after_write.index_late_rows(state.embedding_rows, ch=ch)
        await memory_repo.retire_vectors_for_keys(list(retire.values()), ch=ch)
        fences = _high_winners(state)
        if fences:
            await fence_high(fences, ch=ch)
    finally:
        await _bulk_announce(state)  # the rows are in, whatever failed after them


async def _drop_undeclared_of_rows_made_high(
    input: MemoryBulkSetInput, state: _BulkWriteState
) -> None:
    """Each key the batch wrote 'high' over a row it already held: what its text gave the
    graph goes, after the response; the edges of the entities its row names stay."""
    versions: dict[str, int] = {}
    for key, row in kept_versions(state.memory_rows).items():
        if str(row.get("sensitivity") or "") != "high" or key not in state.existing_rows:
            continue
        declared = declared_entities(row.get("entities"))
        if owes_undeclared_drop(state.existing_rows[key], declared):
            versions[str(row["memory_id"])] = int(row["version"])
    # One intent and one drop for the whole batch: a row each would cost a committed
    # insert per row before the response.
    await drop_undeclared_edges_beside(input, versions, op="memory.bulk_set")


def _high_winners(state: _BulkWriteState) -> list[tuple[Any, int]]:
    """(identity key, version) of each key whose winning row is 'high': the fence an
    older write's late vector lands under."""
    import server.memory.store as memory_repo

    return [
        (memory_repo.embedding_key(r), int(r["version"]))
        for r in kept_versions(state.memory_rows).values()
        if str(r.get("sensitivity") or "") == "high"
    ]


def _vectors_to_retire(state: _BulkWriteState) -> dict[str, tuple[Any, int]]:
    """memory_key -> (identity key, version) of each key whose winning row (the
    highest version in the batch) lands unembedded and may have a live vector: the
    batch embedded an earlier entry of it, the replaced version was embedded, or the
    lookup failed and nobody knows. A row the replay left without the vector its level
    takes lands unembedded too."""
    winners = kept_versions(state.memory_rows)
    batch_embedded = {str(r.get("memory_key")) for r in state.embedding_rows}
    # an earlier entry's vector is not the winner's: match the row by its version
    batch_vectors = {(str(r.get("memory_key")), int(r["version"])) for r in state.embedding_rows}
    out: dict[str, tuple[Any, int]] = {}
    for key, r in winners.items():
        vectorless = key in state.vectorless_keys and (key, int(r["version"])) not in batch_vectors
        if not vectorless and is_embedded(
            str(r.get("memory_type")), str(r.get("sensitivity") or "")
        ):
            continue
        prev = state.existing_rows.get(key)
        prev_embedded = prev is not None and is_embedded(
            prev.memory_type, getattr(prev, "sensitivity", "")
        )
        if key in batch_embedded or not state.existing_lookup_ok or prev_embedded:
            ident = (r["org"], r["namespace"], r["agent"], r["user_id"], r["session_id"], key)
            out[key] = (ident, int(r["version"]))
    return out


async def _bulk_post_insert_links(
    ch: Any,
    input: MemoryBulkSetInput,
    state: _BulkWriteState,
    same_key_pairs: list[Follow],
) -> list[Any]:
    """Phase 11, after the insert so the new ids exist: same-key supersession (its pairs
    into `same_key_pairs`), then value-update linking; returns the value-update links
    the triple plane follows."""
    # Same-key supersession: under `newest_wins` the rows just written retire every older
    # live row sharing their key, from any session. After the insert so the new ids are
    # visible; fail-open.
    if state.memory_rows:
        from server.memory.app.key_supersession import supersede_same_key_rows

        await supersede_same_key_rows(
            org=input.org,
            namespace=input.namespace,
            new_rows=state.memory_rows,
            ch=ch,
            follows=same_key_pairs,
            agent=input.agent,
        )

    # Value-update supersession (memory.app.value_updates). Runs after the
    # insert so the new row's id exists for the cross-link; fail-open; off by
    # default. Only rows this batch actually wrote (memory_rows) and left live are checked:
    # a row that landed superseded must not retire anything behind itself.
    _n_value_links = 0
    _value_links: list[Any] = []
    _live_rows = [
        r
        for r in kept_versions(state.memory_rows).values()
        if str(r.get("status") or "") != "superseded"
    ]
    if _live_rows and value_updates_enabled(input.detect_updates):
        try:
            from server.memory.app.value_updates import link_value_updates
            from server.settings import settings

            _report = await link_value_updates(
                org=input.org,
                namespace=input.namespace,
                rows=_live_rows,
                agent=input.agent,
                embeddings_by_key={
                    str(e.get("memory_key") or ""): list(e.get("embedding") or [])
                    for e in state.embedding_rows
                },
                min_cosine=float(settings.memory_extract_value_updates_min_cosine),
                ch=ch,
            )
            _n_value_links = len(_report.links)
            _value_links = list(_report.follow_links)
        except Exception as e:  # noqa: BLE001 — never block an ingest
            log.warning("bulk_set: value-update linking failed (non-fatal): %s", e)
    last_value_updates_linked.set(_n_value_links)
    return _value_links


async def _record_intra_batch_retirements(
    input: MemoryBulkSetInput, state: _BulkWriteState, pairs: list[tuple[str, str]]
) -> None:
    """After the insert: one `memory.auto_supersede` row for the earlier rows of the
    batch's templated pairs, which `newest_wins` landed superseded."""
    if not pairs:
        return
    from server.memory.app.auto_resolution import (
        NEWEST_WINS_CONTRADICTION,
        record_or_owe,
    )

    # A key sent twice is two versions of one row, under one id: only a kept
    # version that landed superseded retired its row.
    kept = kept_versions(state.memory_rows)
    # The kept version names the row it retired behind: one id can be the older row of
    # two pairs.
    older = {o for o, _n in pairs}
    retired = [
        {**r, "superseded_by_key": state.key_of.get(str(r.get("superseded_by") or ""))}
        for r in kept.values()
        if str(r.get("memory_id") or "") in older and r.get("status") == "superseded"
    ]
    await record_or_owe(
        org=input.org,
        namespace=input.namespace,
        retired=retired,
        reason=NEWEST_WINS_CONTRADICTION,
        agent=input.agent,
    )


async def _bulk_settle_verdicts(
    ch: Any, input: MemoryBulkSetInput, state: _BulkWriteState, plan: BulkVerdictPlan
) -> None:
    """Projection leg: settle the verdicts after the insert — peer conflict
    marks, refines, the evidence events of merges, identity duplicates and
    restatements, then the same-key winner repair. Each step fail-open."""
    if plan.conflict_marks:
        from server.memory.app.conflicts import mark_peers_conflicted

        clear: set[str] = set()
        for new_id, peer_ids in plan.conflict_marks:
            try:
                landed = await mark_peers_conflicted(
                    org=input.org, new_memory_id=new_id, peer_ids=peer_ids
                )
            except Exception as e:  # noqa: BLE001 — the rows are in; the link is best-effort
                log.warning("bulk_set: mark_peers_conflicted failed for %s: %s", new_id, e)
            else:
                if landed != "conflict":
                    clear.add(new_id)
        if clear:
            # Nothing was flagged (every peer retired, or the row moved on since the
            # verdict): the row stays active, unflagged, as memory.set leaves it.
            plan.conflict_marks = [(n, p) for n, p in plan.conflict_marks if n not in clear]
            _settle_results(state, plan)
    if plan.refines:
        from server.memory.app.auto_resolution import record_or_owe
        from server.memory.app.conflicts import apply_refines

        # One record per reason for the whole write, not one per verdict.
        refined_out: dict[str, list[dict[str, Any]]] = {}
        for new_id, new_key, refined in plan.refines:
            try:
                await apply_refines(
                    org=input.org,
                    namespace=input.namespace,
                    new_memory_id=new_id,
                    new_memory_key=new_key,
                    refined_peers=refined,
                    ch=ch,
                    record_into=refined_out,
                )
            except Exception as e:  # noqa: BLE001 — the write stands
                log.warning("bulk_set: apply_refines failed for %s: %s", new_id, e)
        for reason, retired in refined_out.items():
            await record_or_owe(
                org=input.org,
                namespace=input.namespace,
                retired=retired,
                reason=reason,
                agent=input.agent,
            )
    # Merges and identity duplicates reinforce the row that already holds the fact. A
    # restatement is the same fact said again under the same key: the usage fold turns the
    # evidence event into memory_embeddings.reinforcement_count, the search-side boost; the
    # row itself is not rewritten or re-embedded.
    if state.reinforce:
        await _record_evidence(input, plan.reinforce_ids, "auto_reinforce bump")
        await _record_evidence(input, state.identity_dup_ids, "identity-dedup reinforcement")
        await _record_evidence(input, state.restated_ids, "restatement reinforcement")


async def _record_evidence(input: MemoryBulkSetInput, memory_ids: list[str], what: str) -> None:
    """One `evidence` usage event per id, each fail-open."""
    from server.memory.store.usage import record_usage_event

    for memory_id in memory_ids:
        try:
            await record_usage_event(
                memory_id=memory_id,
                org=input.org,
                namespace=input.namespace,
                event_type="evidence",
                event_count=1,
                agent=input.agent,
            )
        except Exception as e:  # noqa: BLE001 — a usage bump; the write stands
            log.warning("bulk_set: %s failed (non-fatal): %s", what, e)


async def _bulk_settle_then_audit(
    ch: Any, input: MemoryBulkSetInput, state: _BulkWriteState, plan: BulkVerdictPlan
) -> None:
    """Settle the verdicts, then write the audit row: a conflict whose mark did not land
    is clear in the response, and the audit counts what the response says."""
    try:
        await _bulk_settle_verdicts(ch, input, state, plan)
    finally:
        await _bulk_audit(input, state, plan)


async def _bulk_audit(
    input: MemoryBulkSetInput, state: _BulkWriteState, plan: BulkVerdictPlan
) -> None:
    """Projection leg: the write's one hash-only audit row."""
    # Every write lands in the hash-chained audit log as hashes, not text: what was written,
    # by whom, with which verdict.
    try:
        await _audit_bulk_write(input, state.memory_rows, plan)
    except Exception as e:  # noqa: BLE001 — the ledger row is best-effort
        log.warning("bulk_set: audit row failed (non-fatal): %s", e)


def _bulk_record_write_metrics(input: MemoryBulkSetInput, state: _BulkWriteState) -> None:
    """Phase 12: one `loom.memory.writes` tick per entry outcome."""
    # Count the write outcomes for this path too: this bulk/extract path inserts into
    # loom.memory directly and skips blacklisted/pinned entries with a log line rather than
    # raising, so without these ticks a namespace ingesting steadily through
    # memory.set_from_messages or the observe worker would read as no traffic on the panel
    # that answers whether anything new is going in.
    try:
        from server.observability.metrics import record_memory_write

        ns = input.namespace
        # `skipped_unchanged` is a byte-identical restatement, the same shape `merged` means
        # on the point-write path, so use the same outcome rather than inventing a second name.
        for outcome, count in (
            ("created", state.n_created),
            ("updated", state.n_updated),
            ("unknown", state.n_unknown),
            ("blacklisted", len(state.skipped_blacklisted)),
            ("rejected", len(state.skipped_pinned) + len(state.skipped_locked)),
            # NOT the point path's `merged` (a canonical-form conflict merge).
            # This is a byte-identical restatement, so it gets its own label
            # rather than being indistinguishable in the same series.
            ("unchanged", state.skipped_unchanged),
            # The same bytes live under this identity already.
            ("deduplicated", len(state.identity_duplicates)),
        ):
            for _ in range(count):
                record_memory_write(namespace=ns, outcome=outcome)
    except Exception:  # noqa: BLE001 — telemetry must not fail an ingest
        pass


def _projected(
    input: MemoryBulkSetInput, state: _BulkWriteState, *, declared_high: bool = False
) -> tuple[MemoryBulkSetInput, list[Any]]:
    """The entries and results the graph projections see: only entries that wrote a
    live row that may hold graph facts. An entry that wrote nothing (refused, pinned,
    locked, a byte-identical restatement, an identity duplicate, a merge, a pinned
    reject), or whose row is 'high' projects nothing, and so records nothing for a
    replay; a re-ingest of a fact already stored no longer re-counts its edges.
    `declared_high` (the edges leg): a 'high' entry stays with only the entities its
    writer declared and no facet nodes, as memory.set projects a high row."""
    # A row is its id and version: two entries of one key share the id.
    high = {
        (str(r["memory_id"]), int(r["version"]))
        for r in state.memory_rows
        if r.get("sensitivity") == "high"
    }
    kept = []
    for entry, res in zip(input.entries, state.results, strict=True):
        if not res.version:
            continue
        if (res.memory_id, res.version) not in high:
            kept.append((entry, res))
        elif declared_high:
            declared = {
                "entities": declared_entities(entry.entities),
                "topics": [],
                "files": [],
                "concepts": [],
            }
            kept.append((entry.model_copy(update=declared), res))
    return (
        input.model_copy(update={"entries": [e for e, _r in kept]}),
        [r for _e, r in kept],
    )


async def _bulk_run_projections(
    ch: Any,
    input: MemoryBulkSetInput,
    state: _BulkWriteState,
    plan: BulkVerdictPlan,
    embedder: Any,
    entity_resolution: dict[str, str],
    entity_embeddings: dict[str, list[float]],
) -> None:
    """Phase 13: the post-insert legs — settle verdicts, the audit row and the
    four graph projections — gathered."""
    # An entry whose key landed superseded writes no current graph facts; the input and
    # its results are filtered together (the writers zip them), and so is the replay.
    p_input, p_results = _projected(input, state)
    # The edges leg also sees the high rows, with their declared entities only.
    e_input, e_results = _projected(input, state, declared_high=True)
    await _drop_undeclared_of_rows_made_high(input, state)

    # Graph/entity projections (entity_vectors / edges / triples / derived).
    # Each leg is fail-open + records a replayable projection_failure.
    def _projection(name: str, write: Any) -> Any:
        async def _run() -> None:
            try:
                await write()
            except Exception as e:  # noqa: BLE001 — a projection; the write stands
                log.warning("%s write failed (non-fatal): %s", name, e)
                await record_input_projection_failure(
                    p_input,
                    projection=failed_projection(name, e),
                    error=e,
                    memory_keys=[r.memory_key for r in p_results],
                    payload=bulk_projection_payload(p_input, p_results, error=e),
                    source_operation="memory.bulk_set",
                    source_resource_id=bulk_source_resource_id(input),
                )

        return _run

    # The settlement of verdicts (peer conflict marks, reinforcement events) then the audit
    # row, and the four graph projections, all read this batch's result and write different
    # tables; none reads another's output, so they are awaited together, each
    # with its own fail-open handler. Still awaited before the response: rrf search routes
    # across the entity/graph/temporal planes and may query immediately after ingest, so
    # backgrounding would starve those planes.
    _legs = [
        lambda: _bulk_settle_then_audit(ch, input, state, plan),
        _projection(
            "entity_vectors",
            lambda: write_entity_vectors(
                p_input,
                p_results,
                embedder,
                ch=ch,
                entity_map=entity_resolution,
                entity_embeddings=entity_embeddings,
            ),
        ),
        # Recorded without the high entries, as memory.set records nothing for a high
        # row's edges: a failure payload never holds a high value, so a failed declared
        # edge of a high row is not replayed.
        _projection(
            "entity_edges",
            lambda: write_entity_edges(e_input, e_results, ch=ch, entity_map=entity_resolution),
        ),
        _projection(
            "entity_triples",
            lambda: write_entity_triples(p_input, p_results, ch=ch, entity_map=entity_resolution),
        ),
        _projection(
            "derived_facts",
            lambda: write_derived_facts(p_input, p_results, ch=ch, entity_map=entity_resolution),
        ),
    ]
    await asyncio.gather(*(leg() for leg in _legs))
    # A row revised in place keeps its id: what it held before is judged against its
    # new value, once its new links are in ('high' and judged-superseded included: it
    # now holds none). A brand-new row held nothing, unless this batch wrote it twice.
    written = Counter(r.memory_id for r in state.results if r.version)
    existed = set(state.existing_ids.values())
    revised = [m for m, n in written.items() if m in existed or n > 1]
    if revised:
        # The versions of high rows: their edge closes are recorded without content.
        high = {
            (str(r["memory_id"]), str(r["content_sha256"]))
            for r in state.memory_rows
            if r.get("sensitivity") == "high"
        }
        await _close_overtaken_edges(
            e_input, e_results, set(revised), high=high, entity_map=entity_resolution, ch=ch
        )
        await reconcile_row_facts(p_input, revised, reopen=False, op="memory.bulk_set", ch=ch)


async def _close_overtaken_edges(
    input: MemoryBulkSetInput,
    results: list[Any],
    revised: set[str],
    *,
    high: set[tuple[str, str]],
    entity_map: dict[str, str],
    ch: Any,
) -> None:
    """The revised rows' edges end with their value if another write revised it
    again while they were written (`end_overtaken_edges`). An entry with fewer than
    two graph nodes wrote no edge. One read for all of them: a row holds its edges
    while it holds the value, and a 'high' one only while it names every node paired.
    A failed read is recorded for the projection repair, which ends them; `high` (the
    (memory_id, content_sha256) versions of high rows) are recorded without content."""
    paired: dict[tuple[str, str, str], list[str]] = {}
    for entry, res in zip(input.entries, results, strict=True):
        nodes = edge_nodes(entry, entity_map)
        if res.memory_id in revised and len(nodes) >= 2:
            paired[(res.memory_id, res.memory_key, value_sha256(entry))] = nodes
    if not paired:
        return
    try:
        current = await edge_holders(
            input.org, input.namespace, [(mid, sha) for mid, _, sha in paired], ch=ch
        )
    except Exception as e:  # noqa: BLE001 — recorded; the projection repair ends them
        closes = [
            EdgeClose(memory_id, memory_key, sha, nodes, high=(memory_id, sha) in high)
            for (memory_id, memory_key, sha), nodes in paired.items()
        ]
        await record_edges_closes(input, closes, e, op="memory.bulk_set", ch=ch)
        return
    overtaken = []
    for version, nodes in paired.items():
        memory_id, _, sha = version
        found = current.get((memory_id, sha))
        if found is None:
            overtaken.append(version)
            continue
        sensitivity, entities = found
        if sensitivity == "high" and not set(nodes) <= entities:
            overtaken.append(version)
    if overtaken:
        await end_overtaken_edges(input, sorted(overtaken), op="memory.bulk_set", ch=ch, high=high)


async def _bulk_follow_row_supersessions(
    ch: Any,
    input: MemoryBulkSetInput,
    plan: BulkVerdictPlan,
    value_links: list[Any],
    intra_pairs: list[tuple[str, str]],
    clock: StageClock,
) -> None:
    """Phase 14: the triple plane follows the rows this write retired."""
    # The triple plane follows the rows this write retired. The predecessor's open triples
    # close and the successor triple is written under the predecessor's relation name, after
    # the projections, so a successor the extractor did emit is seen and not repeated.
    # Up to `memory_bulk_follow_max_pairs` pairs are followed in the request; the rest,
    # those whose chain was too long or circular to read, or all of them if the follow
    # fails, are recorded for the projection repair to follow later.
    _pairs: list[tuple[str, str]] = _supersede_pairs(plan)
    _pairs += [(str(ln.older_id), str(ln.newer_id)) for ln in value_links]
    _pairs += intra_pairs
    if _pairs:
        from server.memory.app.triple_follow import follow_row_supersessions
        from server.settings import settings

        cap = settings.memory_bulk_follow_max_pairs
        first, later = _pairs[:cap], _pairs[cap:]
        restated: dict[str, Any] = {}
        try:
            later += (await follow_row_supersessions(input, first, ch=ch)).unfollowed
        except Exception as e:  # noqa: BLE001 — the rows are in; the repair follows them later
            log.warning("bulk_set: triples follow rows failed (recorded for repair): %s", e)
            later = _pairs
            # A fallback's markers whose repair failed: the closes it made.
            restated = restated_repair_payload(e)
        if later:
            await record_input_projection_failure(
                input,
                projection=FOLLOW_ROWS,
                error=f"supersession pairs left for the repair to follow: {len(later)}",
                payload={"pairs": [list(p) for p in later], **restated},
                source_operation="memory.bulk_set",
                source_resource_id=bulk_source_resource_id(input),
            )
    clock.mark("triples_follow_rows")


async def handle_memory_bulk_set(
    input: MemoryBulkSetInput,
    *,
    dry_run: bool = False,
    conflict_check_cap: int | None = None,
    reinforce: bool = True,
) -> MemoryBulkSetOutput:
    """One CH insert for all rows; one more for embeddings. Versions are
    each unique (nanosecond) to preserve LWW semantics.

    - `dry_run`: return after the verdicts; no row is inserted.
    - `conflict_check_cap`: overrides `bulk_conflict_check_max_entries`.
    - `reinforce=False`: no `evidence` event for a restatement, merge or identity duplicate."""
    ch = get_client()
    embedder = get_embedder()
    # The write names its phases on the response.
    from server.memory.app.search.clock import StageClock

    _bulk_work: dict[str, Any] = {}
    _bulk_clock = StageClock(_bulk_work)
    state = _BulkWriteState(conflict_check_cap=conflict_check_cap, reinforce=reinforce)
    await _bulk_existing_lookup(ch, input, state)

    identity_dupes = await _bulk_identity_dupes(ch, input)

    _bulk_clock.mark("existing_lookup")
    entity_resolution = await _bulk_resolve_entities(ch, input)

    _bulk_clock.mark("entity_resolution")
    await _bulk_build_rows(input, state, identity_dupes, entity_resolution)
    if state.pending:
        # Before the calls it pays for: a vector the coded table cannot take fails the batch.
        await codes_state.refuse_width("memory_embeddings", embedder.dim)

    emb_idx_of, value_vectors, entity_embeddings = await _bulk_embed_rows(
        input, state, embedder, entity_resolution, _bulk_clock
    )

    plan = await _bulk_verdicts(input, state, emb_idx_of, value_vectors, _bulk_clock)
    if dry_run:
        _bulk_clock.finish()
        return _bulk_output(state, plan, _bulk_work)
    # After the verdicts: settling the batch moves entries to unchanged or pinned.
    _bulk_log_skips(state)

    await _bulk_mark_same_key(ch, input, state)
    _intra_pairs = await _bulk_intra_batch_pairs(ch, input, state)

    await _bulk_insert(ch, state)
    _bulk_clock.mark("insert")
    await _record_intra_batch_retirements(input, state, _intra_pairs)

    # A bulk write's retirement intents are left to the repair, which resolves them.
    _same_key_follows: list[Follow] = []
    _value_links = await _bulk_post_insert_links(ch, input, state, _same_key_follows)
    _same_key_pairs = [(f.loser, f.winner) for f in _same_key_follows]

    _bulk_clock.mark("value_updates")

    _bulk_record_write_metrics(input, state)

    await _bulk_run_projections(
        ch, input, state, plan, embedder, entity_resolution, entity_embeddings
    )
    _bulk_clock.mark("projections")
    await _bulk_follow_row_supersessions(
        ch,
        input,
        plan,
        _value_links,
        _intra_pairs + _same_key_pairs,
        _bulk_clock,
    )
    _bulk_clock.finish()
    return _bulk_output(state, plan, _bulk_work)


def _bulk_output(
    state: _BulkWriteState, plan: BulkVerdictPlan, work: dict[str, Any]
) -> MemoryBulkSetOutput:
    """The response: each entry's result, the verdict lists and the write's phase times."""
    return MemoryBulkSetOutput(
        written=state.results,
        retyped=state.retyped,
        skipped_pinned=state.skipped_pinned,
        skipped_locked=state.skipped_locked,
        # An identity duplicate is a restatement of an existing memory (the point path's
        # `merged` verdict) reached by the hash instead of the cosine check; it is reported
        # under both names.
        merged=[*plan.merged, *[k for k in state.identity_duplicates if k not in plan.merged]],
        rejected_pinned=plan.rejected_pinned,
        duplicates=plan.duplicates,
        conflicted=plan.conflicted,
        unchecked=plan.unchecked,
        identity_duplicates=state.identity_duplicates,
        skipped_blacklisted=state.skipped_blacklisted,
        stage_ms=dict(work.get("stage_ms", {})),
    )


async def _bulk_announce(state: _BulkWriteState) -> None:
    """The written rows, to the namespace's webhooks: a key that was live, or
    that an earlier entry of this batch wrote, is revised; any other is
    written. An unchanged restatement writes no row and sends nothing. When
    the live-key lookup failed (`existing_lookup_ok` is False) every key looks
    new, so a revision is announced as written."""
    seen = set(state.existing_ids)
    changes: list[tuple[EventType, dict[str, Any]]] = []
    for row in state.memory_rows:
        key = str(row["memory_key"])
        changes.append(("memory.revised" if key in seen else "memory.written", row))
        seen.add(key)
    if await webhooks.announce(changes):
        note_budget_shape("webhook")


def _supersede_pairs(plan: BulkVerdictPlan) -> list[tuple[str, str]]:
    """(retired peer, new row) pairs of the plan's `refines` verdicts, for
    the triple plane to follow."""
    return [
        (str(getattr(p, "memory_id", "") or ""), str(new_id))
        for new_id, _new_key, refined in plan.refines
        for p in refined
        if getattr(p, "memory_id", "")
    ]


async def _bulk_conflict_verdicts(
    input: Any,
    memory_rows: list[dict[str, Any]],
    entry_metas: list[tuple[Any, ...]],
    pending: list[tuple[Any, ...]],
    value_vectors: list[list[float]],
    *,
    cap: int | None = None,
) -> dict[int, Any]:
    """check_on_commit for every searchable entry, concurrency-bounded. Keys are
    indexes into memory_rows; values are ConflictResults. A bulk past the cap (`cap`,
    else bulk_conflict_check_max_entries) checks the first N and writes the rest
    unchecked (the caller counts them); a check that errors counts as clear — the write
    must not fail because the check could not run — except one the model-call switch
    holds (`customer.HOLDS`), which the write answers as its own embeds would."""
    import asyncio

    from server.memory.app.conflicts import check_on_commit
    from server.settings import settings as _settings

    if not pending or not value_vectors:
        return {}
    if cap is None:
        cap = int(_settings.bulk_conflict_check_max_entries)
    cap = max(cap, 0)
    sem = asyncio.Semaphore(int(_settings.bulk_conflict_check_concurrency))
    out: dict[int, Any] = {}

    async def _one(
        idx: int,
        vec: list[float],
        candidate_rows: list[dict[str, Any]] | None = None,
        resolved_threshold: float | None = None,
    ) -> None:
        entry, serialized, _t, _e, _x = entry_metas[idx]
        async with sem:
            try:
                out[idx] = await check_on_commit(
                    org=input.org,
                    namespace=input.namespace,
                    agent=input.agent,
                    memory_type=entry.memory_type,
                    memory_key=entry.memory_key,
                    new_value=serialized,
                    new_embedding=vec,
                    new_run_id=str(getattr(input, "run_id", "") or "-"),
                    new_valid_at=(
                        getattr(entry, "valid_at", None) or getattr(input, "valid_at", None)
                    ),
                    candidate_rows=candidate_rows,
                    resolved_threshold=resolved_threshold,
                )
            except customer.HOLDS:
                raise  # held, as the batch's own embeds would be: the write answers it
            except Exception as e:  # noqa: BLE001 — the fact writes unchecked
                log.warning("bulk_set: conflict check failed for %r: %s", entry.memory_key, e)

    pairs = list(zip(pending, value_vectors, strict=False))[:cap]
    # The peer statement runs once per batch of facts, not once per fact: the namespace's
    # vectors are read once per batch. Thresholds resolve once per memory_type. A batch that
    # failed leaves its facts without an entry and they fall back to the per-fact statement.
    batch = int(_settings.memory_write_candidate_batch)
    prefetched: dict[int, list[dict[str, Any]]] = {}
    floors: dict[str, tuple[float, float]] = {}
    if batch > 0 and len(pairs) > 1:
        from server.memory.app import conflicts as _conflicts

        items: list[tuple[str, str, list[float], float]] = []
        for (idx, _t, _x, _k), vec in pairs:
            mt = str(entry_metas[idx][0].memory_type)
            if vec and mt not in floors:
                try:
                    floors[mt] = await _conflicts.resolve_conflict_floor(
                        input.org, input.namespace, mt
                    )
                except Exception as e:  # noqa: BLE001 — per-fact path resolves it
                    log.warning("bulk_set: threshold lookup failed for %r: %s", mt, e)
            md = floors.get(mt, (0.0, 0.0))[1]
            items.append((mt, str(entry_metas[idx][0].memory_key), vec if mt in floors else [], md))
        try:
            prefetched = await _conflicts.candidate_rows_many(
                org=input.org, namespace=input.namespace, items=items, batch=batch
            )
        except Exception as e:  # noqa: BLE001 — fall back to per-fact statements
            log.warning("bulk_set: batched candidate lookup failed: %s", e)
            prefetched = {}

    jobs = []
    for j, ((idx, _t, _x, _k), vec) in enumerate(pairs):
        mt = str(entry_metas[idx][0].memory_type)
        rows = prefetched.get(j)
        thr = floors.get(mt, (None, None))[0] if rows is not None else None
        jobs.append(_one(idx, vec, candidate_rows=rows, resolved_threshold=thr))
    if jobs:
        await asyncio.gather(*jobs)
    return out


async def _audit_bulk_write(
    input: Any, memory_rows: list[dict[str, Any]], plan: BulkVerdictPlan
) -> None:
    """One hash-chained audit row per bulk write: keys, ids and content hashes
    (never text — set_from_messages carries raw conversation), the writer and
    the verdict counts. Best-effort; never fails the write."""
    from server.observability import audit as _audit

    if not memory_rows and not plan.merged and not plan.rejected_pinned:
        return
    try:
        await _audit.emit(
            event_type="memory.bulk_set",
            org=input.org,
            namespace=input.namespace,
            actor=writer_actor() or "-",
            subject={
                "count": len(memory_rows),
                "memory_keys": [str(r["memory_key"]) for r in memory_rows][:200],
                "memory_ids": [str(r["memory_id"]) for r in memory_rows][:200],
                "content_sha256": [str(r.get("content_sha256") or "") for r in memory_rows][:200],
                "source_trace": (
                    str(memory_rows[0].get("source_trace") or "") if memory_rows else ""
                ),
                "merged": plan.merged[:50],
                "rejected_pinned": plan.rejected_pinned[:50],
                "duplicates": len(plan.duplicates),
                "conflicted": len(plan.conflicted),
                "unchecked": plan.unchecked,
            },
        )
    except Exception as e:  # noqa: BLE001 — the write stands; logged
        log.warning("bulk_set: audit emit failed (non-fatal): %s", e)
