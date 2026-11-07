"""The triples a point write states, and their reconcile when a revision, an Undo or a
follow changes what a row holds."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from server.core.ch_writes import async_insert_settings
from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
from server.memory.app import graph_holders
from server.memory.app.projection_errors import (
    ProjectionScope,
    bulk_projection_payload,
    failed_projection,
    record_input_projection_failure,
    restated_repair_payload,
)
from server.memory.app.projection_failures import (
    TRIPLES_RECONCILE,
    TRIPLES_REOPEN,
)
from server.memory.app.projections import (
    as_bulk_write,
    close_triples,
    end_edges,
    end_triples_now,
    entity_triple_row,
    open_triples_among,
    reopen_triples,
    repair_intervals,
    resolve_projection_entity_map,
    triple_row_link,
    write_entity_triples,
)
from server.memory.app.provenance import writer_actor
from server.memory.domain import triples_follow_rows as tfr
from server.memory.domain.temporal_sql import (
    HOLDS_VALUE_SQL,
)

log = logging.getLogger(__name__)


async def project_stated_relations(
    row: Mapping[str, Any],
    value: Any,
    relations: list[tfr.Triple],
    *,
    revised: bool,
    op: str,
    ch: Any,
) -> None:
    """Write the `relations` a point write states, as a `memory.bulk_set` entry's are:
    linked to `row` (the `loom.memory` row holding `value`), and through the interval
    repair, which may close an older single-valued fact and, under `newest_wins`,
    retire the rows behind it. A row that may not hold graph facts (`holds_graph_facts`)
    states none: a proposal is one, and accepting it later projects nothing, so its
    facts are stated again once it is established. Then, for a row `revised`
    (it held a value before this write), the facts it held are reconciled with its
    new value (`close_dropped_facts`). It runs after the same-key supersession and
    before the follow of the rows it retired (`auto_resolution.follow_beside_response`),
    so the follow sees the successor these state. A first write is fenced on the
    value it states (`row`'s `content_sha256`), as its edges are: it states nothing once
    another write revised or retired the row, and is reconciled as a revision if that
    happened while it wrote. Fail-open: a failed write is recorded for the projection
    repair to replay."""
    if relations and graph_holders.holds_graph_facts(row):
        # A revision closes what it dropped itself, after these land.
        if revised or await _holds_its_value(row, ch=ch):
            await _write_stated_relations(row, value, relations, op=op, ch=ch)
            # Overtaken while they were written: the revision's close may have run
            # before they landed, so close them as a revision would.
            if not revised and not await _holds_its_value(row, ch=ch):
                revised = True
    if revised:
        scope = ProjectionScope(
            org=row["org"], namespace=row["namespace"], agent=row["agent"], user_id=row["user_id"]
        )
        await reconcile_row_facts(scope, [str(row["memory_id"])], reopen=False, op=op, ch=ch)


async def _holds_its_value(row: Mapping[str, Any], *, ch: Any) -> bool:
    """Whether the stored row still holds `row`'s value (`HOLDS_VALUE_SQL`)."""
    version = [(str(row["memory_id"]), str(row["content_sha256"]))]
    key = (str(row["agent"]), str(row["user_id"]), str(row["session_id"]), str(row["memory_key"]))
    held = await graph_holders.rows_holding(
        row["org"], row["namespace"], version, ch=ch, holder=(HOLDS_VALUE_SQL, {}), key=key
    )
    return bool(held)


async def _write_stated_relations(
    row: Mapping[str, Any], value: Any, relations: list[tfr.Triple], *, op: str, ch: Any
) -> None:
    try:
        write, results = as_bulk_write(row, value, relations)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("%s: relations not projected: %s", op, e)
        return
    try:
        await write_entity_triples(write, results, ch=ch)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("%s: relations projection failed (recorded for repair): %s", op, e)
        await record_input_projection_failure(
            write,
            projection=failed_projection("entity_triples", e),
            error=e,
            memory_keys=[results[0].memory_key],
            payload=bulk_projection_payload(write, results, error=e),
            source_operation=op,
            source_resource_id=f"{op}:{results[0].memory_id}",
        )


async def reconcile_row_facts(
    input: Any,
    ids: list[str],
    *,
    reopen: bool,
    op: str,
    ch: Any,
    hashes: Mapping[str, str] | None = None,
    restored_at: datetime | None = None,
    restore_id: str | None = None,
) -> None:
    """Fail-open `reopen_held_facts` (`reopen`) or `close_dropped_facts` for the rows
    `ids` of `input`'s namespace. An Undo passes the time it restored at and the
    `content_sha256` each row had (`hashes`): a row revised since is left to its
    revision, now and on a replay. A failure is recorded for the projection repair with
    both and the Undo's id, which its replay finds its rows by. `restore_id`: the same
    for every call that finishes one restore, so a repeat writes no new markers."""
    restored_at, restore_id = restored_at or naive_utc_now(), restore_id or uuid.uuid4().hex
    try:
        if reopen:
            # Lazy: graph_undo imports this module at its top.
            from server.memory.app import graph_undo

            await graph_undo.reopen_held_facts(
                input, ids, ch=ch, restored_at=restored_at, restore_id=restore_id, hashes=hashes
            )
        else:
            await close_dropped_facts(input, ids, ch=ch)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("%s: facts not reconciled (recorded for repair): %s", op, e)
        payload: dict[str, Any] = {"memory_ids": ids, "reopen": reopen}
        if reopen:
            payload["restored_at"] = restored_at.isoformat()
            payload["restore_id"] = restore_id
            payload["content_sha256"] = dict(hashes or {})
        # An Undo's or a fallback's markers whose repair failed: the closes it made.
        payload.update(restated_repair_payload(e))
        await record_input_projection_failure(
            input,
            projection=TRIPLES_RECONCILE,
            error=e,
            payload=payload,
            source_operation=op,
            source_resource_id=f"{op}:{ids[0]}",
        )


async def linked_triples(input: Any, ids: list[str], *, ch: Any) -> list[tfr.Triple]:
    rows = await ch.query_all(
        """
        SELECT DISTINCT subject, relation, object
        FROM loom.entity_triple_rows
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_id IN {ids:Array(UUID)}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "ids": ids},
    )
    return sorted((str(r["subject"]), str(r["relation"]), str(r["object"])) for r in rows)


async def close_dropped_facts(input: Any, ids: list[str], *, ch: Any) -> None:
    """Close the facts linked to the rows `ids` that no row holds any longer: a
    revision that no longer states them (`tfr.carried`), or that made its row one
    that holds none. One still valid later ends now."""
    linked = await linked_triples(input, ids, ch=ch)
    held = await graph_holders.stated_by_live_rows(input.org, input.namespace, linked, ch=ch)
    gone = [t for t in linked if t not in held]
    opened = await open_triples_among(ch, org=input.org, namespace=input.namespace, triples=gone)
    if opened:
        await write_follow_plan(input, tfr.FollowPlan(closes=sorted(opened), inserts=[]), ch=ch)
    await end_triples_now(ch, org=input.org, namespace=input.namespace, triples=gone)
    await after_the_close(input, gone, held, ch=ch)


async def after_the_close(
    input: Any,
    gone: list[tfr.Triple],
    held: frozenset[tfr.Triple],
    *,
    ch: Any,
) -> None:
    """End the edges of the `gone` facts and keep one value current for each
    single-valued relation they touch, from one read of the triples.

    gone: the facts the rows revised or retired no longer keep, closed now or before
        (a replay finds the close already landed and still checks).
    held: the facts the caller found a live row still holds."""
    from server.identity.app.namespace_ontology import load_relation_cardinality
    from server.memory.domain.graph_quality import is_single_valued_relation

    if not gone:
        return
    names = sorted({e for s, _r, o in gone for e in (s, o)})
    declared = await load_relation_cardinality(input.org, input.namespace)
    single = sorted({(s, r) for s, r, _o in gone if is_single_valued_relation(r, declared)})
    # On the fly: the closes just made are mutations that may still be pending.
    rows = await ch.query_all(
        """
        SELECT subject, relation, object, valid_from, valid_to, invalid_at, seen_at
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND ((subject IN {names:Array(String)} AND object IN {names:Array(String)})
               OR (subject, relation) IN {single:Array(Tuple(String, String))})
        SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1
        """,
        params={"org": input.org, "ns": input.namespace, "names": names, "single": single},
    )
    between = [r for r in rows if r["subject"] in names and r["object"] in names]
    await _close_dropped_edges(input, gone, held, between, ch=ch)
    await _keep_one_current(
        input, [r for r in rows if (r["subject"], r["relation"]) in single], ch=ch
    )


async def _close_dropped_edges(
    input: Any,
    gone: list[tfr.Triple],
    held: frozenset[tfr.Triple],
    rows: list[Mapping[str, Any]],
    *,
    ch: Any,
) -> None:
    """End the edges of the `gone` facts with them (`tfr.edge_ends`), unless a live row
    still holds another fact between the same two entities, or a live row's current
    value names both (`end_edges`). `rows` are the stored triples between the
    entities; only those neither in `gone` nor `held` are asked about."""
    known = set(gone) | held
    others = sorted(
        {(str(r["subject"]), str(r["relation"]), str(r["object"])) for r in rows} - known
    )
    held = held | await graph_holders.stated_by_live_rows(input.org, input.namespace, others, ch=ch)
    ends = tfr.edge_ends(gone, held, rows, naive_utc_now())
    await end_edges(ch, org=input.org, namespace=input.namespace, ends=ends)


async def _keep_one_current(input: Any, rows: list[Mapping[str, Any]], *, ch: Any) -> None:
    """One value of a single-valued relation stays current, never neither. For each
    relation of `rows` (the stored triples of the relations a close touched) with no
    value valid now, the newest value a live row still holds is stated again from the
    relation's last close, as an Undo states it, edge included: London undone over
    Paris, then revised away, gives Paris back from then on. It goes through the Undo's
    `reopen_held_facts` with the rows holding it, so a row revised meanwhile is left
    to its revision."""
    from server.memory.app import graph_undo
    from server.memory.domain.graph_quality import observation_key

    now = naive_utc_now()

    # When each relation's open values start ("moving to Berlin on Dec 1").
    open_starts: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        if r["valid_to"] is None:
            open_starts.setdefault((r["subject"], r["relation"]), []).append(r["valid_from"])

    def valid_now(r: Mapping[str, Any]) -> bool:
        started = r["valid_from"] is None or r["valid_from"] <= now
        ends = r["valid_to"]
        if not started or (ends is not None and ends <= now):
            return False
        if ends is None or r["invalid_at"] is None:
            return True
        # A close gave it a later end (`invalid_at` set): it holds on only while an open
        # value starts by then to take over, and not once that value is gone too.
        starts = open_starts.get((r["subject"], r["relation"]), [])
        return any(start is None or start <= ends for start in starts)

    current = {(r["subject"], r["relation"]) for r in rows if valid_now(r)}
    # A value still open starts later ("moving on Dec 1"): it is not stated again.
    closed = [
        r
        for r in rows
        if (r["subject"], r["relation"]) not in current and r["valid_to"] is not None
    ]
    if not closed:
        return
    held = await graph_holders.live_holders(
        input.org,
        input.namespace,
        sorted({(r["subject"], r["relation"], r["object"]) for r in closed}),
        ch=ch,
    )
    # Oldest first, so each relation ends up with its newest held row.
    newest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for r in sorted(
        closed, key=lambda r: observation_key(r["valid_from"], r["seen_at"], r["object"])
    ):
        if (r["subject"], r["relation"], r["object"]) in held:
            newest[(r["subject"], r["relation"])] = r
    # A value that ended on its own stays ended; a close stamps `invalid_at`.
    back = sorted(
        (r["subject"], r["relation"], r["object"], str(r["invalid_at"]), str(r["seen_at"]))
        for r in newest.values()
        if r["invalid_at"] is not None
    )
    if not back:
        return
    # Stated as of the relations' last close, which a replay reads back as it is: every
    # attempt dates its edge and triple markers alike.
    restated = {(s, r) for s, r, *_ in back}
    restored_at = max(
        r["invalid_at"]
        for r in rows
        if (r["subject"], r["relation"]) in restated and r["invalid_at"] is not None
    )
    # Named by the closes it answers, so a replay finds the markers an attempt wrote.
    # The row's own seen_at (microseconds) too: a marker closed again in the second of
    # the close it answered is another row, and needs a marker of its own.
    restore_id = uuid.uuid5(uuid.NAMESPACE_OID, repr((back, str(restored_at)))).hex
    facts = frozenset(t[:3] for t in back)
    # Only rows found holding a value stated again, each value's newest few: a value many
    # rows link would have each of them read again by the Undo's check.
    hashes: dict[str, str] = {}
    for f in facts:
        latest_first = sorted(held[f], key=lambda h: (h.updated_at, h.id), reverse=True)
        hashes.update({h.id: h.sha for h in latest_first[:_FALLBACK_ROWS]})
    await graph_undo.reopen_held_facts(
        input,
        sorted(hashes),
        ch=ch,
        restore_id=restore_id,
        restored_at=restored_at,
        hashes=hashes,
        facts=facts,
        adapter_name="row-fallback",
    )


# The rows a fallback asks to state each value again: one is enough, and a few more
# keep it from failing when the newest is revised meanwhile.
_FALLBACK_ROWS = 8  # not a setting: algorithm shape


async def write_derived_facts(
    input: MemoryBulkSetInput,
    results: list[_BulkSetResult],
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
) -> None:
    """Persist per-occurrence derived operands to loom.derived_facts.

    ONE row per occurrence — never deduped/summed at write time — so
    cross-session SUM / COUNT / max-min(occurred_at) stay correct at read
    time. Mirrors write_entity_triples: shared canonicalizer, fail-open,
    async insert. Empty `derived` produces zero rows.
    """
    from server.memory.app.entity_resolution import apply_entity_resolution
    from server.memory.app.extract import MAX_DERIVED_PER_FACT
    from server.memory.domain.graph_quality import canonical_entity_text
    from server.memory.domain.search_text import canonicalize_verb

    now = naive_utc_now()
    rows: list[dict[str, Any]] = []
    # Same cap the extractor applies when emitting `derived`; a lower cap here would silently
    # drop the operands past it.
    _MAX_DERIVED_PER_MEMORY = MAX_DERIVED_PER_FACT

    if entity_map is None:
        objs: list[str] = []
        for entry in input.entries:
            for d in list(getattr(entry, "derived", None) or []):
                if isinstance(d, dict) and d.get("object"):
                    objs.append(str(d["object"]))
        entity_map = await resolve_projection_entity_map(input, objs, ch=ch)

    for entry, result in zip(input.entries, results, strict=True):
        derived = list(getattr(entry, "derived", None) or [])
        if not derived:
            continue
        _conf = float(getattr(entry, "confidence", 1.0) or 1.0)
        kept = 0
        for d in derived:
            if kept >= _MAX_DERIVED_PER_MEMORY:
                break
            if not isinstance(d, dict):
                continue
            predicate = canonicalize_verb(str(d.get("predicate", "")).strip())
            obj = apply_entity_resolution(str(d.get("object", "")).strip(), entity_map)
            if len(predicate) < 2 or len(obj) < 1:
                continue
            # subject defaults to "user"; the extractor may override for
            # third-party occurrences.
            subject = apply_entity_resolution(str(d.get("subject") or "user").strip(), entity_map)
            nv = d.get("numeric_value")
            try:
                numeric_value = None if nv is None else float(nv)
            except (TypeError, ValueError):
                numeric_value = None
            occurred_at = parse_iso_dt(d.get("occurred_at") or "") or parse_iso_dt(
                getattr(entry, "temporal_anchor", "") or ""
            )
            rows.append(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "agent": input.agent or "",
                    "user_id": input.user_id or "",
                    "memory_key": result.memory_key,
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    # Fold category surface variance via the same canonicalizer
                    # the object gets, so surfaces don't split read-time COUNT/SUM.
                    "category": canonical_entity_text(str(d.get("category") or "").strip()),
                    "numeric_value": numeric_value,
                    "unit": str(d.get("unit") or "").strip(),
                    "occurred_at": occurred_at,
                    "session_id": input.session_id or "",
                    "confidence": _conf,
                    "created_at": now,
                }
            )
            kept += 1

    if not rows:
        return

    await ch.insert(
        "loom.derived_facts",
        rows,
        settings={"async_insert": 1, "wait_for_async_insert": 0},
    )


async def write_follow_plan(input: Any, plan: tfr.FollowPlan, *, ch: Any) -> tuple[int, int]:
    """Write the plan's successors, then close its triples: a close that lands after
    a failed insert would leave a replay nothing open to follow. Returns (closed,
    inserted), not counting a triple opened again after its close."""
    arrived = naive_utc_now()
    # Whole seconds, as `invalid_at` stores it: a reopen finds this close by it.
    now = arrived.replace(microsecond=0)
    # The plan read open triples by their raw text and so can miss a successor a
    # failed close left open ("Berlin, Germany" is `berlin germany`): look it up.
    already = await open_triples_among(
        ch, org=input.org, namespace=input.namespace, triples=[s.triple for s in plan.inserts]
    )
    inserted = await _insert_successors(input, plan.inserts, already, at=arrived, ch=ch)
    closed = await _close_planned(input, plan, now, ch=ch) if plan.closes else 0
    # After the closes, which date a closed value by its own row's replacement.
    await _repair_successors(input, plan.inserts, at=arrived, ch=ch)
    return closed, inserted


async def _close_planned(input: Any, plan: tfr.FollowPlan, now: datetime, *, ch: Any) -> int:
    """Close the plan's triples at `now` and open again those a live row holds.
    Returns how many stay closed."""

    # valid_to = when its own row was replaced (an erased winner's: now);
    # invalid_at = now.
    def _until(t: tfr.Triple) -> datetime:
        at = plan.closed_at.get(t)
        return at if isinstance(at, datetime) else now

    until = [(s, r, o, _until((s, r, o))) for s, r, o in plan.closes]
    closes: list[tuple[str, str, str, datetime, datetime | None]] = [(*c, now) for c in until]
    await close_triples(ch, org=input.org, namespace=input.namespace, closes=closes)
    try:
        held = await reopen_held(
            org=input.org, namespace=input.namespace, closes=until, closed_at=now, ch=ch
        )
    except Exception as e:  # noqa: BLE001 — the closes are in; the repair re-checks them
        log.warning("triples follow rows: re-check after the close failed (recorded): %s", e)
        await record_input_projection_failure(
            input,
            projection=TRIPLES_REOPEN,
            error=e,
            payload={
                "closes": [[s, r, o, at.isoformat()] for s, r, o, at in until],
                "closed_at": now.isoformat(),
            },
            source_operation="triples.follow_rows",
            source_resource_id=f"triples.follow_rows:{input.namespace}",
        )
        # Audited as closed; the repair logs what it opens again.
        held = frozenset()
    closed = [t for t in plan.closes if t not in held]
    try:
        from server.observability import audit as _audit

        for s, r, o in closed[:16]:
            await _audit.emit(
                event_type="triple.follows_row_supersession",
                org=input.org,
                namespace=input.namespace,
                actor=writer_actor() or "-",
                # The fact's text is memory content; the audit log keeps its hash.
                subject={"closed_sha256": sha256_value(f"{s} {r} {o}")},
            )
    except Exception as e:  # noqa: BLE001 — the closes are in; the trail is best effort
        log.debug("triples follow rows: audit skipped: %s", e)
    return len(closed)


async def _repair_successors(
    input: Any, successors: list[tfr.Successor], *, at: datetime, ch: Any
) -> None:
    """Repair the open successor triples as a write of them is repaired: an older
    value another live row still states closes when the successor starts. Read back
    rather than taken from the insert, so a replay that inserts none still repairs them."""
    if not successors:
        return
    rows = await ch.query_all(
        """
        SELECT subject, relation, object, valid_from, max(seen_at) AS seen_at
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND (subject, relation, object) IN {triples:Array(Tuple(String, String, String))}
          AND valid_to IS NULL AND invalid_at IS NULL AND adapter_name = 'row-supersession'
        GROUP BY subject, relation, object, valid_from
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "triples": sorted({s.triple for s in successors}),
        },
    )
    if rows:
        # The follow's closes just queued may still be pending: read them applied.
        await repair_intervals(input, [dict(r) for r in rows], ch=ch, at=at, on_fly=True)


