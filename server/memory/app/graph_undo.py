"""Undo of a retirement in the fact graph: the facts a restored row still holds are
stated again from the restore, as triple and edge markers carrying the Undo's id."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from server.memory.app.entity_projections import (
    held_by_unlinked_rows,
    linked_triples,
    stated_by_live_rows,
)
from server.memory.app.projections import entity_edge_row, entity_triple_row, repair_intervals
from server.memory.domain.graph_quality import (
    canonical_edge_pair,
    is_single_valued_relation,
    observation_key,
)


async def rows_still_holding(
    org: str, namespace: str, hashes: Mapping[str, str], *, ch: Any
) -> list[str]:
    """The rows of `hashes` (id to `content_sha256`) whose value is still that one: a row
    revised since an Undo is its revision's to reconcile, not the Undo's."""
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS id, toString(content_sha256) AS sha
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_id IN {ids:Array(UUID)}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "ids": sorted(hashes)},
    )
    return sorted(str(r["id"]) for r in rows if hashes.get(str(r["id"])) == str(r["sha"]))


async def reopen_held_facts(
    input: Any,
    ids: list[str],
    *,
    ch: Any,
    restore_id: str,
    restored_at: datetime,
) -> None:
    """Open again the facts the rows `ids` hold now: an Undo brings a retired row's
    facts back. An Undo is an assertion made at the restore, so each fact gets a new
    observation starting then, repaired like any other value; no row retires.
    `restore_id` names this Undo's markers, so a replay with the same id and
    `restored_at` finds and restates them."""
    from server.identity.app.namespace_ontology import load_relation_cardinality

    # Only what the restored rows' values hold now: another row's claim keeps a fact
    # from closing, but never reopens one.
    linked = await linked_triples(input, ids, ch=ch)
    held = await stated_by_live_rows(input.org, input.namespace, linked, ch=ch, rows=ids)
    # A row written before rows were linked has no links: read what its value names of
    # its key's facts (a single row that has links cannot be one).
    if not (linked and len(ids) == 1):
        held |= await held_by_unlinked_rows(input.org, input.namespace, None, ch=ch, rows=ids)
    if not held:
        return
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
            adapter_name="row-restore",
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
    await repair_intervals(input, restated, ch=ch, at=restored_at)


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
