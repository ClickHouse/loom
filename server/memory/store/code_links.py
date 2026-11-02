"""Statements over loom.memory_code_links (a memory's links to the code it is about)
and loom.memory_code_link_queue (the memory writes the link worker reads)."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, TypedDict, cast, get_origin, get_type_hints

from server.core.ch_params import chunks
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client, query_all_sliced
from server.memory.domain.code_link_keys import IDENTITY, LINK_KEY, QueuePosition

TABLE = "loom.memory_code_links"


class Former(TypedDict):
    """An identity a link had before a move, with its baseline there and where that
    baseline's text was."""

    path: str
    name: str
    hash: str
    baseline_commit: str
    baseline_path: str
    baseline_name: str


class LinkRow(TypedDict):
    """A row of loom.memory_code_links, read or to write; `memory_id` as text."""

    org: str
    code_namespace: str
    path: str
    qualified_name: str
    namespace: str
    memory_id: str
    memory_key: str
    agent: str
    user_id: str
    session_id: str
    inputs_hash: str
    complete: int
    body_hash: str
    checked_hash: str
    start_line: int
    end_line: int
    state: str
    checked_commit: str
    checked_path: str
    anchored_commit: str
    anchored_path: str
    anchored_hash: str
    anchored_name: str
    baseline_commit: str
    baseline_path: str
    baseline_name: str
    changed_since: str
    former: list[Former]
    policy_due: int
    suggestion: str
    judged_hash: str
    carried: int
    fingerprint_version: str
    checked_at: datetime
    tombstoned_at: datetime | None
    version: int


# the columns whose default is not their type's empty value
_DEFAULTS: dict[str, Any] = {"user_id": "-", "session_id": "-", "complete": 1, "state": "fresh"}


def _empty(hint: Any) -> Any:
    return [] if get_origin(hint) is list else {str: "", int: 0}.get(hint)


LINK_COLS: dict[str, Any] = {
    name: _DEFAULTS.get(name, _empty(hint)) for name, hint in get_type_hints(LinkRow).items()
}


class PendingLink(LinkRow):
    expired: int  # older than the days `pending` was asked for


def _links(rows: list[dict[str, Any]]) -> list[LinkRow]:
    """Link rows read back, `memory_id` (a UUID) as text."""
    for r in rows:
        r["memory_id"] = str(r["memory_id"])
    return cast(list[LinkRow], rows)


_LIVE = f"FROM {TABLE} FINAL WHERE tombstoned_at IS NULL"


def link_row(base: LinkRow | None = None, *, follows: int | None = None, **fields: Any) -> LinkRow:
    """A full link row: `base` (a row read back) with `fields` over it, a fresh version;
    with `follows`, the version right after that one, so any write made since it wins."""
    if unknown := fields.keys() - LINK_COLS.keys():
        raise TypeError(f"no such link column: {', '.join(sorted(unknown))}")
    read: Mapping[str, object] = base or {}
    row = {k: read.get(k, d) for k, d in LINK_COLS.items()} | fields
    row["checked_at"] = row["checked_at"] or naive_utc_now()
    row["version"] = time.time_ns() if follows is None else follows + 1
    return cast(LinkRow, row)


async def insert(rows: list[LinkRow], *, ch: _CHClient | None = None) -> None:
    """Write `rows`; where an org had a recent erasure, delete those of gone memories again."""
    if not rows:
        return
    ch = ch or _default_client()
    await ch.insert(TABLE, cast(list[dict[str, Any]], rows))
    await _drop_links_of_erased(ch, rows)


async def _drop_links_of_erased(ch: _CHClient, rows: list[LinkRow]) -> None:
    """Delete the links just written for memories that are gone, in the orgs with a recent
    erasure: a writer that read before it would bring back what it removed. `sweep_page`
    retries what this misses."""
    by_org: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_org[r["org"]].add(r["memory_id"])
    erasing = await _erasing(ch, sorted(by_org))
    await _drop_gone(ch, {org: ids for org, ids in by_org.items() if org in erasing})


