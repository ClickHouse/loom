"""Undo of a retirement in the fact graph: the facts a restored row still holds are
stated again from the restore, as triple and edge markers carrying the Undo's id."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from server.memory.app import graph_holders
from server.memory.app.graph_holders import held_by_unlinked_rows, stated_by_live_rows
from server.memory.app.projection_errors import TriplesRepairFailed
from server.memory.app.projections import (
    entity_edge_row,
    entity_triple_row,
    finish_interval_repair,
    repair_intervals,
)
from server.memory.app.triple_projections import close_dropped_facts, linked_triples
from server.memory.domain.graph_quality import (
    canonical_edge_pair,
    is_single_valued_relation,
    observation_key,
)


async def rows_still_holding(
    org: str, namespace: str, hashes: Mapping[str, str], *, ch: Any
) -> list[str]:
    """The rows of `hashes` (id to `content_sha256`) whose value is still that one and
    that may still hold graph facts: a row revised or retired since an Undo is its
    revision's or retirement's to reconcile, not the Undo's."""
    return sorted(await graph_holders.rows_holding(org, namespace, list(hashes.items()), ch=ch))


async def _held_now(input: Any, ids: list[str], *, ch: Any) -> frozenset[Any]:
    """What the rows `ids` hold now. Only their own values: another row's claim keeps a
    fact from closing, but never reopens one."""
    linked = await linked_triples(input, ids, ch=ch)
    held = await stated_by_live_rows(input.org, input.namespace, linked, ch=ch, rows=ids)
    # A row with no links of its value's own (written before row links, or by a build
    # without them): read what its value names of its key's facts.
    held |= await held_by_unlinked_rows(input.org, input.namespace, None, ch=ch, rows=ids)
    return held


async def reopen_held_facts(
    input: Any,
    ids: list[str],
    *,
    ch: Any,
    restore_id: str,
    restored_at: datetime,
    hashes: Mapping[str, str] | None = None,
    facts: frozenset[Any] | None = None,
    adapter_name: str = "row-restore",
) -> None:
    """Open again the facts the rows `ids` hold now, as an assertion made at the restore:
    `restore_id` and `restored_at` name its markers, so a replay restates the same ones.
    `hashes`: only rows still holding that value; `facts`: only those (a fallback)."""
    if hashes:
        ids = sorted(i for i in ids if i in hashes)
    asked = ids
    held: frozenset[Any] = frozenset()
    while ids:
        held = await _held_now(input, ids, ch=ch)
        if facts is not None:
            held &= facts
        if not hashes:
            break
        # Checked after the read: a row revised or retired since the restore, even while
        # this read ran, is left to that change, so it is dropped and the rest read again.
        still = await rows_still_holding(
            input.org, input.namespace, {i: hashes[i] for i in ids}, ch=ch
        )
        if still == ids:
            break
        ids = still
    if ids and held:
        await restate_facts(
            input,
            held,
            ch=ch,
            restore_id=restore_id,
            restored_at=restored_at,
            adapter_name=adapter_name,
        )
    if hashes and asked:
        # A row revised or retired after the check above ran its own close before these
        # markers landed, or before an earlier attempt's did: close what it no longer
        # holds now.
        still = await rows_still_holding(
            input.org, input.namespace, {i: hashes[i] for i in asked}, ch=ch
        )
        changed = [i for i in asked if i not in still]
        if changed:
            await close_dropped_facts(input, changed, ch=ch)


