"""Memory projections: the derived rows a memory write fans out to.

A memory write lands its memory row first; its projections are the rows
derived from it afterwards — verbatim conversation chunks
(loom.memory_chunks, written by server/memory/app/chunk_projection.py),
entity edges, entity triples (with their interval repairs and supersession
closes) and entity vectors. They are best-effort:
a projection that fails is logged to loom.memory_projection_failures and retried
from server.memory.app.projection_failures, which calls these same writers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
from server.memory.app import conflicts as _conflicts
from server.memory.app.projection_errors import TriplesInsertFailed, TriplesRepairFailed
from server.memory.domain.graph_quality import (
    MAX_TRIPLES_PER_ROW,
    observation_key,
    order_undated_before_later_starts,
    restates_current,
)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from server.memory.domain.graph_quality import IntervalRepair


def resolved_entity_list(
    raw_entities: list[str],
    entity_map: dict[str, str] | None = None,
) -> list[str]:
    """Canonicalize + apply namespace-local entity resolution."""

    from server.memory.app.entity_resolution import apply_entity_resolution

    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_entities:
        resolved = apply_entity_resolution(str(raw), entity_map)
        if len(resolved) < 2 or resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


async def resolve_projection_entity_map(
    input: MemoryBulkSetInput,
    entity_texts: list[str],
    *,
    ch: Any,
) -> dict[str, str]:
    if not entity_texts:
        return {}
    try:
        from server.memory.app.entity_resolution import resolve_entities_for_write
        from server.observability.otel import traced

        with traced("entity.resolve", mentions=len(entity_texts)):
            return await resolve_entities_for_write(
                ch,
                org=input.org,
                namespace=input.namespace,
                entity_texts=entity_texts,
            )
    except Exception as e:  # noqa: BLE001 — rows write without entity ids
        log.warning("entity projection resolution failed (non-fatal): %s", e)
        return {}


def graph_insert_settings(table: str, *, wait: bool, dedup_token: str) -> dict[str, Any]:
    """Insert settings for the plain-MergeTree graph tables, where every row
    counts as an observation. A replay (`dedup_token`, the projection
    repair) inserts synchronously under a per-table deduplication token, so
    a replay that ran and then failed to resolve does not count its rows
    twice (ClickHouse drops a block whose token it has seen)."""
    if dedup_token:
        return {
            "async_insert": 0,
            "insert_deduplicate": 1,
            "insert_deduplication_token": f"{dedup_token}:{table}",
        }
    return {"async_insert": 1, "wait_for_async_insert": 1 if wait else 0}


async def write_entity_edges(
    input: MemoryBulkSetInput,
    results: list[_BulkSetResult],
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
    wait_visible: bool = False,
    dedup_token: str = "",
) -> None:
    """Emit pairwise entity edges + edge-to-memory rows per entry.

    Per memory with N entities, emits C(N, 2) canonical pairs
    (entity_a <= entity_b lexically), capped at _MAX_PAIRS_PER_MEMORY.
    Always-on side effect of writes; read-side (search_mode=graph) is opt-in.
    Fail-open. `wait_visible` acks only once the async-insert buffer has
    flushed, so the rows are readable when the call returns: for a caller off
    the response path, which pays nothing for the wait.
    """
    now = naive_utc_now()
    edges: list[dict[str, Any]] = []
    edge_memories: list[dict[str, Any]] = []
    _MAX_PAIRS_PER_MEMORY = 100
    if entity_map is None:
        entity_map = await resolve_projection_entity_map(
            input,
            [str(e) for entry in input.entries for e in (entry.entities or [])],
            ch=ch,
        )

    from server.memory.domain.graph_quality import canonical_entity_text, is_lexical_junk_entity

    for entry, result in zip(input.entries, results, strict=True):
        # A pronoun, interjection or contraction fragment the
        # extractor emitted as an entity or topic is never a graph node.
        ents = [
            e
            for e in resolved_entity_list(entry.entities or [], entity_map)
            if not is_lexical_junk_entity(e)
        ]
        # Facet nodes join the co-occurrence plane as ordinary entity nodes,
        # canonicalized identically so write and read land on one node:
        # code-native files + concepts (coding facts), and the extractor's
        # `topics` — the concept phrases a fact is ABOUT — for every fact,
        # so two memories about one idea connect even when
        # they name different entities.
        facet_nodes: list[str] = []
        for raw in [
            *(getattr(entry, "files", None) or []),
            *(getattr(entry, "concepts", None) or []),
            *(getattr(entry, "topics", None) or []),
        ]:
            node = (
                str(raw).lower()
                if str(raw).lower().startswith(("commit:", "error:"))
                else canonical_entity_text(str(raw))
            )
            if (
                len(node) >= 2
                and node not in ents
                and node not in facet_nodes
                and not is_lexical_junk_entity(node)
            ):
                facet_nodes.append(node)
        ents = sorted(set(ents) | set(facet_nodes))
        if len(ents) < 2:
            continue
        # Edge validity window from the memory's valid_at / valid_to (extractor
        # populates valid_to on closed-window state changes). NULL = still valid.
        entry_valid_from = parse_iso_dt(getattr(entry, "valid_at", "") or "")
        entry_valid_to = parse_iso_dt(getattr(entry, "valid_to", "") or "")
        # Enumerate pairs by OFFSET, not by first index. Both orders visit the
        # same C(N,2) pairs, but the cap truncates, and `ents` is sorted — so
        # i-major order would spend the whole budget on the alphabetically-first
        # entities, making a node's co-occurrence weight a function of its
        # spelling. Offset-major gives
        # every entity ~2 edges per completed offset, so a truncated
        # neighbourhood is merely smaller instead of skewed.
        n = len(ents)
        pair_count = 0
        for offset in range(1, n):
            if pair_count >= _MAX_PAIRS_PER_MEMORY:
                break
            for i in range(n - offset):
                if pair_count >= _MAX_PAIRS_PER_MEMORY:
                    break
                a, b = ents[i], ents[i + offset]  # sorted, so a <= b
                edges.append(
                    entity_edge_row(
                        input.org,
                        input.namespace,
                        a,
                        b,
                        last_seen=now,
                        valid_from=entry_valid_from,
                        valid_to=entry_valid_to,
                    )
                )
                edge_memories.append(
                    {
                        "org": input.org,
                        "namespace": input.namespace,
                        "entity_a": a,
                        "entity_b": b,
                        "memory_key": result.memory_key,
                        "seen_at": now,
                    }
                )
                pair_count += 1

    if not edges:
        return

    # Plain MergeTree: every row is one observation (co_occur = 1); readers
    # aggregate (sum(co_occur) GROUP BY pair) at query time.
    # Concurrent because these are two independent tables with no ordering
    # requirement between them, and against ClickHouse Cloud each insert is a
    # ~50ms round trip on the `memory.set` path.
    from server.observability.otel import traced as _traced

    with _traced("ch.insert.entity_edges", pairs=len(edges)):
        await asyncio.gather(
            ch.insert(
                "loom.entity_edges",
                edges,
                settings=graph_insert_settings(
                    "entity_edges", wait=wait_visible, dedup_token=dedup_token
                ),
            ),
            ch.insert(
                "loom.entity_edge_memories",
                edge_memories,
                settings=graph_insert_settings(
                    "entity_edge_memories", wait=wait_visible, dedup_token=dedup_token
                ),
            ),
        )


def entity_edge_row(
    org: str,
    namespace: str,
    entity_a: str,
    entity_b: str,
    *,
    last_seen: datetime,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    co_occur: int = 1,
    restore_id: str = "",
) -> dict[str, Any]:
    """One `loom.entity_edges` row for the canonical pair (entity_a <= entity_b). A
    fresh observation is never born superseded (`invalid_at` NULL):
    _repair_contradictory_edges stamps the OLDER pair. `co_occur` is the weight the
    readers sum; an Undo's observation carries 0 when the edge is open with weight,
    and the Undo's id (`restore_id`)."""
    return {
        "org": org,
        "namespace": namespace,
        "entity_a": entity_a,
        "entity_b": entity_b,
        "co_occur": co_occur,
        "last_seen": last_seen,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "invalid_at": None,
        "restore_id": restore_id,
    }