async def _erasing(ch: _CHClient, orgs: list[str]) -> set[str]:
    """The orgs among `orgs` with an erasure not finished, or finished in the last day; a
    failed one that is never retried stops counting a day after it ended."""
    rows = await ch.query_all(
        "SELECT DISTINCT org FROM loom.erasure_requests FINAL WHERE org IN {orgs:Array(String)} "
        "AND (completed_at IS NULL OR completed_at > now() - INTERVAL 1 DAY)",
        params={"orgs": orgs},
    )
    return {str(r["org"]) for r in rows}


_KEY_COLUMNS = LINK_KEY
_KEY = ", ".join(LINK_KEY)


def _past(columns: Sequence[str], types: Sequence[str]) -> str:
    """SQL: `columns` sort after the cursor bound as {a0}, {a1}, …; written column by
    column, so the primary index reads it as a range (a tuple `>` it cannot)."""
    last = len(columns) - 1
    sql = f"{columns[last]} > {{a{last}:{types[last]}}}"
    for i in range(last - 1, -1, -1):
        bound = f"{{a{i}:{types[i]}}}"
        sql = f"{columns[i]} >= {bound} AND ({columns[i]} > {bound} OR ({sql}))"
    return sql


def _past_params(after: Sequence[Any]) -> dict[str, Any]:
    return {f"a{i}": v for i, v in enumerate(after)}


# link rows a sweep page reads per memory write a pass links: a key-only read
SWEEP_ROWS_PER_WRITE = 10
_KEY_TYPES = ("String",) * 5 + ("UUID",)


async def sweep_page(
    after: tuple[str, ...] | None, limit: int, *, ch: _CHClient | None = None
) -> tuple[tuple[str, ...] | None, int]:
    """Drop gone memories' links and old tombstones among the next `limit` ×
    `SWEEP_ROWS_PER_WRITE` link rows after `after`; returns the next page's start (None
    at the table's end) and the memories."""
    ch = ch or _default_client()
    rows_per_page = int(limit) * SWEEP_ROWS_PER_WRITE
    since = f"WHERE {_past(_KEY_COLUMNS, _KEY_TYPES)}" if after else ""
    rows = await ch.query_all(
        # memory_id as stored: an alias of that name would be what WHERE and ORDER BY
        # read, a String against the cursor's UUID
        f"SELECT {_KEY} FROM {TABLE} {since} ORDER BY {_KEY} LIMIT {{lim:UInt32}}",
        params={"lim": rows_per_page, **_past_params(after or ())},
    )
    by_org: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_org[str(r["org"])].add(str(r["memory_id"]))
    dropped = await _drop_gone(ch, by_org)
    await _purge_tombstones(ch, [tuple(r[c] for c in _KEY_COLUMNS) for r in rows])
    full = len(rows) == rows_per_page
    last = tuple(str(rows[-1][c]) for c in _KEY_COLUMNS) if full else None
    return last, dropped


# days a link's tombstone is kept before every version of its key is deleted
TOMBSTONE_DAYS = 7
_KEY_TUPLES = "Array(Tuple(String, String, String, String, String, UUID))"
_ROW_TUPLES = "Array(Tuple(String, String, String, String, String, UUID, UInt64))"
_FINAL = f"FROM {TABLE} FINAL"


async def _purge_tombstones(ch: _CHClient, keys: list[tuple[Any, ...]]) -> None:
    """Delete every version of those of `keys` whose newest row is a tombstone older
    than `TOMBSTONE_DAYS`, up to that tombstone: the tombstone alone would bring the
    versions under it back, and a row written since the read stays."""
    if not keys:
        return
    old = await query_all_sliced(
        ch,
        f"SELECT {_KEY}, version {_FINAL} WHERE ({_KEY}) IN {{keys:{_KEY_TUPLES}}} "
        "AND tombstoned_at < now() - INTERVAL {days:UInt32} DAY",
        {"keys": keys, "days": TOMBSTONE_DAYS},
        "keys",
    )
    last = {tuple(r[c] for c in _KEY_COLUMNS): r["version"] for r in old}
    rows = await query_all_sliced(
        ch,
        f"SELECT {_KEY}, version FROM {TABLE} WHERE ({_KEY}) IN {{keys:{_KEY_TUPLES}}}",
        {"keys": list(last)},
        "keys",
    )
    under = [
        (*key, r["version"])
        for r in rows
        if r["version"] <= last[key := tuple(r[c] for c in _KEY_COLUMNS)]
    ]
    for part in chunks(under):
        await ch.execute(
            f"DELETE FROM {TABLE} WHERE ({_KEY}, version) IN {{rows:{_ROW_TUPLES}}} "
            "SETTINGS lightweight_deletes_sync = 2",
            params={"rows": part},
        )


