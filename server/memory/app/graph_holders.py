"""Which rows hold a value, an edge or a fact now: the reads every graph write and
repair fences on. They read `loom.memory`. Under the Postgres authority a first
`memory.set` answers at its commit, so a fence of that write (its entity projection)
first waits for its publication, up to its lease (`pg_batch.own_write_published`); every
other row is read as ClickHouse holds it, as on a write whose publication is pending."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, NamedTuple

from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.core.repo_base import query_all_sliced
from server.memory.domain import triples_follow_rows as tfr
from server.memory.domain.temporal_sql import (
    EDGE_HOLDS_VALUE_SQL,
    GRAPH_HOLDER_SQL,
    LIVE_STATUSES,
)
from server.memory.store.pg_batch import own_write_published

log = logging.getLogger(__name__)


def holds_graph_facts(row: Mapping[str, Any]) -> bool:
    """`GRAPH_HOLDER_SQL` for a row in hand: live, not deleted, not ended, not 'high'."""
    if row.get("status") not in LIVE_STATUSES or row.get("tombstoned_at") is not None:
        return False
    ends = [parse_iso_dt(row.get(c)) for c in ("expires_at", "valid_to")]
    if any(end is not None and end <= naive_utc_now() for end in ends):
        return False
    return row.get("sensitivity") != "high"


def as_text(raw: Any) -> str:
    """The prose a stored `value` carries, decoded: names are matched in it."""
    from server.memory.domain.triple_provenance import carrier_text
    from server.memory.store.rows import parse_memory_value

    return carrier_text(parse_memory_value(raw))


async def carried_by(
    org: str,
    namespace: str,
    heads: list[dict[str, Any]],
    stated_now: dict[str, frozenset[tfr.Triple]],
    hashes: dict[str, dict[tfr.Triple, set[str]]],
    *,
    ch: Any,
) -> dict[str, frozenset[tfr.Triple]]:
    """The successors each live row holds now (`tfr.carried`), by row id.
    `stated_now`: what the extractor stated for each row's current value; `hashes`:
    the hashes of the values each successor was written for. Those values are read
    from `loom.memory_versions`, the latest unredacted version with that hash;
    a hash none still holds (erased or redacted) holds nothing."""
    from server.identity.app.namespace_ontology import load_relation_cardinality

    current = {str(h["id"]): str(h.get("content_sha256") or "") for h in heads}
    old = sorted(
        {
            (mid, sha)
            for mid in current
            for shas in hashes.get(mid, {}).values()
            for sha in shas
            if sha != current[mid]
        }
    )
    old_text: dict[tuple[str, str], str] = {}
    if old:
        versions = await ch.query_all(
            """
            SELECT toString(memory_id) AS id, toString(content_sha256) AS sha,
                   argMax(value, version) AS value
            FROM loom.memory_versions
            WHERE org = {org:String} AND namespace = {ns:String}
              AND (memory_id, content_sha256) IN {keys:Array(Tuple(UUID, String))}
              AND redacted_at IS NULL
            GROUP BY id, sha
            SETTINGS select_sequential_consistency = 1
            """,
            params={"org": org, "ns": namespace, "keys": old},
        )
        old_text = {(str(v["id"]), str(v["sha"])): as_text(v["value"]) for v in versions}
    declared = await load_relation_cardinality(org, namespace) if old else {}
    return {
        mid: tfr.carried(
            as_text(h.get("value")),
            current[mid],
            stated_now.get(mid, frozenset()),
            {
                t: {sha: old_text.get((mid, sha), "") for sha in shas}
                for t, shas in hashes.get(mid, {}).items()
            },
            declared,
        )
        for h in heads
        if (mid := str(h["id"])) in hashes
    }


async def rows_holding(
    org: str,
    namespace: str,
    versions: list[tuple[str, str]],
    *,
    ch: Any,
    holder: tuple[str, dict[str, Any]] = (GRAPH_HOLDER_SQL, {}),
    key: tuple[str, str, str, str] | None = None,
) -> set[str]:
    """Of `versions`, (memory_id, content_sha256) pairs, the ids of the rows whose
    current value is still that version and that pass `holder` (a test and its
    parameters): by default GRAPH_HOLDER_SQL (may hold graph facts), or a weaker test
    such as HOLDS_VALUE_SQL (not deleted, retired or 'high') or one `edge_holder_sql`
    builds. `key` as `versions_held`."""
    held = await versions_held(org, namespace, versions, ch=ch, holder=holder, key=key)
    return {memory_id for memory_id, _ in held}


async def versions_held(
    org: str,
    namespace: str,
    versions: list[tuple[str, str]],
    *,
    ch: Any,
    holder: tuple[str, dict[str, Any]] = (GRAPH_HOLDER_SQL, {}),
    key: tuple[str, str, str, str] | None = None,
) -> set[tuple[str, str]]:
    """`rows_holding`, answered by version: the pairs still current. A row revised
    twice in one write is asked about both values and holds only one. `key` (agent,
    user_id, session_id, memory_key), when every version is of that one row, narrows
    the read to it through the table's sort key, as `memory.get_by_id` does."""
    if not versions:
        return set()
    await own_write_published()
    holder_sql, holder_params = holder
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "versions": sorted(set(versions)),
        **holder_params,
    }
    prewhere = ""
    if key is not None:
        prewhere = """PREWHERE agent = {agent:String} AND user_id = {uid:String}
          AND session_id = {sid:String} AND memory_key = {key:String}"""
        params.update(zip(("agent", "uid", "sid", "key"), key, strict=True))
    found = await ch.query_all(
        f"""
        SELECT DISTINCT toString(memory_id) AS id, toString(content_sha256) AS sha
        FROM loom.memory FINAL
        {prewhere}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND (memory_id, content_sha256) IN {{versions:Array(Tuple(UUID, String))}}
          AND {holder_sql}
        SETTINGS select_sequential_consistency = 1
        """,
        params=params,
    )
    return {(str(r["id"]), str(r["sha"])) for r in found}