async def reopen_held(
    *,
    org: str,
    namespace: str,
    closes: list[tuple[str, str, str, datetime]],
    closed_at: datetime,
    ch: Any,
) -> frozenset[tfr.Triple]:
    """Undo the close at `closed_at` (`closes` as `reopen_triples` takes them) for the
    triples a live row holds, and return those. A row written while a follow ran may
    state a triple it closed ("back to London"): the close also ends the open row that
    write inserted. Every writer lands a triple's row links first, so once the close is
    queued they are found here."""
    held = await graph_holders.stated_by_live_rows(org, namespace, [c[:3] for c in closes], ch=ch)
    await reopen_triples(
        ch,
        org=org,
        namespace=namespace,
        closes=[c for c in closes if c[:3] in held],
        closed_at=closed_at,
    )
    return held


async def _insert_successors(
    input: Any,
    successors: list[tfr.Successor],
    already: set[tfr.Triple],
    *,
    at: datetime,
    ch: Any,
) -> int:
    """Insert the successor triples not `already` open, after every successor's key
    and row links: a triple that landed without them would never get them, as a
    replay sees it open and skips it. Each triple is written once, though several
    winners may be linked to it; a repeated link is the same row. `at` is the follow's
    arrival, in microseconds. Returns how many triples were written."""
    now = at.replace(microsecond=0)
    new: dict[tfr.Triple, tfr.Successor] = {}
    for ins in successors:
        if ins.triple not in already:
            new.setdefault(ins.triple, ins)
    keyed = {(ins.triple, ins.memory_key): ins for ins in successors if ins.memory_key}
    adapter = "row-supersession"
    triples = [
        entity_triple_row(
            input.org,
            input.namespace,
            ins.subject,
            ins.relation,
            ins.object,
            provenance=ins.provenance,
            confidence=ins.confidence,
            adapter_name=adapter,
            last_seen=now,
            seen_at=at,
            valid_from=ins.valid_from if isinstance(ins.valid_from, datetime) else None,
        )
        for ins in new.values()
    ]
    provenance = [
        {
            "org": input.org,
            "namespace": input.namespace,
            "subject": ins.subject,
            "relation": ins.relation,
            "object": ins.object,
            "memory_key": ins.memory_key,
            "seen_at": now,
            "confidence": ins.confidence,
            "adapter_name": adapter,
        }
        for ins in keyed.values()
    ]
    links = [
        triple_row_link(
            input.org,
            input.namespace,
            ins.memory_id,
            ins.content_sha256,
            ins.triple,
            user_id=ins.user_id,
            inferred=True,
        )
        for ins in successors
        if ins.memory_id
    ]
    settings = async_insert_settings()
    if provenance:
        await ch.insert("loom.entity_triple_memories", provenance, settings=settings)
    if links:
        await ch.insert("loom.entity_triple_rows", links, settings=settings)
    if triples:
        await ch.insert("loom.entity_triples", triples, settings=settings)
    return len(triples)
