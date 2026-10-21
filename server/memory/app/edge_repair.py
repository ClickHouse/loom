"""Ending the co-occurrence edges of values a row no longer holds: after a late
projection, and in the projection repair."""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from server.core.datetimes import naive_utc_now, to_naive_utc
from server.memory.app import graph_holders, projections
from server.memory.app.projection_failures import (
    EDGES_CLOSE,
    insert_projection_failures,
    projection_failure_row,
)
from server.memory.domain.temporal_sql import (
    EDGE_HOLDS_VALUE_SQL,
    HOLDS_VALUE_SQL,
    edge_holder_sql,
)

log = logging.getLogger(__name__)


# Values of one row a value-less close repair reads a page at a time, and the pages one
# repair reads at most; past them it raises, and the failure stays for the next pass.
_VALUES_PAGE = 100  # not a setting: the page size of one read
_VALUES_PAGES = 20

# The repair's reads follow its own closes (async ALTER UPDATEs): one still queued must
# read as applied, or a value it just closed reads as open.
_REPAIR_READ_SETTINGS = "SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1"


async def close_overtaken_values(org: str, namespace: str, memory_id: str, *, ch: Any) -> None:
    """Ends the pairs of the row's older values that can close (open, held by no live
    row, not named by the row now), one `end_edges` call per page. Each value read
    closes, so a pass that runs out of pages raises and the next pass reads what is
    left."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "id": memory_id}
    named = await ch.query_all(
        f"""
        SELECT entities FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND memory_id = {{id:UUID}}
          AND {EDGE_HOLDS_VALUE_SQL} AND sensitivity = 'high'
        {_REPAIR_READ_SETTINGS}
        """,
        params=params,
    )
    keep = {str(e) for row in named for e in row["entities"]}
    params.update(
        keep=sorted(keep),
        page=_VALUES_PAGE,
        skip_versions=[],
        edge_links=projections.EDGE_ROW_LINKS_MIGRATION,
    )
    held = projections.held_pairs_ctes("(SELECT entity_a, entity_b FROM row_pairs)")
    after = ""
    for _ in range(_VALUES_PAGES):
        values = await ch.query_all(
            f"""
            WITH row_pairs AS (
                SELECT DISTINCT entity_a, entity_b FROM loom.entity_edge_rows
                WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  AND memory_id = {{id:UUID}}
            ),
            {held}
            SELECT toString(content_sha256) AS sha,
                   groupUniqArray((entity_a, entity_b)) AS pairs
            FROM loom.entity_edge_rows
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND memory_id = {{id:UUID}}
              AND content_sha256 > {{after:String}}
              AND content_sha256 != (
                  SELECT any(content_sha256) FROM loom.memory FINAL
                  WHERE org = {{org:String}} AND namespace = {{ns:String}}
                    AND memory_id = {{id:UUID}} AND {EDGE_HOLDS_VALUE_SQL})
              AND NOT (has({{keep:Array(String)}}, entity_a)
                       AND has({{keep:Array(String)}}, entity_b))
              AND (entity_a, entity_b) NOT IN (SELECT entity_a, entity_b FROM held)
              AND (entity_a, entity_b) IN (
                  SELECT entity_a, entity_b FROM loom.entity_edges
                  WHERE org = {{org:String}} AND namespace = {{ns:String}} AND valid_to IS NULL
                    AND (entity_a, entity_b) IN (SELECT entity_a, entity_b FROM row_pairs))
            GROUP BY content_sha256
            ORDER BY sha
            LIMIT {{page:UInt32}}
            {_REPAIR_READ_SETTINGS}
            """,
            params={**params, "after": after},
        )
        await _close_page(org, namespace, memory_id, values, ch=ch)
        if len(values) < _VALUES_PAGE:
            return
        # The cursor keeps a pass from reading again a value whose close a row written
        # meanwhile undid.
        after = str(values[-1]["sha"])
    raise RuntimeError(f"more than {_VALUES_PAGE * _VALUES_PAGES} values of row {memory_id}")


async def _close_page(
    org: str, namespace: str, memory_id: str, values: list[dict[str, Any]], *, ch: Any
) -> None:
    """One `end_edges` for a page of `values` (sha, pairs): each pair ends at the
    earliest end of the values linking it."""
    ends: dict[tuple[str, str], datetime] = {}
    for value in values:
        pairs = [(str(a), str(b)) for a, b in value["pairs"]]
        ended_at = await _value_ended(org, namespace, memory_id, str(value["sha"]), pairs, ch=ch)
        if ended_at is None:
            continue  # the row holds the value again
        for pair in pairs:
            ends[pair] = min(ends.get(pair, ended_at), ended_at)
    if ends:
        await projections.end_edges(
            ch,
            org=org,
            namespace=namespace,
            ends=[(a, b, at) for (a, b), at in sorted(ends.items())],
            open_only=True,
        )


async def _value_ended(
    org: str,
    namespace: str,
    memory_id: str,
    content_sha256: str,
    pairs: Sequence[tuple[str, str]],
    *,
    ch: Any,
) -> datetime | None:
    """When the row stopped holding the value linking `pairs`, or None when it holds it
    again (`edge_holder_sql` of their entities)."""
    holder = edge_holder_sql(sorted({e for pair in pairs for e in pair}))
    version = [(memory_id, content_sha256)]
    if await graph_holders.rows_holding(org, namespace, version, ch=ch, holder=holder):
        return None
    return await value_ended_at(org, namespace, memory_id, content_sha256, ch=ch, holder=holder)


async def close_late_edges(
    org: str,
    namespace: str,
    memory_id: str,
    content_sha256: str,
    *,
    ended_at: datetime | None = None,
    entities: Sequence[str] = (),
    keep: Collection[str] = (),
    ch: Any,
) -> None:
    """The projection repair of a late projection whose cleanup failed: end the
    edges of that value at `ended_at` (when the row stopped holding it; read again
    when not given) unless the row holds it again (`edge_holder_sql` of the projected
    `entities`). The edges between two of the entities `keep` stay. Safe to run twice."""
    holder = edge_holder_sql(entities)
    version = [(memory_id, content_sha256)]
    if await graph_holders.rows_holding(org, namespace, version, ch=ch, holder=holder):
        return
    if ended_at is None:
        ended_at = await value_ended_at(
            org, namespace, memory_id, content_sha256, ch=ch, holder=holder
        )
    await _end_edges_of(org, namespace, memory_id, content_sha256, ended_at, keep=keep, ch=ch)


async def end_overtaken_edges(
    input: Any,
    versions: list[tuple[str, str, str]],
    *,
    op: str,
    ch: Any,
    entities: Sequence[str] = (),
    high: Collection[tuple[str, str]] = (),
) -> None:
    """After revisions wrote their edges: another write since may have revised or
    retired one of these `versions` (memory_id, memory_key, content_sha256) before
    they landed, so end its edges at the moment the row stopped holding it. One read
    asks which rows still hold theirs (`edge_holder_sql` of the projected `entities`).
    A failure is recorded for the projection repair; `high`: the (memory_id,
    content_sha256) versions of 'high' rows, recorded without their content."""
    holder = edge_holder_sql(entities)
    held: set[tuple[str, str]] = set()
    try:
        holding = [(memory_id, sha) for memory_id, _, sha in versions]
        held = await graph_holders.versions_held(
            input.org, input.namespace, holding, ch=ch, holder=holder
        )
    except Exception as e:  # noqa: BLE001 — recorded; the projection repair ends them
        closes = [
            EdgeClose(mid, key, sha, list(entities), high=(mid, sha) in high)
            for mid, key, sha in versions
        ]
        await record_edges_closes(input, closes, e, op=op, ch=ch)
        return
    for memory_id, memory_key, sha in versions:
        if (memory_id, sha) in held:
            continue
        ended_at = None
        try:
            ended_at = await value_ended_at(
                input.org, input.namespace, memory_id, sha, ch=ch, holder=holder
            )
            await _end_edges_of(input.org, input.namespace, memory_id, sha, ended_at, ch=ch)
        except Exception as e:  # noqa: BLE001 — recorded; the projection repair ends them
            close = EdgeClose(
                memory_id, memory_key, sha, list(entities), ended_at, (memory_id, sha) in high
            )
            await record_edges_closes(input, [close], e, op=op, ch=ch)


class EdgeClose(NamedTuple):
    """A value's edge close left to the projection repair: the row's version, the
    entities its edges paired, when the row stopped holding the value (if read), and
    whether the version is a 'high' row's."""

    memory_id: str
    memory_key: str
    sha: str
    entities: list[str]
    ended_at: datetime | None = None
    high: bool = False