def entity_triple_row(
    org: str,
    namespace: str,
    subject: str,
    relation: str,
    obj: str,
    *,
    provenance: str,
    confidence: float,
    adapter_name: str,
    last_seen: datetime,
    seen_at: datetime,
    valid_from: Any = None,
    valid_to: Any = None,
    co_occur: int = 1,
    restore_id: str = "",
) -> dict[str, Any]:
    """One `loom.entity_triples` row as every triple writer inserts it. A fresh
    triple is never born superseded (`invalid_at` NULL): the contradiction
    repair stamps the older row. `seen_at` is the write's arrival to the
    microsecond, which orders values (`observation_key`); `last_seen` is its
    second. `co_occur` is the weight the readers sum; an Undo's observation
    carries 0 when the fact is open with weight, and the Undo's id (`restore_id`)."""
    return {
        "org": org,
        "namespace": namespace,
        "subject": subject,
        "relation": relation,
        "object": obj,
        "provenance": provenance,
        "co_occur": co_occur,
        "last_seen": last_seen,
        "seen_at": seen_at,
        "confidence": confidence,
        "adapter_name": adapter_name,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "invalid_at": None,
        "restore_id": restore_id,
    }


def value_sha256(entry: Any) -> str:
    """The content version a projection of `entry` belongs to: the `content_sha256`
    of the row its value wrote, which the row's triple links carry."""
    return sha256_value(entry.value)


def triple_row_link(
    org: str,
    namespace: str,
    memory_id: str,
    content_sha256: str,
    triple: tuple[str, str, str],
    *,
    user_id: str,
    inferred: bool = False,
) -> dict[str, Any]:
    """The `entity_triple_rows` row saying the row `memory_id`, written by `user_id`
    and holding the value `content_sha256` hashes, stated `triple` (or, `inferred`,
    that the triple plane wrote it for that row)."""
    subject, relation, obj = triple
    return {
        "org": org,
        "namespace": namespace,
        "memory_id": memory_id,
        "user_id": user_id,
        "content_sha256": content_sha256,
        "subject": subject,
        "relation": relation,
        "object": obj,
        "inferred": inferred,
    }