async def drop_gone(org: str, memory_ids: set[str], *, ch: _CHClient | None = None) -> int:
    """Delete the links of those of `memory_ids` that are gone or tombstoned."""
    return await _drop_gone(ch or _default_client(), {org: memory_ids})


async def _drop_gone(ch: _CHClient, by_org: dict[str, set[str]]) -> int:
    """Delete the links older than this read of the memories in `by_org` that are gone or
    tombstoned; returns how many memories that was."""
    dropped = 0
    for org, ids in by_org.items():
        before = time.time_ns()
        live = await query_all_sliced(
            ch,
            "SELECT DISTINCT toString(memory_id) AS mid FROM loom.memory FINAL "
            "WHERE org = {org:String} AND memory_id IN {ids:Array(UUID)} "
            "AND tombstoned_at IS NULL",
            {"org": org, "ids": sorted(ids)},
            "ids",
        )
        gone = sorted(ids - {str(r["mid"]) for r in live})
        for part in chunks(gone):
            await ch.execute(
                f"DELETE FROM {TABLE} WHERE org = {{org:String}} "
                "AND memory_id IN {ids:Array(UUID)} AND version < {before:UInt64} "
                "SETTINGS lightweight_deletes_sync = 2",
                params={"org": org, "ids": part, "before": before},
            )
        dropped += len(gone)
    return dropped


def tombstones(rows: Sequence[LinkRow]) -> list[LinkRow]:
    """`rows` deleted, each one version above the row read (`link_row`): a write made
    since it was read is newer and stays. A tombstone keeps no suggestion, a rewrite of
    the memory's text."""
    now = naive_utc_now()
    return [
        link_row(
            r,
            follows=r["version"],
            tombstoned_at=now,
            checked_at=now,
            suggestion="",
            judged_hash="",
        )
        for r in rows
    ]


async def for_memories(
    org: str, memory_ids: Sequence[str], *, ch: _CHClient | None = None
) -> list[LinkRow]:
    """Live links of these memories."""
    if not memory_ids:
        return []
    rows = await query_all_sliced(
        ch or _default_client(),
        f"SELECT * {_LIVE} AND org = {{org:String}} AND memory_id IN {{ids:Array(UUID)}}",
        {"org": org, "ids": sorted({str(i) for i in memory_ids})},
        "ids",
    )
    return _links(rows)


async def for_paths(
    org: str,
    code_namespaces: Sequence[str] | None,
    paths: Sequence[str],
    *,
    include_former: bool = False,
    namespaces: Sequence[str] | None = None,
    states: Sequence[str] | None = None,
    ch: _CHClient | None = None,
) -> list[LinkRow]:
    """Live links of `code_namespaces` (None: every one) on `paths`; with
    `include_former`, also those that were there before a move or were made there. Only
    those of the memory `namespaces` and in `states`, when given."""
    ch = ch or _default_client()
    columns = ("path", "former_paths", "anchored_path") if include_former else ("path",)
    narrow = {"namespace": namespaces, "state": states}
    # one read per column, each narrowed by its own index: an OR of them by none
    reads = await asyncio.gather(
        *(_for_code(ch, org, code_namespaces, {column: paths, **narrow}) for column in columns)
    )
    return list({_key(r): r for rows in reads for r in rows}.values())


async def in_code_namespace(
    org: str, code_namespace: str, *, ch: _CHClient | None = None
) -> list[LinkRow]:
    """Every live link of `code_namespace`."""
    return await _for_code(ch or _default_client(), org, [code_namespace], {})


async def for_commits(
    org: str, code_namespaces: Sequence[str], commits: Sequence[str], *, ch: _CHClient | None = None
) -> list[LinkRow]:
    """Live links of `code_namespaces` last checked at one of `commits`."""
    narrow: dict[str, Sequence[str] | None] = {"checked_commit": commits}
    return await _for_code(ch or _default_client(), org, code_namespaces, narrow)