async def record_edges_closes(
    input: Any, closes: list[EdgeClose], error: Exception, *, op: str, ch: Any
) -> None:
    """Record failed edge closes for the projection repair in one insert, each with the
    value's end when it was read: the repair ends the edges at the same moment.
    Fail-open, as the write it describes has landed. A high row's close is recorded
    without its content_sha256 or end: no failure payload names a high row's content,
    and the repair closes whichever of the row's values is no longer current, each at
    its own end."""
    rows = []
    for close in closes:
        payload: dict[str, Any] = {"memory_id": close.memory_id}
        if not close.high:
            payload["content_sha256"] = close.sha
        if close.entities:
            payload["entities"] = close.entities
        if close.ended_at is not None and not close.high:
            payload["ended_at"] = close.ended_at.isoformat()
        rows.append(
            projection_failure_row(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                projection=EDGES_CLOSE,
                source_operation=op,
                memory_keys=[close.memory_key],
                error=error,
                payload=payload,
                user_id=str(getattr(input, "user_id", "") or ""),
            )
        )
    try:
        await insert_projection_failures(rows, ch=ch)
    except Exception as e:  # noqa: BLE001 — must not fail the write it describes
        log.warning("%s: %d edge closes not recorded: %s", op, len(rows), e)


async def value_ended_at(
    org: str,
    namespace: str,
    memory_id: str,
    content_sha256: str,
    *,
    ch: Any,
    holder: tuple[str, dict[str, Any]] = (HOLDS_VALUE_SQL, {}),
) -> datetime:
    """When the row last stopped holding the value `content_sha256`: the first version
    after the last one that held it (a revision, a retirement, a delete), read from the
    version history, dated by what that version stamped. Now when the history has none."""
    row = await ch.query_one(
        f"""
        -- holder: the last version holding the value, and its invalid_at.
        WITH (
            SELECT (maxOrNull(version), argMax(tuple(invalid_at), version).1)
            FROM loom.memory_versions
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id = {{id:UUID}} AND content_sha256 = {{sha:String}}
              AND {holder[0]}
        ) AS holder
        -- A tombstone or retirement can keep the row's updated_at. A tombstoned_at here is
        -- fresh (the holders had none); an invalid_at is fresh when the holder's differs,
        -- and may be back-dated (a value update retires as of the newer row's valid_at).
        -- A retirement that stamps neither is dated by its version, the wall-clock
        -- nanoseconds it was written at (`next_version`).
        SELECT argMinOrNull(
            coalesce(
                tombstoned_at,
                if(invalid_at IS DISTINCT FROM holder.2, invalid_at, NULL),
                greatest(updated_at, toDateTime(fromUnixTimestamp64Nano(toInt64(version))))),
            version) AS ended
        FROM loom.memory_versions
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND memory_id = {{id:UUID}}
          AND version > holder.1
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "id": memory_id, "sha": content_sha256, **holder[1]},
    )
    ended = row.get("ended") if row else None
    return to_naive_utc(ended) if isinstance(ended, datetime) else naive_utc_now()


async def _end_edges_of(
    org: str,
    namespace: str,
    memory_id: str,
    content_sha256: str,
    ended_at: datetime,
    *,
    keep: Collection[str] = (),
    ch: Any,
) -> None:
    """End at `ended_at` the open edges linked to the row's value `content_sha256` that
    no live row names (`end_edges` keeps those), but for the edges between two of the
    entities `keep`. An edge row another row closed since keeps its end."""
    pairs = await ch.query_all(
        """
        SELECT DISTINCT entity_a, entity_b FROM loom.entity_edge_rows
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_id = {id:UUID} AND content_sha256 = {sha:String}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "id": memory_id, "sha": content_sha256},
    )
    kept = set(keep)
    ends = [
        (str(p["entity_a"]), str(p["entity_b"]), ended_at)
        for p in pairs
        if not {str(p["entity_a"]), str(p["entity_b"])} <= kept
    ]
    await projections.end_edges(ch, org=org, namespace=namespace, ends=ends, open_only=True)