async def write_entity_triples(
    input: MemoryBulkSetInput,
    results: list[_BulkSetResult],
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
    dedup_token: str = "",
    arrived_at: datetime | None = None,
    inserted_at: datetime | None = None,
    repaired: list[IntervalRepair] | None = None,
) -> None:
    """Insert the write's (subject, relation, object) triples and repair the intervals
    they contradict. A failed insert raises `TriplesInsertFailed`; its replay passes
    `arrived_at` to date the triples as the write did. A failed repair raises
    `TriplesRepairFailed`; its replay passes `inserted_at`, `repaired` and `entity_map`
    and only finishes the repair. Both are recorded by every caller for replay."""
    from server.memory.app.entity_resolution import apply_entity_resolution
    from server.memory.domain.graph_quality import (
        canonical_entity_text,
        canonical_relation,
        facet_triples,
    )
    from server.memory.domain.search_text import canonicalize_verb
    from server.settings import settings as _settings

    # adapter_name = the extractor model, so an operator can roll back triples
    # from a specific extractor. No per-relation confidence yet; carry the
    # parent memory's through.
    _adapter = _settings.llm_model

    # The arrival orders this write's values, to the microsecond; a replay reuses it.
    # Each triple is seen one microsecond after the one stated before it, so the stored
    # key keeps the write's order. `last_seen` and `invalid_at` store the arrival's
    # second, even when a long write's last offsets run into the next second; an
    # undated triple starts in the second of its own `seen_at`.
    arrived = inserted_at or arrived_at or naive_utc_now()
    now = arrived.replace(microsecond=0)
    triples: list[dict[str, Any]] = []
    triple_memories: list[dict[str, Any]] = []
    triple_rows: list[dict[str, Any]] = []
    # The names this write resolved to something else, for a replay to reuse.
    resolved_names: dict[str, str] = {}
    if entity_map is None:
        relation_entities: list[str] = []
        for entry in input.entries:
            for rel in list(entry.relations or []):
                if isinstance(rel, (list, tuple)) and len(rel) == 3:
                    relation_entities.extend([str(rel[0]), str(rel[2])])
        entity_map = await resolve_projection_entity_map(
            input,
            relation_entities,
            ch=ch,
        )

    from server.memory.domain import triple_provenance as _tp

    for entry, result in zip(input.entries, results, strict=True):
        rels = list(entry.relations or [])
        entry_files = [str(p) for p in (getattr(entry, "files", None) or []) if str(p).strip()]
        entry_concepts = [
            str(c) for c in (getattr(entry, "concepts", None) or []) if str(c).strip()
        ]
        entry_topics = [str(t) for t in (getattr(entry, "topics", None) or []) if str(t).strip()]
        if not rels and not entry_files and not entry_concepts and not entry_topics:
            continue
        # Parent-memory confidence flows to each derived triple (default 1.0).
        _conf = float(getattr(entry, "confidence", 1.0) or 1.0)
        # Triple validity from the parent's valid_at/valid_to, falling back to
        # temporal_anchor (an event triple is valid from the date it's ABOUT,
        # and non-pipeline writers often carry only the anchor). NULL = always.
        _entry_valid_from = parse_iso_dt(getattr(entry, "valid_at", "") or "") or parse_iso_dt(
            getattr(entry, "temporal_anchor", "") or ""
        )
        _entry_valid_to = parse_iso_dt(getattr(entry, "valid_to", "") or "")
        kept = 0

        # Bind per-iteration values as defaults so the closure captures THIS
        # entry's window/result, not the loop's last (B023).
        # The fact's own prose, normalised once, decides whether
        # an extractor relation is `extracted` (both endpoints in the text) or
        # `inferred`; loom-minted facet edges are `facet`.
        _carrier_norm = _tp.normalize(_tp.carrier_text(getattr(entry, "value", "")))

        def _append_triple(
            s: str,
            v: str,
            o: str,
            conf: float,
            *,
            provenance: str = "",
            _cn: str = _carrier_norm,
            _vf: Any = _entry_valid_from,
            _vt: Any = _entry_valid_to,
            _mk: Any = result.memory_key,
            _mid: str = result.memory_id,
            _sha: str = value_sha256(entry) if result.memory_id else "",
        ) -> None:
            nonlocal kept
            if kept >= MAX_TRIPLES_PER_ROW:
                return
            if len(s) < 2 or len(v) < 2 or len(o) < 1:
                return
            if not provenance:
                provenance = _tp.classify(s, o, _cn)
            triples.append(
                entity_triple_row(
                    input.org,
                    input.namespace,
                    s,
                    v,
                    o,
                    provenance=provenance,
                    confidence=conf,
                    adapter_name=_adapter,
                    last_seen=now,
                    seen_at=arrived + timedelta(microseconds=len(triples)),
                    valid_from=_vf,
                    valid_to=_vt,
                )
            )
            triple_memories.append(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "subject": s,
                    "relation": v,
                    "object": o,
                    "memory_key": _mk,
                    "seen_at": now,
                    "confidence": conf,
                    "adapter_name": _adapter,
                }
            )
            # A facet edge is the agent's, shared by every row that names it: no row's.
            if _mid and provenance != _tp.FACET:
                triple_rows.append(
                    triple_row_link(
                        input.org, input.namespace, _mid, _sha, (s, v, o), user_id=input.user_id
                    )
                )
            kept += 1

        for rel in rels:
            if kept >= MAX_TRIPLES_PER_ROW:
                break
            if not isinstance(rel, (list, tuple)) or len(rel) != 3:
                continue
            s = apply_entity_resolution(str(rel[0]), entity_map)
            relation = canonical_relation(canonicalize_verb(str(rel[1])))
            o = apply_entity_resolution(str(rel[2]), entity_map)
            for raw, name in ((rel[0], s), (rel[2], o)):
                if canonical_entity_text(str(raw)) != name:
                    resolved_names[canonical_entity_text(str(raw))] = name
            _append_triple(s, relation.verb, o, min(_conf, relation.confidence))

        fact_verbs = [str(r[1]) for r in rels if isinstance(r, (list, tuple)) and len(r) == 3]
        facets = facet_triples(input.agent, fact_verbs, entry_files, entry_concepts, entry_topics)
        for subject, verb, node in facets:
            _append_triple(subject, verb, node, _conf, provenance=_tp.FACET)

    if not triples:
        return
    from server.identity.app.namespace_ontology import load_relation_cardinality

    order_undated_before_later_starts(
        triples, await load_relation_cardinality(input.org, input.namespace)
    )

    if inserted_at is None:
        try:
            await _insert_triples(
                triples, triple_memories, triple_rows, dedup_token=dedup_token, ch=ch
            )
        except Exception as e:
            raise TriplesInsertFailed(arrived, e) from e

    try:
        await _repair_triples(
            input,
            triples,
            triple_memories,
            at=arrived,
            replay=inserted_at is not None,
            repaired=repaired or [],
            ch=ch,
        )
    except TriplesRepairFailed as e:
        e.entity_map = resolved_names
        raise


async def _insert_triples(
    triples: list[dict[str, Any]],
    triple_memories: list[dict[str, Any]],
    triple_rows: list[dict[str, Any]],
    *,
    dedup_token: str,
    ch: Any,
) -> None:
    """Insert a write's triples, their key links and their row links, before the repair.

    The repair SELECT scans open
    (valid_to IS NULL) rows, so this batch's triples must be visible to
    it: that lets a batch close intervals it contradicts within itself,
    and closes the cross-request race where the previous write's
    fire-and-forget insert was still invisible to this write's repair.
    wait_for_async_insert=1 keeps server-side batching but acks only
    once rows are queryable (the read-your-writes the repair contract
    needs). Ordering safety: same-object rows — including the row just
    inserted for this very triple — are skipped, and the batch's other
    values are ordered by their place in it, so repairing against a set
    that contains this batch is sound.
    The row links land before the triples: a follow that closes a triple
    this write states looks for its link once the close is queued, and
    opens the triple again when it finds one (`reopen_held`)."""
    if triple_rows:
        # Synchronous; a replay's repeat collapses in the ReplacingMergeTree.
        await ch.insert("loom.entity_triple_rows", triple_rows)
    await asyncio.gather(
        *(
            ch.insert(
                f"loom.{t}",
                r,
                settings=graph_insert_settings(t, wait=True, dedup_token=dedup_token),
            )
            for t, r in (
                ("entity_triples", triples),
                ("entity_triple_memories", triple_memories),
            )
            if r
        ),
    )