async def edge_holders(
    org: str, namespace: str, versions: list[tuple[str, str]], *, ch: Any
) -> dict[tuple[str, str], tuple[str, set[str]]]:
    """Of `versions`, (memory_id, content_sha256) pairs, the ones still current and
    holding their value, a 'high' row included (EDGE_HOLDS_VALUE_SQL): each with the
    row's sensitivity and entities, so a caller decides per edge in one read whether
    a high row still names its entities."""
    if not versions:
        return {}
    await own_write_published()
    found = await ch.query_all(
        f"""
        SELECT DISTINCT toString(memory_id) AS id, toString(content_sha256) AS sha,
               toString(sensitivity) AS sensitivity, entities
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND (memory_id, content_sha256) IN {{versions:Array(Tuple(UUID, String))}}
          AND {EDGE_HOLDS_VALUE_SQL}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "versions": sorted(set(versions))},
    )
    return {
        (str(r["id"]), str(r["sha"])): (str(r["sensitivity"]), {str(e) for e in r["entities"]})
        for r in found
    }


class Holder(NamedTuple):
    """A live row holding a fact: its id, current `content_sha256` and last write."""

    id: str
    sha: str
    updated_at: datetime


async def stated_by_live_rows(
    org: str,
    namespace: str,
    triples: list[tfr.Triple],
    *,
    ch: Any,
    rows: list[str] | None = None,
) -> frozenset[tfr.Triple]:
    """Of `triples`, those a row that may hold graph facts (`GRAPH_HOLDER_SQL`) holds:
    stated for its current value, or carried from a value it was revised from
    (`tfr.carried`). `rows` (ids) limits the rows asked; without it, rows with no
    row links of their own count too (`held_by_unlinked_rows`)."""
    return frozenset(await live_holders(org, namespace, triples, ch=ch, rows=rows))


async def live_holders(
    org: str,
    namespace: str,
    triples: list[tfr.Triple],
    *,
    ch: Any,
    rows: list[str] | None = None,
) -> dict[tfr.Triple, set[Holder]]:
    """`stated_by_live_rows`, with the rows holding each triple."""
    if not triples:
        return {}
    await own_write_published()
    only_rows = "AND memory_id IN {rows:Array(UUID)}" if rows is not None else ""
    # A parameter's worth of triples at a time: long names make a long list.
    found = await query_all_sliced(
        ch,
        f"""
        WITH links AS (
            SELECT memory_id, content_sha256, subject, relation, object
            FROM loom.entity_triple_rows
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND (subject, relation, object) IN {{triples:Array(Tuple(String, String, String))}}
              {only_rows}
        )
        SELECT toString(l.memory_id) AS id, toString(m.content_sha256) AS current,
               toString(l.content_sha256) AS sha, m.value AS value,
               m.updated_at AS updated_at,
               l.subject AS subject, l.relation AS relation, l.object AS object
        FROM links AS l
        INNER JOIN (
            SELECT memory_id, content_sha256, value, updated_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id IN (SELECT memory_id FROM links)
              AND {GRAPH_HOLDER_SQL}
        ) AS m ON l.memory_id = m.memory_id
        SETTINGS select_sequential_consistency = 1
        """,
        {"org": org, "ns": namespace, "triples": triples, "rows": rows or []},
        "triples",
    )
    # A link to the row's current value holds; one to a value the row was revised
    # from is judged against the value it has now.
    held: dict[tfr.Triple, set[Holder]] = {}
    heads: dict[str, dict[str, Any]] = {}
    hashes: dict[str, dict[tfr.Triple, set[str]]] = {}
    for f in found:
        triple = (str(f["subject"]), str(f["relation"]), str(f["object"]))
        mid = str(f["id"])
        if f["sha"] == f["current"]:
            held.setdefault(triple, set()).add(Holder(mid, str(f["current"]), f["updated_at"]))
        else:
            heads[mid] = {
                "id": mid,
                "content_sha256": f["current"],
                "value": f["value"],
                "updated_at": f["updated_at"],
            }
            hashes.setdefault(mid, {}).setdefault(triple, set()).add(str(f["sha"]))
    if rows is None:
        for triple, holders in (await _unlinked_holders(org, namespace, triples, ch=ch)).items():
            held.setdefault(triple, set()).update(holders)
    if not heads:
        return held
    stated_now = await _stated_links(org, namespace, list(heads.values()), ch=ch)
    holds = await carried_by(org, namespace, list(heads.values()), stated_now, hashes, ch=ch)
    for mid, carried in holds.items():
        head = heads[mid]
        for triple in carried:
            holder = Holder(mid, str(head["content_sha256"]), head["updated_at"])
            held.setdefault(triple, set()).add(holder)
    return held


# The migration that started linking rows to triples: a value first written before it
# was applied has no links of its own.
_ROW_LINKS_MIGRATION = "065_entity_triple_rows.sql"


async def held_by_unlinked_rows(
    org: str,
    namespace: str,
    triples: list[tfr.Triple] | None,
    *,
    ch: Any,
    rows: list[str] | None = None,
) -> frozenset[tfr.Triple]:
    """Of `triples`, those a row with no link of its own holds: a live row whose current
    value has no triple its extractor stated (`entity_triple_rows`), of a key the triple
    is linked to (`entity_triple_memories`), that still names it (`tfr.still_states`).
    The key link counts for a value first written before rows were linked, or for one a
    build that links only keys wrote since (the previous build, during a rollout): its
    key link is then no older than the value, and no row of the key links the
    triple. A value this build wrote that stated nothing has neither, and holds none.
    With `rows` (ids) and no `triples`: what those rows hold of every triple their keys
    are linked to."""
    await own_write_published()
    return frozenset(await _unlinked_holders(org, namespace, triples, ch=ch, rows=rows))


async def _unlinked_holders(
    org: str,
    namespace: str,
    triples: list[tfr.Triple] | None,
    *,
    ch: Any,
    rows: list[str] | None = None,
) -> dict[tfr.Triple, set[Holder]]:
    """`held_by_unlinked_rows`, with the rows holding each triple."""
    if triples is None:
        # The triples of the rows' keys, as the follow matched them by text.
        wanted = """memory_key IN (
                  SELECT memory_key FROM loom.memory
                  WHERE org = {org:String} AND namespace = {ns:String}
                    AND memory_id IN {rows:Array(UUID)})"""
    else:
        wanted = "(subject, relation, object) IN {triples:Array(Tuple(String, String, String))}"
    only_rows = "AND memory_id IN {rows:Array(UUID)}" if rows is not None else ""
    sql = f"""
        WITH keyed AS (
            SELECT memory_key, subject, relation, object, max(seen_at) AS seen_at
            FROM loom.entity_triple_memories
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND {wanted}
            GROUP BY memory_key, subject, relation, object
        ),
        live AS (
            SELECT memory_id, memory_key, value, content_sha256, updated_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_key IN (SELECT memory_key FROM keyed)
              AND {GRAPH_HOLDER_SQL}
              {only_rows}
        ),
        -- When each value was first written: a pin, a lock or an Undo writes it again. A
        -- value older than the mirror has no version there, and joins as 0.
        first_written AS (
            SELECT memory_id, content_sha256, min(updated_at) AS at
            FROM loom.memory_versions
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id IN (SELECT memory_id FROM live)
            GROUP BY memory_id, content_sha256
        ),
        -- The rows of each key that link the triple (any of their values): a key link
        -- they write is theirs.
        linked AS (
            SELECT DISTINCT m.memory_key AS memory_key, r.subject AS subject,
                   r.relation AS relation, r.object AS object
            FROM loom.entity_triple_rows AS r
            INNER JOIN (
                SELECT DISTINCT memory_id, memory_key FROM loom.memory
                WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  AND memory_key IN (SELECT memory_key FROM keyed)
            ) AS m ON r.memory_id = m.memory_id
            WHERE r.org = {{org:String}} AND r.namespace = {{ns:String}}
              AND (r.subject, r.relation, r.object) IN (SELECT subject, relation, object FROM keyed)
        )
        SELECT toString(l.memory_id) AS id, toString(l.content_sha256) AS sha,
               l.updated_at AS updated_at, l.value AS value,
               k.subject AS subject, k.relation AS relation, k.object AS object
        FROM keyed AS k
        INNER JOIN live AS l ON k.memory_key = l.memory_key
        LEFT JOIN first_written AS f
            ON f.memory_id = l.memory_id AND f.content_sha256 = l.content_sha256
        -- No link its extractor stated for its current value (a successor written for
        -- it, inferred, or a link of another value does not count)...
        WHERE (l.memory_id, l.content_sha256) NOT IN (
            SELECT memory_id, content_sha256 FROM loom.entity_triple_rows
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND NOT inferred
              AND memory_id IN (SELECT memory_id FROM live)
        )
          -- ...and first written before rows were linked, or by a build that links only
          -- keys: a key link no older than the value, that no linked row of the key wrote.
          -- The cutoff assumes one ledger row per migration (migrate inserts it once):
          -- a re-applied row would move it to the later time once the two merge.
          AND (
              f.at < (
                  SELECT min(applied_at) FROM loom._migrations
                  WHERE filename = {{row_links:String}}
              )
              OR (
                  k.seen_at >= f.at
                  AND (k.memory_key, k.subject, k.relation, k.object) NOT IN (
                      SELECT memory_key, subject, relation, object FROM linked)
              )
          )
        SETTINGS select_sequential_consistency = 1
        """
    params = {
        "org": org,
        "ns": namespace,
        "triples": triples or [],
        "rows": rows or [],
        "row_links": _ROW_LINKS_MIGRATION,
    }
    if triples is None:
        found = await ch.query_all(sql, params=params)
    else:  # a parameter's worth of triples at a time: long names make a long list
        found = await query_all_sliced(ch, sql, params, "triples")
    held: dict[tfr.Triple, set[Holder]] = {}
    for f in found:
        triple = (str(f["subject"]), str(f["relation"]), str(f["object"]))
        if tfr.still_states(as_text(f["value"]), triple):
            holder = Holder(str(f["id"]), str(f["sha"]), f["updated_at"])
            held.setdefault(triple, set()).add(holder)
    return held


async def _stated_links(
    org: str, namespace: str, rows: list[dict[str, Any]], *, ch: Any
) -> dict[str, frozenset[tfr.Triple]]:
    """What the extractor stated for each row's current value, by row id."""
    found = await ch.query_all(
        """
        SELECT toString(memory_id) AS id, subject, relation, object
        FROM loom.entity_triple_rows
        WHERE org = {org:String} AND namespace = {ns:String}
          AND NOT inferred AND (memory_id, content_sha256) IN {keys:Array(Tuple(UUID, String))}
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": org,
            "ns": namespace,
            "keys": sorted({(str(r["id"]), str(r["content_sha256"])) for r in rows}),
        },
    )
    stated: dict[str, set[tfr.Triple]] = {}
    for f in found:
        stated.setdefault(str(f["id"]), set()).add(
            (str(f["subject"]), str(f["relation"]), str(f["object"]))
        )
    return {i: frozenset(t) for i, t in stated.items()}
