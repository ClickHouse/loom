"""The co-occurrence edges a point write or a revision projects, and the drop of a
'high' row's links to pairs it does not declare."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from server.core.ch import get_client
from server.core.ch_params import chunks
from server.core.repo_base import query_all_sliced
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
from server.memory.app import graph_holders, projections, set_entities
from server.memory.app.edge_repair import end_overtaken_edges
from server.memory.app.projection_errors import (
    bulk_projection_payload,
    record_input_projection_failure,
)
from server.memory.app.projection_failures import (
    EDGES_DROP_UNDECLARED,
    EDGES_RESTORE,
    record_projection_failure,
)
from server.memory.domain.temporal_sql import EDGE_HOLDER_SQL, edge_holder_sql
from server.memory.store.rows import version_time

log = logging.getLogger(__name__)


async def project_memory_set_entities(
    input: Any,  # MemorySetInput
    *,
    memory_key: str,
    value: Any,
    ch: Any = None,
    wait_visible: bool = False,
    memory_id: str,
    ents: list[str] | None = None,
    content_sha256: str,
    high: bool = False,
) -> int:
    """Fail-open entity projection for a single `memory.set`.

    Derives candidate entities from `value`, then reuses write_entity_edges
    for the same pairing / cap / window / insert path as the bulk write.
    Returns the count of distinct candidates projected (0 when <2).
    `wait_visible` is for the projection that runs beside the response: its
    edges are readable once it returns. The edges are linked to the row `memory_id`
    and its value `content_sha256` (`entity_edge_rows`). `ents`: the caller's own
    `set_entities_for_projection(input, value)`, not mined again. The projection is
    fenced on that value: it writes only while the row still holds it, and ends its
    edges if the row changed meanwhile (`end_overtaken_edges`), which can find them
    only once they are flushed (`wait_visible`).
    """
    from server.observability.otel import traced

    if ents is None:
        with traced("entities.extract"):
            ents = set_entities.set_entities_for_projection(input, value)
    if len(ents) < 2:
        # <2 entities = no co-occurrence pair. Lone nodes come from the
        # bulk/worker path; edges are what point-writes were missing.
        return 0

    with traced("entities.build_input", entities=len(ents)):
        bulk_like = MemoryBulkSetInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            entries=[
                _BulkSetEntry(
                    memory_key=memory_key,
                    value=value,
                    memory_type=getattr(input, "memory_type", "semantic"),
                    entities=ents,
                    valid_at=getattr(input, "valid_at", None),
                )
            ],
        )
    result = _BulkSetResult(memory_key=memory_key, version=0, memory_id=memory_id)
    ch = ch or get_client()
    # A revision that ran first closed what it dropped already: this value is not current.
    # A row made 'high' holds these edges only if it still names every entity.
    key = (str(input.agent), str(input.user_id), str(input.session_id), memory_key)
    if not await graph_holders.rows_holding(
        input.org,
        input.namespace,
        [(memory_id, content_sha256)],
        ch=ch,
        holder=edge_holder_sql(ents),
        key=key,
    ):
        return 0
    with traced("entities.write_edges", entities=len(ents)):
        await projections.write_entity_edges(bulk_like, [result], ch=ch, wait_visible=wait_visible)
    # Revised or retired while the edges were written: its close may have run before
    # they landed, so end the ones no live row names.
    version = (memory_id, memory_key, content_sha256)
    high_versions = {(memory_id, content_sha256)} if high else set()
    await end_overtaken_edges(
        input, [version], op="memory.set", ch=ch, entities=ents, high=high_versions
    )
    return len(ents)


def owes_undeclared_drop(previous: Any, declared: Sequence[str]) -> bool:
    """Whether a write that leaves its row 'high' over `previous` (the row it replaced)
    must drop edges: a row that was not high had its text mined, and a high one keeps
    the edges of the entities it named, so only the ones it no longer names go."""
    if str(getattr(previous, "sensitivity", "") or "") != "high":
        return True
    return not set(declared) >= set(
        set_entities.declared_entities(getattr(previous, "entities", None))
    )


async def drop_undeclared_links(
    input: Any, versions: Mapping[str, int], *, ch: Any, op: str
) -> None:
    """Rows a write left 'high' (`versions`: memory_id -> the version that write gave the
    row) keep only the edges of the entities on the row now: their links to any other
    pair go, and those edges end when that write landed unless another row names them,
    on a replay too (a later write that kept the row high does not move the end). A row
    no longer 'high' is skipped: a later plain write's links are its own. One still
    'high' is read as it is now, so a later write that kept it high (a pin, a lock, a
    relabel) neither skips the drop nor brings back what it dropped."""
    keep = await _rows_still_high(input, sorted(versions), ch=ch)
    if keep:
        await _drop_links_of(input, keep, versions, ch=ch, op=op)


async def _rows_still_high(input: Any, ids: list[str], *, ch: Any) -> dict[str, set[str]]:
    """Of the rows `ids`, those 'high' now, each with the entities whose edges it keeps."""
    current = await query_all_sliced(
        ch,
        """
        SELECT toString(memory_id) AS id, entities FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_id IN {ids:Array(UUID)} AND sensitivity = 'high'
        SETTINGS select_sequential_consistency = 1
        """,
        {"org": input.org, "ns": input.namespace, "ids": ids},
        "ids",
    )
    return {str(r["id"]): set(set_entities.declared_entities(r["entities"])) for r in current}


async def _drop_links_of(
    input: Any, keep: Mapping[str, set[str]], versions: Mapping[str, int], *, ch: Any, op: str
) -> None:
    """`drop_undeclared_links` for the rows still 'high' (`keep`: memory_id -> the
    entities whose edges it keeps). The links read here go, and those a write since then
    holds come back (`_restore_kept_links`; a failed restore is recorded as
    EDGES_RESTORE, which the projection repair replays)."""
    params: dict[str, Any] = {"org": input.org, "ns": input.namespace, "ids": sorted(keep)}
    links = await query_all_sliced(
        ch,
        """
        SELECT DISTINCT toString(memory_id) AS id, toString(content_sha256) AS sha,
               entity_a, entity_b
        FROM loom.entity_edge_rows
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_id IN {ids:Array(UUID)}
        SETTINGS select_sequential_consistency = 1
        """,
        params,
        "ids",
    )
    drop = sorted(
        (str(r["id"]), str(r["sha"]), str(r["entity_a"]), str(r["entity_b"]))
        for r in links
        if not {str(r["entity_a"]), str(r["entity_b"])} <= keep[str(r["id"])]
    )
    if not drop:
        return
    # A pair another of these rows keeps stays open; it holds through that row's link.
    kept = {
        (str(r["entity_a"]), str(r["entity_b"]))
        for r in links
        if {str(r["entity_a"]), str(r["entity_b"])} <= keep[str(r["id"])]
    }
    # Each edge ends when the first write that dropped it landed, a replay included.
    ends: dict[tuple[str, str], datetime] = {}
    for memory_id, _, a, b in drop:
        at = version_time(versions[memory_id])
        ends[(a, b)] = min(ends.get((a, b), at), at)
    # The edges end before their links go, the values read holding none, so a failure
    # between leaves the links for the replay to find the edges by.
    close_ids = await projections.end_edges(
        ch,
        org=input.org,
        namespace=input.namespace,
        ends=[(a, b, at) for (a, b), at in sorted(ends.items()) if (a, b) not in kept],
        open_only=True,
        skip_versions=sorted({(memory_id, sha) for memory_id, sha, *_ in drop}),
    )
    # Read again right before the DELETE: a row re-set plain since the first read keeps
    # its links, and one re-set high naming more entities keeps those. The restore (or
    # its replay) covers a write landing after this read. Parameters only: a mutation
    # with a subquery is refused on a replicated table.
    high = await _rows_still_high(input, sorted({memory_id for memory_id, *_ in drop}), ch=ch)
    deleted = [
        (memory_id, sha, a, b)
        for memory_id, sha, a, b in drop
        if memory_id in high and not {a, b} <= high[memory_id]
    ]
    for part in chunks(deleted):
        await ch.execute(
            """
            DELETE FROM loom.entity_edge_rows
            WHERE org = {org:String} AND namespace = {ns:String}
              AND memory_id IN {ids:Array(UUID)}
              AND (toString(memory_id), toString(content_sha256), entity_a, entity_b)
                  IN {links:Array(Tuple(String, String, String, String))}
            """,
            params={**params, "ids": sorted({memory_id for memory_id, *_ in part}), "links": part},
        )
    try:
        await _restore_kept_links(input, drop, set(deleted), close_ids, ch=ch)
    except Exception as e:  # noqa: BLE001 — recorded; the projection repair restores them
        log.warning(
            "%s: restore after dropping %d links failed (recorded): %s", op, len(deleted), e
        )
        await record_projection_failure(
            org=input.org,
            namespace=input.namespace,
            agent=str(getattr(input, "agent", "") or ""),
            projection=EDGES_RESTORE,
            source_operation=op,
            error=e,
            payload=restore_payload(drop, set(deleted), close_ids),
            ch=ch,
            user_id=str(getattr(input, "user_id", "") or ""),
        )


def restore_payload(
    drop: list[tuple[str, str, str, str]],
    deleted: set[tuple[str, str, str, str]],
    close_ids: list[str],
) -> dict[str, Any]:
    """An EDGES_RESTORE payload: the arguments of the `_restore_kept_links` it owes, each
    link of the drop with whether the DELETE took it."""
    return {
        "links": [[*link, link in deleted] for link in drop],
        "close_ids": list(close_ids),
    }


async def restore_kept_links(payload: Mapping[str, Any], input: Any, *, ch: Any, op: str) -> None:
    """Replay the restore an EDGES_RESTORE `payload` records, against the rows as they are
    now. A row that holds the same value but was made 'high' again since gets back the
    links the DELETE took, and its drop runs again: that drop found none of them, so it
    ended none of their edges."""
    links = [
        (str(m), str(sha), str(a), str(b), bool(gone)) for m, sha, a, b, gone in payload["links"]
    ]
    drop = [link[:4] for link in links]
    deleted = {link[:4] for link in links if link[4]}
    remade = await _restore_kept_links(
        input, drop, deleted, [str(c) for c in payload["close_ids"]], ch=ch, relink_high=True
    )
    if remade:
        await drop_undeclared_links(input, remade, ch=ch, op=op)


async def _restore_kept_links(
    input: Any,
    drop: list[tuple[str, str, str, str]],
    deleted: set[tuple[str, str, str, str]],
    close_ids: list[str],
    *,
    ch: Any,
    relink_high: bool = False,
) -> dict[str, int]:
    """After the DELETE, for the links of `drop` whose row holds them now (a write landed
    since the drop read them): write back those `deleted` took, and undo the ends this
    drop's close (`close_ids`) stamped on their pairs. With `relink_high`, a 'high' row
    holding the same value also gets back the `deleted` links to pairs it does not name;
    returns those rows (memory_id -> version), whose drop is owed again."""
    rows = await query_all_sliced(
        ch,
        f"""
        SELECT toString(memory_id) AS id, user_id, toString(content_sha256) AS sha,
               sensitivity, entities, version
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_id IN {{ids:Array(UUID)}} AND {EDGE_HOLDER_SQL}
        SETTINGS select_sequential_consistency = 1
        """,
        {
            "org": input.org,
            "ns": input.namespace,
            "ids": sorted({memory_id for memory_id, *_ in drop}),
        },
        "ids",
    )
    current = {str(r["id"]): r for r in rows}
    links = []
    pairs = set()
    remade: dict[str, int] = {}
    for memory_id, sha, a, b in drop:
        row = current.get(memory_id)
        if row is None or str(row["sha"]) != sha:
            continue  # gone, or a later value: that write's own links and edges
        named = set(set_entities.declared_entities(row["entities"]))
        if row["sensitivity"] == "high" and not {a, b} <= named:
            if not relink_high or (memory_id, sha, a, b) not in deleted:
                continue
            remade[memory_id] = int(row["version"])
        else:
            pairs.add((a, b))
        if (memory_id, sha, a, b) in deleted:
            writer = SimpleNamespace(
                org=input.org, namespace=input.namespace, user_id=row["user_id"]
            )
            links.append(projections.edge_row_link(writer, memory_id, sha, (a, b)))
    if links:
        await ch.insert("loom.entity_edge_rows", links)
    await projections.reopen_closed_by(
        ch, org=input.org, namespace=input.namespace, close_ids=close_ids, pairs=sorted(pairs)
    )
    return remade


def drop_payload(versions: Mapping[str, int]) -> dict[str, Any]:
    """An EDGES_DROP_UNDECLARED payload: for each row, the version of the write that owes
    the drop, which dates the edges' end."""
    return {"versions": {memory_id: int(version) for memory_id, version in versions.items()}}