async def _repair_triples(
    input: MemoryBulkSetInput,
    triples: list[dict[str, Any]],
    triple_memories: list[dict[str, Any]],
    *,
    at: datetime,
    replay: bool,
    repaired: list[IntervalRepair],
    ch: Any,
) -> None:
    """Close what the write's `triples` contradict, as of its arrival `at`; under
    `newest_wins` the rows behind the closed triples retire too. A replay never reads
    the clock: it finishes the closes the write recorded (`repaired`) or, with none
    recorded, repairs only the triples still open. A failure raises
    `TriplesRepairFailed` with the closes made, for the next replay to finish."""
    repairs = repaired
    try:
        if repairs:
            repairs, reissued = await _finish_recorded_repairs(
                input, repairs, triples, triple_memories, at=at, ch=ch
            )
            await close_triples(ch, org=input.org, namespace=input.namespace, closes=reissued)
        else:
            if replay:
                still_open = await open_triples_among(
                    ch,
                    org=input.org,
                    namespace=input.namespace,
                    triples=sorted({(t["subject"], t["relation"], t["object"]) for t in triples}),
                )
                triples = [
                    t for t in triples if (t["subject"], t["relation"], t["object"]) in still_open
                ]
            repairs = await _repair_contradictory_triples(input, triples, at=at, ch=ch)
        if repairs:
            await _repair_contradictory_edges(input, repairs, ch=ch)
            await _retire_memories_behind_repairs(input, repairs, triple_memories, ch=ch)
    except TriplesRepairFailed:
        raise
    except Exception as e:
        raise TriplesRepairFailed(at, e, list(repairs)) from e


async def _finish_recorded_repairs(
    input: MemoryBulkSetInput,
    repairs: list[IntervalRepair],
    triples: list[dict[str, Any]],
    triple_memories: list[dict[str, Any]],
    *,
    at: datetime,
    ch: Any,
) -> tuple[list[IntervalRepair], list[tuple[Any, ...]]]:
    """Of the closes a replayed write recorded, the ones it still makes, and those to
    issue again because they never landed. A value restated since is skipped; one
    restated while the close landed is reopened (`_reopen_restated`)."""
    from server.identity.app.namespace_ontology import load_relation_cardinality

    closed_at = at.replace(microsecond=0)  # as `invalid_at` stores it
    declared = await load_relation_cardinality(input.org, input.namespace)
    states = await _recorded_close_states(input, repairs, triples, at=at, ch=ch)
    skip: set[tuple[str, str, str]] = set()
    unlanded: set[tuple[str, str, str]] = set()
    for r in repairs:
        value = (r.subject, r.relation, r.old_object)
        state = states.get(value)
        if state is None:
            continue
        if state.restated:
            # Restated wins over raced: a newer write already handled the value.
            skip.add(value)
            continue
        # As on the write: only a restatement newer than the closing value reopens.
        current = [
            x
            for x in state.raced
            if restates_current(r, x.valid_from, x.seen, closed_at=closed_at, declared=declared)
        ]
        if current:
            latest = max(current, key=lambda x: observation_key(x.valid_from, x.seen, r.old_object))
            await _reopen_restated(
                input,
                r,
                valid_from=latest.valid_from,
                seen=latest.seen,
                closed_at=closed_at,
                triple_memories=triple_memories,
                ch=ch,
            )
            skip.add(value)
        elif state.unlanded:
            unlanded.add(value)
    kept = [r for r in repairs if (r.subject, r.relation, r.old_object) not in skip]
    closes = [
        (r.subject, r.relation, r.old_object, r.valid_to, r.invalid_at)
        for r in kept
        if (r.subject, r.relation, r.old_object) in unlanded
    ]
    return kept, closes


async def _reopen_restated(
    input: MemoryBulkSetInput,
    repair: IntervalRepair,
    *,
    valid_from: datetime | None,
    seen: datetime,
    closed_at: datetime,
    triple_memories: list[dict[str, Any]],
    ch: Any,
) -> None:
    """Undo the write's close of a value stated again while it landed, first repairing
    that restatement (from `valid_from`, seen at `seen`) against the values written
    since. A newer one closes it again and retires the rows behind it."""
    restated = {
        "subject": repair.subject,
        "relation": repair.relation,
        "object": repair.old_object,
        "valid_from": valid_from,
        "seen_at": seen,
    }
    try:
        # Judged first, while the value is still closed: nothing of its own reopens
        # under the judgment, and a failure here leaves the next replay the same start.
        closed = await repair_intervals(input, [restated], ch=ch, at=seen)
    except TriplesRepairFailed as e:
        # This write's failure, dated at this write, not at the restatement.
        raise RuntimeError("repairing a restated value failed") from e
    value = (repair.subject, repair.relation, repair.old_object)
    lost = [c for c in closed if (c.subject, c.relation, c.old_object) == value]
    await reopen_triples(
        ch,
        org=input.org,
        namespace=input.namespace,
        closes=[(*value, repair.valid_to)],
        closed_at=closed_at,
    )
    # Mutations run in order: this close lands after the reopen.
    await close_triples(
        ch,
        org=input.org,
        namespace=input.namespace,
        closes=[(*value, c.valid_to, c.invalid_at) for c in lost],
    )
    await _retire_memories_behind_repairs(input, lost, triple_memories, ch=ch)


class Restatement(NamedTuple):
    """A row of a closed value stated again: its valid_from and when it was seen."""

    valid_from: datetime | None
    seen: datetime


class RecordedClose(NamedTuple):
    """What became of one value the write recorded closing."""

    restated: bool  # open again since: that write's repair decided
    unlanded: bool  # open, and the close never landed: the replay issues it again
    raced: list[Restatement]  # rows stated while the close landed, which it took


def _closed_value(repair: IntervalRepair) -> tuple[str, str, str]:
    return (repair.subject, repair.relation, repair.old_object)


def _closer_key(repair: IntervalRepair, at: datetime) -> tuple[Any, ...]:
    """Where the value that closed `repair`'s value sits in the order (seen at `at` when
    the repair recorded no arrival)."""
    return observation_key(repair.new_valid_from, repair.new_seen_at or at, repair.new_object)