async def restate_facts(
    input: Any,
    held: frozenset[Any],
    *,
    ch: Any,
    restore_id: str,
    restored_at: datetime,
    adapter_name: str = "row-restore",
) -> None:
    """Write the markers for the facts `held`, stated again at `restored_at`, and repair
    them, for `reopen_held_facts`."""
    from server.identity.app.namespace_ontology import load_relation_cardinality

    now = restored_at.replace(microsecond=0)  # as the DateTime columns store it
    # On the fly: a retirement's closes are mutations, and a row they will close is
    # weighed as closed already (the close expressions are deterministic).
    latest = await ch.query_all(
        """
        WITH restore_id = {rid:String} AS ours
        SELECT subject, relation, object, argMax(confidence, seen_at) AS confidence,
               argMax(provenance, seen_at) AS provenance,
               -- The latest row's place in the order of its relation.
               max(seen_at) AS seen, argMax(tuple(valid_from), seen_at).1 AS start,
               argMax(valid_to IS NULL, seen_at) AS open,
               -- The newest open row by start day, then seen, as the repair orders them;
               -- a fact with none reads back as the 1970 default, older than any marker.
               argMaxIf(tuple(valid_from, seen_at),
                        tuple(toDate(ifNull(valid_from, toDateTime(seen_at))), seen_at),
                        valid_to IS NULL) AS newest_open,
               -- A tuple keeps a NULL (open) end, which argMax would skip. Only the
               -- latest row's own end: a supersession close stamps `invalid_at`.
               argMax(tuple(if(invalid_at IS NULL, valid_to, NULL)), seen_at).1 AS ends,
               countIf(ours) AS restored,
               -- This Undo's own marker, found by a replay: its start and when seen.
               minIf(valid_from, ours) AS marker_start, minIf(seen_at, ours) AS marker_seen,
               -- The intervals of the rows the readers count, weighed against the marker's.
               groupArrayIf(tuple(valid_from, valid_to),
                            co_occur > 0 AND invalid_at IS NULL) AS support
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND (subject, relation, object) IN {triples:Array(Tuple(String, String, String))}
        GROUP BY subject, relation, object
        SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "triples": sorted(held),
            "rid": restore_id,
        },
    )
    # Marker i is seen i microseconds after the restore, in the facts' stored order, so
    # the later of two values the rows hold stays the later one.
    latest = sorted(latest, key=lambda r: observation_key(r["start"], r["seen"], r["object"]))
    markers = _markers(latest, restored_at, now)
    declared = await load_relation_cardinality(input.org, input.namespace)
    # A marker is never current when the repair closes its value as it starts: another
    # value's marker is newer than the value's newest row and starts no later.
    beaten = {
        _triple(m)
        for m in markers
        if is_single_valued_relation(m.row["relation"], declared)
        and any(
            (n.row["subject"], n.row["relation"]) == (m.row["subject"], m.row["relation"])
            and n.start <= m.start
            and _key(n) > _newest(m)
            for n in markers
            if n is not m
        )
    }
    # This Undo's markers are the rows carrying its id: a replay finds them and adds
    # none, and no other Undo takes them for its own.
    fresh = [m for m in markers if not m.row["restored"]]
    settings = {"async_insert": 1, "wait_for_async_insert": 1}
    # Edges first: if the triple markers then fail to land, a replay finds these edges
    # and adds none.
    await _insert_edge_markers(
        input, fresh, beaten, now, restore_id=restore_id, settings=settings, ch=ch
    )
    rows = [
        entity_triple_row(
            input.org,
            input.namespace,
            m.row["subject"],
            m.row["relation"],
            m.row["object"],
            provenance=m.row["provenance"],
            confidence=float(m.row["confidence"]),
            adapter_name=adapter_name,
            last_seen=now,
            seen_at=m.seen,
            # Dated at the Undo; a closed interval before it stays as history.
            valid_from=m.start,
            valid_to=_still_ahead(m.row["ends"], m.start),
            # The Undo states nothing new: it only gives a closed fact its weight back,
            # so Undoing back and forth adds none, and a value it closes as it starts
            # gets none.
            co_occur=0
            if _covered(m.row["support"], m.start, m.row["ends"]) or _triple(m) in beaten
            else 1,
            restore_id=restore_id,
        )
        for m in fresh
    ]
    if rows:
        await ch.insert("loom.entity_triples", rows, settings=settings)
    restated = [
        {
            "subject": m.row["subject"],
            "relation": m.row["relation"],
            "object": m.row["object"],
            "valid_from": m.start,
            "seen_at": m.seen,
        }
        for m in markers
    ]
    try:
        # The closes this restates against may still be pending mutations.
        await repair_intervals(input, restated, ch=ch, at=restored_at, on_fly=True)
    except TriplesRepairFailed as e:
        # Recorded by the caller with this id, for `finish_restated_repair` to finish.
        e.restore_id = restore_id
        raise
    except Exception as e:
        raise TriplesRepairFailed(restored_at, e, restore_id=restore_id) from e


async def finish_restated_repair(
    input: Any,
    restore_id: str,
    *,
    restored_at: datetime,
    repaired: list[Any],
    ch: Any,
) -> None:
    """Finish the interval repair a `restate_facts` with `restore_id` recorded failing,
    with the closes it had made (`repaired`) and their edges: the markers carrying the
    id are the triples it was repairing."""
    markers = await ch.query_all(
        """
        SELECT subject, relation, object, valid_from, seen_at
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String} AND restore_id = {rid:String}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "rid": restore_id},
    )
    try:
        await finish_interval_repair(
            input, [dict(m) for m in markers], at=restored_at, repaired=repaired, ch=ch
        )
    except TriplesRepairFailed as e:
        # The closes this finish made ride the failure under the same id, as in
        # `restate_facts`: a closed triple is not found open again.
        e.restore_id = restore_id
        raise


class _Marker(NamedTuple):
    """One fact an Undo restates: its latest stored row, when the marker is seen, and
    when it starts."""

    row: Mapping[str, Any]
    seen: datetime
    start: datetime