def _key(r: LinkRow) -> tuple[str, ...]:
    """The table's sort key of link `r`."""
    row = cast(Mapping[str, str], r)
    return tuple(row[c] for c in _KEY_COLUMNS)


async def _for_code(
    ch: _CHClient,
    org: str,
    code_namespaces: Sequence[str] | None,
    narrow: dict[str, Sequence[str] | None],
) -> list[LinkRow]:
    """Live links of `code_namespaces` with each column of `narrow` in its values (None:
    any); the one long list, a pass's changed paths or its commits, bound in slices."""
    where = ["org = {org:String}"]
    params: dict[str, Any] = {"org": org}
    long = ""
    for column, values in {"code_namespace": code_namespaces, **narrow}.items():
        if values is not None:
            bound = f"{{{column}:Array(String)}}"
            # an array column holds any of them
            where.append(
                f"hasAny(former.path, {bound})"
                if column == "former_paths"
                else f"{column} IN {bound}"
            )
            params[column] = list(values)
            if column in ("path", "former_paths", "anchored_path", "checked_commit"):
                long = column
    sql = f"SELECT * {_LIVE} AND {' AND '.join(where)}"
    return _links(
        await query_all_sliced(ch, sql, params, long) if long else await ch.query_all(sql, params)
    )


async def stale_fingerprints(
    org: str,
    code_namespace: str,
    version: str,
    *,
    after: tuple[str, ...] | None,
    limit: int,
    ch: _CHClient | None = None,
) -> tuple[list[LinkRow], tuple[str, ...] | None]:
    """Up to `limit` live links of the namespace fingerprinted under another `version`,
    in key order after `after`, and the next page's start (None at the end)."""
    since = f"AND {_past(_KEY_COLUMNS, _KEY_TYPES)} " if after else ""
    rows = _links(
        await (ch or _default_client()).query_all(
            f"SELECT * {_LIVE} AND org = {{org:String}} AND code_namespace = {{cns:String}} "
            f"AND state != 'pending' AND fingerprint_version != {{v:String}} {since}"
            f"ORDER BY {_KEY} LIMIT {{lim:UInt32}}",
            params={
                "org": org,
                "cns": code_namespace,
                "v": version,
                "lim": int(limit),
                **_past_params(after or ()),
            },
        )
    )
    return rows, _key(rows[-1]) if len(rows) == int(limit) else None


async def pending(
    org: str, code_namespace: str, *, days: int, ch: _CHClient | None = None
) -> list[PendingLink]:
    """The namespace's pending names (and `*` markers of memories written before its
    first index), each with `expired` (older than `days`)."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT *, checked_at < now() - INTERVAL {{days:UInt32}} DAY AS expired {_LIVE} "
        "AND org = {org:String} AND code_namespace = {cns:String} AND state = 'pending'",
        params={"org": org, "cns": code_namespace, "days": int(days)},
    )
    return cast(list[PendingLink], _links(rows))


async def lost(
    org: str, code_namespace: str, *, days: int, ch: _CHClient | None = None
) -> list[LinkRow]:
    """The namespace's unresolved and missing links that entered that state at most
    `days` ago."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT * {_LIVE} AND org = {{org:String}} AND code_namespace = {{cns:String}} "
        "AND state IN ('unresolved', 'missing') "
        "AND checked_at > now() - INTERVAL {days:UInt32} DAY",
        params={"org": org, "cns": code_namespace, "days": int(days)},
    )
    return _links(rows)


async def policy_due(
    org: str, code_namespace: str, version: str, *, limit: int, ch: _CHClient | None = None
) -> list[str]:
    """Up to `limit` memories with a link of `code_namespace` hashed under rules `version`
    whose policy has yet to run, the longest unchecked first."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT toString(memory_id) AS mid, max(checked_at) AS last_checked {_LIVE} "
        "AND org = {org:String} AND code_namespace = {cns:String} AND policy_due = 1 "
        "AND fingerprint_version = {version:String} "
        "GROUP BY memory_id ORDER BY last_checked, mid LIMIT {lim:UInt32}",
        params={"org": org, "cns": code_namespace, "version": version, "lim": int(limit)},
    )
    return [str(r["mid"]) for r in rows]


async def code_namespaces(*, ch: _CHClient | None = None) -> list[tuple[str, str]]:
    """Every (org, code namespace) holding a live link, and maybe one whose links were
    all tombstoned since: read without FINAL."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT DISTINCT org, code_namespace FROM {TABLE} WHERE tombstoned_at IS NULL "
        "ORDER BY org, code_namespace"
    )
    return [(str(r["org"]), str(r["code_namespace"])) for r in rows]