def _since(row: Restatement, cutoff: datetime, newest: tuple[Any, ...], obj: str) -> bool:
    """Whether a row of `obj` came after the closes: seen after `cutoff`, or newer than
    the newest value that closed it."""
    return row.seen > cutoff or observation_key(row.valid_from, row.seen, obj) > newest


def _triple_key(triple: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(triple["subject"]), str(triple["relation"]), str(triple["object"]))


async def _recorded_close_states(
    input: MemoryBulkSetInput,
    repairs: list[IntervalRepair],
    triples: list[dict[str, Any]],
    *,
    at: datetime,
    ch: Any,
) -> dict[tuple[str, str, str], RecordedClose]:
    """What became of each value the write arriving at `at` recorded closing: restated
    since, open because the close never landed, or stated again while the close landed
    (`RecordedClose`). "Since" is per value, as memory-architecture.md's replay says."""
    values = sorted({_closed_value(r) for r in repairs})
    rows = await ch.query_all(
        """
        SELECT subject, relation, object,
               countIf(invalid_at = {at:DateTime}) > 0 AS landed,
               -- A tuple keeps a NULL valid_from, which an aggregate would skip.
               groupArrayIf(tuple(valid_from, seen_at), valid_to IS NULL) AS opened,
               groupArrayIf(tuple(valid_from, seen_at), invalid_at = {at:DateTime}) AS stamped
        FROM loom.entity_triples
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (subject, relation, object) IN {triples:Array(Tuple(String, String, String))}
          AND ((valid_to IS NULL AND invalid_at IS NULL) OR invalid_at = {at:DateTime})
        GROUP BY subject, relation, object
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "triples": values,
            "at": at.replace(microsecond=0),
        },
    )
    states: dict[tuple[str, str, str], RecordedClose] = {}
    for row in rows:
        key = _triple_key(row)
        closers = [r for r in repairs if _closed_value(r) == key]
        # A row seen before the newest closer cannot beat it on the same day. The
        # write's own rows of the value are never later than that when the write stated
        # the closer, but a stored value that closed the write's own may have been seen
        # before it: the cutoff is then the write's last row of it.
        own = [t["seen_at"] for t in triples if _triple_key(t) == key]
        cutoff = max([*(r.new_seen_at or at for r in closers), *own])
        newest = max(_closer_key(r, at) for r in closers)
        opened = [Restatement(*t) for t in row["opened"]]
        stamped = [Restatement(*t) for t in row["stamped"]]
        # The write's own rows are never stated since.
        later = [
            t
            for t in [*opened, *stamped]
            if t.seen not in own and _since(t, cutoff, newest, key[2])
        ]
        restated = bool(opened) and (bool(row["landed"]) or any(t in later for t in opened))
        states[key] = RecordedClose(
            restated=restated,
            unlanded=bool(opened) and not restated,
            raced=[t for t in stamped if t in later],
        )
    return states


async def repair_intervals(
    input: Any, triples: list[dict[str, Any]], *, ch: Any, at: datetime
) -> list[IntervalRepair]:
    """Close the triples and edges the new `triples` contradict on a single-valued
    relation, as of their arrival `at`; no memory row retires. Returns the repairs
    made."""
    repairs = await _repair_contradictory_triples(input, triples, at=at, ch=ch)
    if repairs:
        await _repair_contradictory_edges(input, repairs, ch=ch)
    return repairs


# ---------------------------------------------------------------------------
# One mutation per table per write. One `ALTER TABLE … UPDATE` per supersession
# would rewrite the table over and over, in a mutation queue shared with erasure.
# The closes of one write are folded into one UPDATE per table: the row's key is
# matched by `IN` and its own valid_to is looked up positionally with
# arrayElement/indexOf.
# ---------------------------------------------------------------------------
_GRAPH_KEY_SEP = "\x1f"
_EDGE_KEY_EXPR = (
    "concat(entity_a, char(31), entity_b, char(31), "
    "ifNull(toString(toUnixTimestamp(valid_from)), ''))"
)


def _edge_key(entity_a: str, entity_b: str, valid_from: datetime | None) -> str:
    import calendar

    vf = "" if valid_from is None else str(calendar.timegm(valid_from.timetuple()))
    return _GRAPH_KEY_SEP.join((entity_a, entity_b, vf))


# Each row's own entry in the parallel arrays a triple close passes, and the valid_to
# it writes: never before the interval starts (a future-dated fact).
_TRIPLE_AT = "indexOf({triples:Array(Tuple(String, String, String))}, (subject, relation, object))"
_CLOSED_UNTIL = f"arrayElement({{valid_tos:Array(DateTime)}}, {_TRIPLE_AT})"
_CLOSED_VALID_TO = f"greatest({_CLOSED_UNTIL}, ifNull(valid_from, {_CLOSED_UNTIL}))"


async def close_triples(
    ch: Any,
    *,
    org: str,
    namespace: str,
    closes: list[tuple[str, str, str, datetime, datetime | None]],
) -> int:
    """Close open (subject, relation, object) intervals at each one's own
    valid_to — one UPDATE per table, two when only some closes carry an
    invalid_at (transaction time). Returns the number of mutations issued."""
    issued = 0
    for with_invalid_at in (True, False):
        group = [c for c in closes if (c[4] is not None) is with_invalid_at]
        if not group:
            continue
        set_cols = f"valid_to = {_CLOSED_VALID_TO}"
        params: dict[str, Any] = {
            "org": org,
            "ns": namespace,
            "triples": [(s, r, o) for s, r, o, _, _ in group],
            "valid_tos": [c[3] for c in group],
        }
        if with_invalid_at:
            set_cols += (
                f", invalid_at = arrayElement({{invalid_ats:Array(DateTime)}}, {_TRIPLE_AT})"
            )
            params["invalid_ats"] = [c[4] for c in group]
        await ch.execute(
            f"""
            ALTER TABLE loom.entity_triples
            UPDATE {set_cols}
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND (subject, relation, object) IN {{triples:Array(Tuple(String, String, String))}}
              AND valid_to IS NULL
            """,
            params=params,
        )
        issued += 1
    if issued:
        # A mutation is no INSERT, so the client's graph-write mark does not see it.
        from server.memory.app.graph_write_marks import marks as _graph_marks

        _graph_marks().note_write(org, namespace)
    return issued


async def reopen_triples(
    ch: Any,
    *,
    org: str,
    namespace: str,
    closes: list[tuple[str, str, str, datetime]],
    closed_at: datetime,
) -> None:
    """Undo a `close_triples` call for `closes`, each (subject, relation, object,
    valid_to) as it was passed: the rows it stamped carry its `invalid_at` and that
    valid_to as it clamped it. Mutations run in the order they were made, so this one
    lands after that close, and the close takes only rows still open. The interval
    repair stamps `invalid_at` too: a repair close of the same triple in the same second
    at the same valid_to is undone with it."""
    if not closes:
        return
    await ch.execute(
        f"""
        ALTER TABLE loom.entity_triples
        UPDATE valid_to = NULL, invalid_at = NULL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND (subject, relation, object) IN {{triples:Array(Tuple(String, String, String))}}
          AND invalid_at = {{closed_at:DateTime}}
          AND valid_to = {_CLOSED_VALID_TO}
        """,
        params={
            "org": org,
            "ns": namespace,
            "triples": [(s, r, o) for s, r, o, _ in closes],
            "valid_tos": [c[3] for c in closes],
            "closed_at": closed_at,
        },
    )
    from server.memory.app.graph_write_marks import marks as _graph_marks

    _graph_marks().note_write(org, namespace)


async def open_triples_among(
    ch: Any, *, org: str, namespace: str, triples: list[tuple[str, str, str]]
) -> set[tuple[str, str, str]]:
    """Which of `triples` are open."""
    if not triples:
        return set()
    rows = await ch.query_all(
        """
        SELECT subject, relation, object
        FROM loom.entity_triples
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (subject, relation, object) IN {triples:Array(Tuple(String, String, String))}
          AND valid_to IS NULL AND invalid_at IS NULL
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "triples": triples},
    )
    return {(r["subject"], r["relation"], r["object"]) for r in rows}