def _markers(latest: list[Mapping[str, Any]], restored: datetime, now: datetime) -> list[_Marker]:
    """The Undo's markers, in the facts' stored order. A replay restates the markers it
    finds as they are stored, and a fact held only since gets a marker after all of
    them, so no two share a `seen_at`."""
    stored = [r["marker_seen"] for r in latest if r["restored"]]
    first = max(stored) + timedelta(microseconds=1) if stored else restored
    markers = []
    fresh_count = 0
    for r in latest:
        if r["restored"]:
            markers.append(_Marker(r, r["marker_seen"], r["marker_start"]))
        else:
            seen = first + timedelta(microseconds=fresh_count)
            markers.append(_Marker(r, seen, _marker_start(r, now)))
            fresh_count += 1
    return markers


def _marker_start(latest: Mapping[str, Any], now: datetime) -> datetime:
    """A closed fact is restated from the restore, or from its own start when that is
    still to come ("moving to Berlin on Dec 1"). An open fact's latest row may be
    another live row's claim, so the Undo restates it from the restore."""
    start: datetime | None = latest["start"]
    if start is None or latest["open"]:
        return now
    return max(now, start)


def _key(m: _Marker) -> tuple[date, datetime, str]:
    return observation_key(m.start, m.seen, m.row["object"])


def _triple(m: _Marker) -> tuple[str, str, str]:
    return (m.row["subject"], m.row["relation"], m.row["object"])


async def _insert_edge_markers(
    input: Any,
    fresh: list[_Marker],
    beaten: set[tuple[str, str, str]],
    now: datetime,
    *,
    restore_id: str,
    settings: dict[str, Any],
    ch: Any,
) -> None:
    """Give each fresh marker's edge an observation from the Undo, from its earliest
    marker's start to its markers' own end (`_edge_end`): only an edge the graph has,
    weighed like its triples. An edge row carrying the Undo's id is an earlier
    attempt's, and none is added."""
    edge_markers: dict[tuple[str, str], list[_Marker]] = {}
    for m in fresh:
        pair = canonical_edge_pair(m.row["subject"], m.row["object"])
        edge_markers.setdefault(pair, []).append(m)
    pairs = sorted(edge_markers)
    starts = {p: min(m.start for m in edge_markers[p]) for p in pairs}
    edges = (
        await ch.query_all(
            """
            SELECT entity_a, entity_b,
                   countIf(restore_id = {rid:String}) AS restored,
                   groupArrayIf(tuple(valid_from, valid_to),
                                co_occur > 0 AND invalid_at IS NULL) AS support
            FROM loom.entity_edges
            WHERE org = {org:String} AND namespace = {ns:String}
              AND (entity_a, entity_b) IN {pairs:Array(Tuple(String, String))}
            GROUP BY entity_a, entity_b
            SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1
            """,
            params={
                "org": input.org,
                "ns": input.namespace,
                "pairs": pairs,
                "rid": restore_id,
            },
        )
        if pairs
        else []
    )
    edge_rows = []
    for e in edges:
        pair = (e["entity_a"], e["entity_b"])
        if e["restored"]:
            continue
        start, ends = starts[pair], _edge_end(edge_markers[pair])
        lost = all(_triple(m) in beaten for m in edge_markers[pair])
        edge_rows.append(
            entity_edge_row(
                input.org,
                input.namespace,
                pair[0],
                pair[1],
                last_seen=now,
                valid_from=start,
                valid_to=ends,
                co_occur=0 if _covered(e["support"], start, ends) or lost else 1,
                restore_id=restore_id,
            )
        )
    if edge_rows:
        await ch.insert("loom.entity_edges", edge_rows, settings=settings)


def _edge_end(markers: list[_Marker]) -> datetime | None:
    """The end an edge's markers keep: none while any of them is open, else the latest.
    Another relation's row of the same pair never gives the edge its end."""
    ends = [_still_ahead(m.row["ends"], m.start) for m in markers]
    bounded = [end for end in ends if end is not None]
    return max(bounded) if len(bounded) == len(ends) else None


def _newest(m: _Marker) -> tuple[date, datetime, str]:
    """The newest row the repair weighs `m`'s value at: the marker, or the fact's newest
    open row when that is newer."""
    start, seen = m.row["newest_open"]
    return max(_key(m), observation_key(start, seen, m.row["object"]))


def _covered(support: list[tuple[Any, Any]], start: datetime, ends: datetime | None) -> bool:
    """Whether one weighted row (`support`: their intervals) spans the whole of a marker
    from `start` to the end it keeps (none: open), so the marker needs no weight of its
    own. Rows that only together span it do not count: one weight too many at worst,
    never a fact lost."""
    end = _still_ahead(ends, start)
    return any(
        (vf is None or vf <= start) and (vt is None or (end is not None and vt >= end))
        for vf, vt in support
    )


def _still_ahead(end: datetime | None, now: datetime) -> datetime | None:
    """A latest row's stated end the restore keeps: one still to come."""
    return end if end is not None and end > now else None