async def drop_undeclared_edges(
    input: Any, versions: Mapping[str, int], *, ch: Any, op: str
) -> None:
    """`drop_undeclared_links`, fail-open: a failure is recorded for the projection repair
    (`EDGES_DROP_UNDECLARED`), which drops them again."""
    try:
        await drop_undeclared_links(input, versions, ch=ch, op=op)
    except Exception as e:  # noqa: BLE001 — recorded; the projection repair drops them
        log.warning(
            "%s: undeclared edges of %d rows not dropped (recorded): %s", op, len(versions), e
        )
        await _record_drop(input, versions, op=op, error=e, ch=ch)


async def _record_drop(
    input: Any, versions: Mapping[str, int], *, op: str, error: Any, ch: Any = None
) -> str:
    """An EDGES_DROP_UNDECLARED failure for the rows `versions`: the drop the projection
    repair owes. Returns its id."""
    return await record_projection_failure(
        org=input.org,
        namespace=input.namespace,
        agent=str(getattr(input, "agent", "") or ""),
        projection=EDGES_DROP_UNDECLARED,
        source_operation=op,
        error=error,
        payload=drop_payload(versions),
        ch=ch,
        user_id=str(getattr(input, "user_id", "") or ""),
    )


async def drop_undeclared_edges_beside(input: Any, versions: Mapping[str, int], *, op: str) -> None:
    """`drop_undeclared_links` after the response, for the rows a write made 'high' or
    whose declared entities it narrowed (`versions`: memory_id -> the version the write
    gave it; callers pick them by `owes_undeclared_drop`). Owed first, as a retirement
    is: one EDGES_DROP_UNDECLARED intent for the whole write goes in before the drop is
    scheduled and is resolved in the next batched mark once the drop lands, so a stop
    between them leaves it to the repair. Scheduled after the write's own declared
    projection returned, so it runs after its fence."""
    from server.memory.app import after_write, intent_marks

    if not versions:
        return
    try:
        intent = await _record_drop(
            input, versions, op=op, error="rows made high: undeclared edges to drop"
        )
    except Exception as e:  # noqa: BLE001 — the drop still runs; a failure is recorded then
        log.warning("%s: drop intent not written for %d rows: %s", op, len(versions), e)
        intent = ""

    async def drop() -> None:
        if not intent:
            await drop_undeclared_edges(input, versions, ch=get_client(), op=op)
            return
        try:
            await drop_undeclared_links(input, versions, ch=get_client(), op=op)
        except Exception as e:  # noqa: BLE001 — the intent stays; the repair drops them
            log.warning(
                "%s: undeclared edges of %d rows not dropped (owed): %s", op, len(versions), e
            )
            return
        intent_marks.resolve_soon(input.org, [intent])

    after_write.schedule(drop(), name=f"{op}.drop-undeclared", scope=(input.org, input.namespace))