async def _repair_contradictory_triples(
    input: MemoryBulkSetInput,
    new_triples: list[dict[str, Any]],
    *,
    ch: Any,
    at: datetime,
) -> list[IntervalRepair]:
    """Close open intervals contradicted by new single-valued triples.

    The namespace's declared relation cardinalities are consulted first (seed list
    second). Values order by `observation_key`; a new triple without its own
    `seen_at` arrived at `at`, this write's arrival. An older value closes when the
    newer starts, a late-arriving older value closes the new row, and every close
    stamps `invalid_at` (the second of `at`) so the supersession axis records when
    loom learned it.
    """

    from server.identity.app.namespace_ontology import load_relation_cardinality
    from server.memory.domain.graph_quality import (
        is_single_valued_relation,
        triple_interval_repairs,
    )

    declared = await load_relation_cardinality(input.org, input.namespace)
    second = at.replace(microsecond=0)  # as `invalid_at` stores it
    # Candidates: dated triples (any relation — the gate inside
    # triple_interval_repairs decides) plus UNDATED triples on single-valued
    # relations, which order by arrival time.
    dated = [
        t
        for t in new_triples
        if t.get("valid_from") is not None
        or is_single_valued_relation(str(t.get("relation") or ""), declared)
    ]
    if not dated:
        return []
    subjects = sorted({str(t["subject"]) for t in dated})
    relations = sorted({str(t["relation"]) for t in dated})
    # select_sequential_consistency: on CH Cloud (SharedMergeTree) this
    # forces a fresh metadata snapshot so the rows this request just
    # inserted (wait_for_async_insert=1) are guaranteed visible even when
    # the SELECT lands on a different replica. No-op on plain MergeTree.
    # The rows another repair closed in this second are read too, so the re-check
    # after the close does not take them for a racing write; only open rows repair.
    existing = await ch.query_all(
        """
        SELECT subject, relation, object, valid_from, valid_to, seen_at
        FROM loom.entity_triples
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND subject IN {subjects:Array(String)}
          AND relation IN {relations:Array(String)}
          AND (valid_to IS NULL OR invalid_at = {at:DateTime})
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "subjects": subjects,
            "relations": relations,
            "at": second,
        },
    )

    def other_values(triple: dict[str, Any], others: list[dict[str, Any]]) -> set[str]:
        return {
            str(t["object"])
            for t in others
            if str(t["subject"]) == str(triple["subject"])
            and str(t["relation"]) == str(triple["relation"])
            and str(t["object"]) != str(triple["object"])
        }

    all_repairs: list[IntervalRepair] = []
    for i, triple in enumerate(dated):
        # A later triple in this same write for the same subject + relation
        # is the value that should survive; leave its row to its own repair.
        repairs = triple_interval_repairs(
            existing=existing,
            new_subject=str(triple["subject"]),
            new_relation=str(triple["relation"]),
            new_object=str(triple["object"]),
            new_valid_at=triple.get("valid_from"),
            superseded_at=second,
            declared=declared,
            later_batch_objects=other_values(triple, dated[i + 1 :]),
            new_seen_at=triple.get("seen_at") or at,
        )
        all_repairs.extend(repairs)
    # One close per start of a value, at its EARLIEST end: a batch carrying the
    # whole history of a single-valued relation would otherwise close the oldest
    # value once per newer value. Each start keeps its own repair, because each
    # start is its own edge.
    earliest: dict[tuple[str, str, str, datetime | None], IntervalRepair] = {}
    for repair in all_repairs:
        key = (repair.subject, repair.relation, repair.old_object, repair.valid_from)
        prior = earliest.get(key)
        if prior is None:
            earliest[key] = repair
            continue
        # It records the newest value that closed it: a restatement has to beat that one.
        first = min(prior, repair, key=lambda r: r.valid_to)
        newest = max(prior, repair, key=lambda r: _closer_key(r, at))
        earliest[key] = replace(
            first,
            new_object=newest.new_object,
            new_valid_from=newest.new_valid_from,
            new_seen_at=newest.new_seen_at,
        )
    all_repairs = list(earliest.values())
    # valid_to is the older fact's stated end; invalid_at (when the repair
    # carries it) is the transaction time loom learned the fact was stale. The
    # triple close is per value, so the starts of one value share one close.
    closes = [(r.subject, r.relation, r.old_object, r.valid_to, r.invalid_at) for r in all_repairs]
    try:
        await close_triples(
            ch, org=input.org, namespace=input.namespace, closes=list(dict.fromkeys(closes))
        )
        return await _undo_raced_closes(
            input, all_repairs, existing, closed_at=second, declared=declared, ch=ch
        )
    except Exception as e:
        # The closes may have landed: a replay finishes them.
        raise TriplesRepairFailed(at, e, list(all_repairs)) from e


async def _undo_raced_closes(
    input: MemoryBulkSetInput,
    repairs: list[IntervalRepair],
    read: list[dict[str, Any]],
    *,
    closed_at: datetime,
    declared: Mapping[str, str],
    ch: Any,
) -> list[IntervalRepair]:
    """Undo the closes of a value another write stated again while this repair ran
    ("back to London"), when it now has more restatements newer than its closer
    (`restates_current`) than the repair `read`. Returns the repairs that stand."""
    if not repairs:
        return repairs
    rows = await ch.query_all(
        """
        SELECT subject, relation, object, valid_from, seen_at
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND (subject, relation, object) IN {triples:Array(Tuple(String, String, String))}
          AND (valid_to IS NULL OR invalid_at = {closed_at:DateTime})
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "triples": sorted({(r.subject, r.relation, r.old_object) for r in repairs}),
            "closed_at": closed_at,
        },
    )

    def current(found: list[dict[str, Any]], repair: IntervalRepair) -> int:
        # The rows of the closed value that are newer than the value closing it.
        return sum(
            1
            for r in found
            if (str(r["subject"]), str(r["relation"]), str(r["object"]))
            == (repair.subject, repair.relation, repair.old_object)
            and restates_current(
                repair, r["valid_from"], r["seen_at"], closed_at=closed_at, declared=declared
            )
        )

    undone = [r for r in repairs if current(rows, r) > current(read, r)]
    if not undone:
        return repairs
    await reopen_triples(
        ch,
        org=input.org,
        namespace=input.namespace,
        closes=[(r.subject, r.relation, r.old_object, r.valid_to) for r in undone],
        closed_at=closed_at,
    )
    return [r for r in repairs if r not in undone]