_HEADS = """
SELECT org, namespace, agent, user_id, session_id, memory_key, actor,
       toString(memory_id) AS mid, value, tags, files,
       toString(content_sha256) AS sha,
       CAST(status AS String) AS status, tombstoned_at,
       pinned, lock_mode, lock_until, verified_by, verified_sha256, sensitivity
FROM loom.memory FINAL
"""


async def served_heads(
    org: str, keys: Sequence[tuple[str, str]], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The served memories stored under these (namespace, memory_key) pairs, every
    identity holding one."""
    if not keys:
        return []
    return await (ch or _default_client()).query_all(
        _HEADS + "WHERE org = {org:String} AND (namespace, memory_key) "
        "IN {keys:Array(Tuple(String, String))} "
        "AND tombstoned_at IS NULL AND status IN ('active', 'conflicted')",
        params={"org": org, "keys": sorted(set(keys))},
    )


_QUEUE_KEY = f"(toUnixTimestamp64Micro(queued_at), version, {', '.join(IDENTITY)})"
_POSITION = "Tuple(Int64, UInt64, String, String, String, String, String, String)"


# loom.memory_code_link_queue's TTL: a cursor older than this may have missed writes.
QUEUE_TTL_DAYS = 7


async def queued_memories(
    after: QueuePosition | None,
    limit: int,
    code_namespaces: Sequence[str],
    *,
    lag_s: int,
    ch: _CHClient | None = None,
) -> tuple[list[dict[str, Any]], list[QueuePosition], QueuePosition]:
    """(heads, positions, cutoff) of the writes queued after `after` and `lag_s` before
    now, in queue order; heads only of memories that may name code, each with its `pos`."""
    ch = ch or _default_client()
    now = await ch.query_one(
        "SELECT toUnixTimestamp64Micro(now64(6) - toIntervalSecond({lag:UInt32})) AS t",
        params={"lag": int(lag_s)},
    )
    cutoff: QueuePosition = (int((now or {})["t"]), 0, "", "", "", "", "", "")
    since = (
        f"AND queued_at >= fromUnixTimestamp64Micro(toInt64({int(after[0])})) "
        f"AND {_QUEUE_KEY} > {{after:{_POSITION}}}"
        if after
        else ""
    )
    entries = await ch.query_all(
        f"""
        SELECT {_QUEUE_KEY} AS pos
        FROM loom.memory_code_link_queue
        WHERE queued_at < fromUnixTimestamp64Micro(toInt64({{cut:Int64}})) {since}
        ORDER BY {_QUEUE_KEY}
        LIMIT {{lim:UInt32}}
        """,
        params={"after": tuple(after) if after else (), "cut": cutoff[0], "lim": int(limit)},
    )
    positions: list[QueuePosition] = [tuple(e["pos"]) for e in entries]
    newest: dict[tuple[str, ...], QueuePosition] = {}
    for p in positions:
        newest[tuple(str(v) for v in p[2:])] = p
    heads = await _heads(ch, list(newest), code_namespaces)
    for h in heads:
        h["pos"] = newest[tuple(str(h[c]) for c in IDENTITY)]
    return heads, positions, cutoff


async def queue_horizon(*, ch: _CHClient | None = None) -> int:
    """The queue time (µs) before which the queue's TTL may have dropped writes."""
    row = await (ch or _default_client()).query_one(
        f"SELECT toUnixTimestamp64Micro(now64(6) - INTERVAL {QUEUE_TTL_DAYS} DAY) AS t"
    )
    return int((row or {})["t"])


async def queue_tail(*, ch: _CHClient | None = None) -> QueuePosition | None:
    """The position of the newest queued write; None when the queue is empty."""
    row = await (ch or _default_client()).query_one(
        f"SELECT {_QUEUE_KEY} AS pos FROM loom.memory_code_link_queue "
        f"ORDER BY {_QUEUE_KEY} DESC LIMIT 1"
    )
    return tuple(row["pos"]) if row else None


async def backfill_page(
    after: tuple[str, ...] | None,
    limit: int,
    code_namespaces: Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> tuple[list[tuple[str, ...]], list[dict[str, Any]]]:
    """A page, in identity order after `after`, of the memories that may name code
    (tagged `repo:`, or written in one of `code_namespaces`) or hold links: the backfill
    of what the queue did not deliver. Returns the page's identities and the head rows
    of those still stored."""
    ch = ch or _default_client()
    params = {"cns": list(code_namespaces), "lim": int(limit), **_past_params(after or ())}
    # the bound is loom.memory's sort key, so a page starts at the cursor; each leg's own
    # order and limit stop it one page on
    since = f" AND {_past(IDENTITY, ('String',) * 6)}" if after else ""
    keys = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key
        FROM (
            SELECT DISTINCT org, namespace, agent, user_id, session_id, memory_key
            FROM loom.memory
            WHERE tombstoned_at IS NULL
              AND (arrayExists(t -> startsWith(t, 'repo:'), tags)
                   OR namespace IN {{cns:Array(String)}}){since}
            ORDER BY org, namespace, agent, user_id, session_id, memory_key
            LIMIT {{lim:UInt32}}
            UNION DISTINCT
            SELECT DISTINCT org, namespace, agent, user_id, session_id, memory_key
            {_LIVE}{since}
            ORDER BY org, namespace, agent, user_id, session_id, memory_key
            LIMIT {{lim:UInt32}}
        )
        ORDER BY org, namespace, agent, user_id, session_id, memory_key
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    idents = [tuple(str(k[c]) for c in IDENTITY) for k in keys]
    return idents, await _heads(ch, idents)


_IDENTS = "Array(Tuple(String, String, String, String, String, String))"


async def for_identities(
    idents: Sequence[tuple[str, ...]], *, ch: _CHClient | None = None
) -> list[LinkRow]:
    """Live links of these memory identities (`IDENTITY`)."""
    if not idents:
        return []
    rows = await query_all_sliced(
        ch or _default_client(),
        f"SELECT * {_LIVE} AND ({', '.join(IDENTITY)}) IN {{ids:{_IDENTS}}}",
        {"ids": sorted(set(idents))},
        "ids",
    )
    return _links(rows)


async def _heads(
    ch: _CHClient, idents: list[tuple[str, ...]], code_namespaces: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """The head rows of `idents`; with `code_namespaces`, only of those that may name code:
    tagged `repo:`, written in one of them, or linked (an untagged one loses its links)."""
    if not idents:
        return []
    may_name_code = ""
    params: dict[str, Any] = {"ident": idents}
    if code_namespaces is not None:
        may_name_code = (
            " AND (arrayExists(t -> startsWith(t, 'repo:'), tags) "
            "OR namespace IN {cns:Array(String)} "
            f"OR memory_id IN (SELECT memory_id FROM {TABLE} WHERE org IN {{orgs:Array(String)}} "
            f"AND ({', '.join(IDENTITY)}) IN {{ident:{_IDENTS}}}))"
        )
        params |= {"cns": list(code_namespaces), "orgs": sorted({i[0] for i in idents})}
    return await query_all_sliced(
        ch,
        _HEADS + f"WHERE ({', '.join(IDENTITY)}) IN {{ident:{_IDENTS}}}" + may_name_code,
        params,
        "ident",
    )


async def memory_heads(
    org: str, memory_ids: Sequence[str], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The head rows of these memories, in the shape `queued_memories` returns."""
    if not memory_ids:
        return []
    return await query_all_sliced(
        ch or _default_client(),
        _HEADS + "WHERE org = {org:String} AND memory_id IN {ids:Array(UUID)}",
        {"org": org, "ids": sorted(set(memory_ids))},
        "ids",
    )


async def code_change_policies(
    org: str, namespaces: Sequence[str], *, ch: _CHClient | None = None
) -> dict[str, str]:
    """namespace -> its `code_change_policy` as stored ('' = the default)."""
    if not namespaces:
        return {}
    rows = await (ch or _default_client()).query_all(
        """
        SELECT namespace, code_change_policy FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace IN {nss:Array(String)}
        """,
        params={"org": org, "nss": sorted(set(namespaces))},
    )
    return {str(r["namespace"]): str(r["code_change_policy"] or "") for r in rows}


def inputs_hash_sql(sha: str, files: str, cns: str, user_id: str) -> str:
    """`code_link_reads.inputs_hash` in SQL, over these expressions."""
    joined = f"arrayConcat([{sha}], arraySort({files}), [{cns}, {user_id}])"
    return f"lower(hex(SHA256(arrayStringConcat({joined}, char(0)))))"


async def stale_of_served(
    org: str,
    namespace: str,
    states: Sequence[str],
    memory_keys: Sequence[str] | None = None,
    *,
    trunk: str,
    tag_namespaces: Sequence[tuple[str, str]],
    readable: Sequence[str],
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Up to `limit` (at least one) of the namespace's live links in `states` made from
    their served memory as it is now (under `memory_keys` when given), into `readable`
    code, the newest checked of each memory, newest first; `total` counts their memories.
    A memory's code namespace is `trunk`, else that of the first (repo tag, namespace) of
    `tag_namespaces` it is tagged."""
    keys = "AND memory_key IN {mks:Array(String)}" if memory_keys is not None else ""
    narrow = (
        f"org = {{org:String}} AND namespace = {{ns:String}} "
        f"AND state IN {{states:Array(String)}} {keys}"
    )
    cns_now = (
        "if({trunk:String} != '', {trunk:String}, arrayElement({tag_cns:Array(String)}, "
        "arrayFirstIndex(t -> has(tags, t), {repo_tags:Array(String)})))"
    )
    return await (ch or _default_client()).query_all(
        f"""
        SELECT l.memory_key AS memory_key, l.code_namespace AS code_namespace,
               l.path AS path, l.qualified_name AS qualified_name, l.state AS state,
               l.changed_since AS changed_since, l.checked_at AS checked_at,
               uniqExact(l.memory_id) OVER () AS total
        FROM (
            SELECT memory_id, memory_key, code_namespace, path, qualified_name, state,
                   changed_since, checked_at, inputs_hash
            {_LIVE} AND {narrow} AND code_namespace IN {{readable:Array(String)}}
        ) AS l
        INNER JOIN (
            SELECT memory_id,
                   {inputs_hash_sql("toString(content_sha256)", "files", cns_now, "user_id")}
                   AS inputs_now
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id IN (SELECT memory_id FROM {TABLE} WHERE {narrow})
              AND tombstoned_at IS NULL AND status IN ('active', 'conflicted')
        ) AS m ON m.memory_id = l.memory_id
        WHERE l.inputs_hash = m.inputs_now
        ORDER BY l.checked_at DESC, l.memory_key, l.path, l.qualified_name
        LIMIT 1 BY l.memory_id
        LIMIT {{lim:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "states": list(states),
            "mks": list(memory_keys or []),
            "trunk": trunk,
            "repo_tags": [t for t, _ in tag_namespaces],
            "tag_cns": [c for _, c in tag_namespaces],
            "readable": sorted(readable),
            "lim": max(int(limit), 1),
        },
    )


async def live_links(
    org: str, idents: Sequence[tuple[str, str, str, str, str]], *, ch: _CHClient | None = None
) -> list[LinkRow]:
    """Live links of the memories at these (namespace, agent, user_id, session_id,
    memory_key), read through the memory_key index."""
    if not idents:
        return []
    rows = await (ch or _default_client()).query_all(
        f"SELECT * {_LIVE} AND org = {{org:String}} AND memory_key IN {{mks:Array(String)}} "
        "AND (namespace, agent, user_id, session_id, memory_key) "
        "IN {ids:Array(Tuple(String, String, String, String, String))}",
        params={"org": org, "ids": sorted(set(idents)), "mks": sorted({i[4] for i in idents})},
    )
    return _links(rows)