async def project_revised_edges(row: Mapping[str, Any], value: Any, *, op: str, ch: Any) -> None:
    """The co-occurrence edges a revision's new `value` names, linked to its `row` (the
    `loom.memory` row holding it), as `memory.set` projects them. Written before the
    close of what the revision dropped, so an edge its new value still names stays
    open. A row that may not hold graph facts (`holds_graph_facts`: retired, 'high',
    ...) projects none. Fail-open: a failed write is recorded for the projection
    repair to replay."""
    if not graph_holders.holds_graph_facts(row):
        return
    scope = SimpleNamespace(tags=row.get("tags"), source_trace=row.get("source_trace", ""))
    # From the new value only: the row's stored `entities` describe the value it replaced.
    ents = set_entities.set_entities_for_projection(scope, value)
    if len(ents) < 2:
        return
    write, results = projections.as_bulk_write(row, value, [])
    entry = write.entries[0].model_copy(update={"entities": ents})
    write = write.model_copy(update={"entries": [entry]})
    try:
        await projections.write_entity_edges(write, results, ch=ch, wait_visible=True)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("%s: edges projection failed (recorded for repair): %s", op, e)
        await record_input_projection_failure(
            write,
            projection="entity_edges",
            error=e,
            memory_keys=[results[0].memory_key],
            payload=bulk_projection_payload(write, results, error=e),
            source_operation=op,
            source_resource_id=f"{op}:{results[0].memory_id}",
        )
        return
    version = (results[0].memory_id, entry.memory_key, projections.value_sha256(entry))
    await end_overtaken_edges(write, [version], op=op, ch=ch, entities=ents)