def retirement_victims(
    repairs: list[IntervalRepair],
    links: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    *,
    new_keys: set[str],
) -> list[dict[str, Any]]:
    """Pure: which memory rows retire behind repaired triples.

    `links` are entity_triple_memories rows (subject, relation, object,
    memory_key); `memories` are live loom.memory rows for the linked keys. A
    memory retires when it carried a CLOSED value (subject, relation,
    old_object) and did not also carry the surviving value, was not written by
    this batch (`new_keys`) and is not pinned. `superseded_by` is the memory
    that carried the new object, when one is known, else empty."""
    closed = {(r.subject, r.relation, r.old_object) for r in repairs}
    survivors: dict[tuple[str, str], str] = {}
    for r in repairs:
        survivors[(r.subject, r.relation)] = getattr(r, "new_object", "") or ""
    by_key: dict[str, dict[str, Any]] = {str(m["memory_key"]): m for m in memories}
    carried_closed: dict[str, tuple[str, str]] = {}
    carried_survivor: set[str] = set()
    survivor_key_for: dict[tuple[str, str], str] = {}
    for ln in links:
        key = str(ln.get("memory_key") or "")
        trip = (str(ln.get("subject")), str(ln.get("relation")), str(ln.get("object")))
        if trip in closed:
            carried_closed.setdefault(key, trip[:2])
        elif survivors.get(trip[:2]) and trip[2] == survivors[trip[:2]]:
            carried_survivor.add(key)
            survivor_key_for.setdefault(trip[:2], key)
    out: list[dict[str, Any]] = []
    for key, sr in carried_closed.items():
        if key in new_keys or key in carried_survivor:
            continue
        m = by_key.get(key)
        if m is None or bool(m.get("pinned")):
            continue
        sk = survivor_key_for.get(sr, "")
        by = by_key.get(sk) if sk else None
        out.append(
            {
                "memory_id": str(m["memory_id"]),
                "namespace": str(m["namespace"]),
                "agent": str(m["agent"]),
                "user_id": str(m["user_id"]),
                "session_id": str(m["session_id"]),
                "memory_key": key,
                "superseded_by": str(by["memory_id"]) if by else "",
                "superseded_by_key": sk if by else "",
            }
        )
    return out


async def _retire_memories_behind_repairs(
    input: MemoryBulkSetInput,
    repairs: list[IntervalRepair],
    triple_memories: list[dict[str, Any]],
    *,
    ch: Any,
) -> int:
    """`newest_wins`: the memory rows that carried a closed
    single-valued value retire (status superseded, superseded_by, invalid_at)
    through the same version re-emit dedup uses, so recall drops them and the
    audit trail keeps them. Only under the namespace policy; never pinned rows,
    never this batch's own rows. Best effort."""
    if not repairs:
        return 0
    policy = await _conflicts.namespace_conflict_policy(input.org, input.namespace, ch=ch)
    if policy != "newest_wins":
        return 0
    from server.memory.store import supersede_duplicates

    subjects = sorted({r.subject for r in repairs})
    relations = sorted({r.relation for r in repairs})
    # select_sequential_consistency: the link rows for THIS batch were inserted
    # moments ago; on CH Cloud a read that lands on another replica would miss
    # them and the closed carrier would look unlinked (the repair read above
    # sets it for the same reason). No-op on plain MergeTree.
    links = await ch.query_all(
        """
        SELECT subject, relation, object, memory_key
        FROM loom.entity_triple_memories
        WHERE org = {org:String} AND namespace = {ns:String}
          AND subject IN {subjects:Array(String)} AND relation IN {relations:Array(String)}
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "subjects": subjects,
            "relations": relations,
        },
    )
    keys = sorted({str(ln["memory_key"]) for ln in links if ln.get("memory_key")})
    if not keys:
        return 0
    from server.identity.app.request_scope import request_scope
    from server.identity.domain.read_scope import scope_and

    params: dict[str, Any] = {"org": input.org, "ns": input.namespace, "keys": keys}
    # A write retires only rows inside the writer's read scope, as the same-key judge does.
    in_scope = scope_and(request_scope(), params)
    memories = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS memory_id, namespace, agent, user_id, session_id,
               memory_key, pinned
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key IN {{keys:Array(String)}} AND tombstoned_at IS NULL
          AND status IN ('active', 'duplicate', 'conflicted'){in_scope}
        SETTINGS select_sequential_consistency = 1
        """,
        params=params,
    )
    new_keys = {str(tm.get("memory_key") or "") for tm in triple_memories}
    victims = retirement_victims(repairs, links, memories, new_keys=new_keys)
    if not victims:
        log.debug(
            "newest_wins: %d repaired triples, %d links, %d live carriers, nothing to retire",
            len(repairs),
            len(links),
            len(memories),
        )
        return 0
    from server.memory.app.auto_resolution import (
        NEWEST_WINS_CONTRADICTION,
        RetirementTracker,
        record_auto_supersessions,
    )
    from server.memory.domain.lock import refuses_retirement

    # The tracker also refuses a row retired since the read (it is not re-pointed).
    tracker = RetirementTracker(refuses_retirement)
    try:
        n = await supersede_duplicates(
            input.org,
            victims,
            ch=ch,
            skip=tracker.skip,
            on_written=tracker.written,
            on_unconfirmed=tracker.unconfirmed,
        )
    finally:
        # What reached loom.memory, even when the embeddings mirror then failed.
        await record_auto_supersessions(
            org=input.org,
            namespace=input.namespace,
            retired=tracker.retired(victims),
            reason=NEWEST_WINS_CONTRADICTION,
        )
    log.info("newest_wins retired %d memories behind %d repaired triples", n, len(repairs))
    return n


async def _repair_contradictory_edges(
    input: MemoryBulkSetInput,
    repairs: list[IntervalRepair],
    *,
    ch: Any,
) -> None:
    """Close pairwise entity edges for repaired single-valued triples — one
    UPDATE per write, two when only some repairs carry an
    invalid_at. Each edge's own valid_to is looked up by its key."""

    from server.memory.domain.graph_quality import canonical_edge_pair

    seen: set[tuple[str, str, object, object]] = set()
    closes: list[tuple[str, str, datetime | None, datetime, datetime | None]] = []
    for repair in repairs:
        entity_a, entity_b = canonical_edge_pair(repair.subject, repair.old_object)
        dedupe_key = (entity_a, entity_b, repair.valid_from, repair.valid_to)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        closes.append((entity_a, entity_b, repair.valid_from, repair.valid_to, repair.invalid_at))
    for with_invalid_at in (True, False):
        group = [c for c in closes if (c[4] is not None) is with_invalid_at]
        if not group:
            continue
        keys = [_edge_key(a, b, vf) for a, b, vf, _, _ in group]
        idx = f"indexOf({{keys:Array(String)}}, {_EDGE_KEY_EXPR})"
        own_valid_to = f"arrayElement({{valid_tos:Array(DateTime)}}, {idx})"
        # As a triple close does: never before the interval starts.
        set_cols = f"valid_to = greatest({own_valid_to}, ifNull(valid_from, {own_valid_to}))"
        params: dict[str, Any] = {
            "org": input.org,
            "ns": input.namespace,
            "keys": keys,
            "valid_tos": [c[3] for c in group],
        }
        if with_invalid_at:
            set_cols += f", invalid_at = arrayElement({{invalid_ats:Array(DateTime)}}, {idx})"
            params["invalid_ats"] = [c[4] for c in group]
        # Mirror the triple-plane supersession close onto the pairwise edge.
        # The key carries the edge's valid_from (unix seconds, '' for NULL), so
        # an edge is closed only at the interval the triple repair named.
        await ch.execute(
            f"""
            ALTER TABLE loom.entity_edges
            UPDATE {set_cols}
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND {_EDGE_KEY_EXPR} IN {{keys:Array(String)}}
              AND valid_to IS NULL
            """,
            params=params,
        )


async def write_entity_vectors(
    input: MemoryBulkSetInput,
    results: list[Any],  # list[_BulkSetResult]
    embedder: Any,
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
    entity_embeddings: dict[str, list[float]] | None = None,
) -> None:
    """Embed each unique entity once and insert one row per
    (entity, memory_key) pair into loom.entity_vectors.

    `entity_embeddings` are vectors the caller already has (
    bulk_set embeds the entity texts in its value/key batch); only entities
    missing from it are embedded here."""
    entity_pairs: list[tuple[str, str]] = []  # (entity_text, memory_key)
    seen: set[tuple[str, str]] = set()
    if entity_map is None:
        entity_map = await resolve_projection_entity_map(
            input,
            [str(e) for entry in input.entries for e in (entry.entities or [])],
            ch=ch,
        )
    for entry, result in zip(input.entries, results, strict=True):
        for ent_clean in resolved_entity_list(entry.entities or [], entity_map):
            if not ent_clean:
                continue
            pair = (ent_clean, result.memory_key)
            if pair in seen:
                continue
            seen.add(pair)
            entity_pairs.append(pair)
    if not entity_pairs:
        return

    # Embed each unique entity once (texts repeat across memory_keys): one
    # embed_many call per bulk_set instead of N serial round-trips.
    unique_entities = sorted({pair[0] for pair in entity_pairs})
    embeddings: dict[str, list[float]] = {
        ent: list(vec)
        for ent, vec in (entity_embeddings or {}).items()
        if ent in set(unique_entities) and vec
    }
    missing = [ent for ent in unique_entities if ent not in embeddings]
    if missing:
        embedding_list = await embedder.embed_many(missing)
        embeddings.update(zip(missing, embedding_list, strict=True))

    now_utc = naive_utc_now()
    rows = [
        {
            "org": input.org,
            "namespace": input.namespace,
            "agent": input.agent,
            "user_id": input.user_id,
            "entity_text": ent,
            "entity_embedding": embeddings[ent],
            "embedding_model": embedder.model_name,
            "embedding_dim": embedder.dim,
            "memory_key": memory_key,
            "tombstoned_at": None,
            "created_at": now_utc,
        }
        for ent, memory_key in entity_pairs
    ]
    await ch.insert(
        "loom.entity_vectors",
        rows,
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
