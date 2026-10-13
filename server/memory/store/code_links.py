"""Statements over loom.memory_code_links (a memory's links to the code it is about)
and loom.memory_code_link_queue (the memory writes the link worker reads)."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, TypedDict, cast, get_origin, get_type_hints

from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client, param_slices, query_all_sliced

TABLE = "loom.memory_code_links"


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
    former_paths: list[str]
    former_names: list[str]
    former_hashes: list[str]
    former_baseline_commits: list[str]
    former_baseline_paths: list[str]
    former_baseline_names: list[str]
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
IDENTITY = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")


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
    """Write `rows`; in an org an erasure is running in, or ran in today, then drop them
    again for any memory that is gone by now. A writer that read before the erasure and
    wrote after it would otherwise bring back what it removed; `sweep_page` catches what
    this misses."""
    if not rows:
        return
    ch = ch or _default_client()
    await ch.insert(TABLE, cast(list[dict[str, Any]], rows))
    by_org: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_org[r["org"]].add(r["memory_id"])
    erasing = await _erasing(ch, sorted(by_org))
    await _drop_gone(ch, {org: ids for org, ids in by_org.items() if org in erasing})


async def _erasing(ch: _CHClient, orgs: list[str]) -> set[str]:
    """The orgs among `orgs` with an erasure not completed, or completed in the last day."""
    rows = await ch.query_all(
        "SELECT DISTINCT org FROM loom.erasure_requests FINAL WHERE org IN {orgs:Array(String)} "
        "AND (status != 'completed' OR completed_at > now() - INTERVAL 1 DAY)",
        params={"orgs": orgs},
    )
    return {str(r["org"]) for r in rows}


_KEY = "org, code_namespace, path, qualified_name, namespace, memory_id"
_KEY_COLUMNS = _KEY.split(", ")


async def sweep_page(
    after: tuple[str, ...] | None, limit: int, *, ch: _CHClient | None = None
) -> tuple[tuple[str, ...] | None, int]:
    """Drop the links of the gone memories among the next `limit` link rows after
    `after`, read in the table's sort order (the retry of an insert's cleanup that
    failed, at whatever age), and the tombstones among them older than
    `TOMBSTONE_DAYS`. Returns where the next page starts (None: the table was read to
    its end) and how many memories' links went."""
    ch = ch or _default_client()
    # the tuple alone is no key condition: bounds on the key's first columns let the read
    # skip the rows before the cursor
    since = ""
    params: dict[str, Any] = {"lim": int(limit)}
    if after:
        since = (
            "WHERE org >= {org:String} AND (org > {org:String} OR (code_namespace >= "
            "{cns:String} AND (code_namespace > {cns:String} OR path >= {path:String}))) "
            f"AND ({_KEY}) > {{after:Tuple(String, String, String, String, String, UUID)}}"
        )
        org, cns, path = after[:3]
        params |= {"after": tuple(after), "org": org, "cns": cns, "path": path}
    rows = await ch.query_all(
        # memory_id as stored: an alias of that name would be what WHERE and ORDER BY
        # read, a String against the cursor's UUID
        f"SELECT {_KEY} FROM {TABLE} {since} ORDER BY {_KEY} LIMIT {{lim:UInt32}}",
        params=params,
    )
    by_org: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_org[str(r["org"])].add(str(r["memory_id"]))
    dropped = await _drop_gone(ch, by_org)
    await _purge_tombstones(ch, [tuple(r[c] for c in _KEY_COLUMNS) for r in rows])
    last = tuple(str(rows[-1][c]) for c in _KEY_COLUMNS) if len(rows) == limit else None
    return last, dropped


# days a link's tombstone is kept before every version of its key is deleted
TOMBSTONE_DAYS = 7
_KEY_TUPLES = "Array(Tuple(String, String, String, String, String, UUID))"


async def _purge_tombstones(ch: _CHClient, keys: list[tuple[Any, ...]]) -> None:
    """Delete every version of those of `keys` whose newest row is a tombstone older
    than `TOMBSTONE_DAYS`: the tombstone alone would bring the versions under it back."""
    if not keys:
        return
    old = await query_all_sliced(
        ch,
        f"SELECT {_KEY} FROM {TABLE} FINAL WHERE ({_KEY}) IN {{keys:{_KEY_TUPLES}}} "
        "AND tombstoned_at < now() - INTERVAL {days:UInt32} DAY",
        {"keys": keys, "days": TOMBSTONE_DAYS},
        "keys",
    )
    for part in param_slices([tuple(r[c] for c in _KEY_COLUMNS) for r in old]):
        await ch.execute(
            f"DELETE FROM {TABLE} WHERE ({_KEY}) IN {{keys:{_KEY_TUPLES}}} "
            "SETTINGS lightweight_deletes_sync = 2",
            params={"keys": part},
        )


async def drop_gone(org: str, memory_ids: set[str], *, ch: _CHClient | None = None) -> int:
    """Delete the links of those of `memory_ids` that are gone or tombstoned."""
    return await _drop_gone(ch or _default_client(), {org: memory_ids})


async def _drop_gone(ch: _CHClient, by_org: dict[str, set[str]]) -> int:
    """Delete the links of the memories in `by_org` that are gone or tombstoned; how many
    memories that was. Only rows older than the liveness read go: a memory restored and
    relinked meanwhile has links of a newer version, and they stay."""
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
        for part in param_slices(gone):
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


async def for_code(
    org: str,
    code_namespaces: Sequence[str] | None,
    *,
    paths: Sequence[str] | None = None,
    namespaces: Sequence[str] | None = None,
    memory_keys: Sequence[str] | None = None,
    states: Sequence[str] | None = None,
    checked_commits: Sequence[str] | None = None,
    or_former_paths: bool = False,
    ch: _CHClient | None = None,
) -> list[LinkRow]:
    """Live links of `code_namespaces` (None = every one), narrowed to `paths` (or, with
    `or_former_paths`, links that lived there before a move or where they were made), the
    memory `namespaces` and `memory_keys`, link `states` and the commits they were checked
    at when given; by `paths` or by `checked_commits`, not both."""
    if paths is not None and checked_commits is not None:
        raise ValueError("for_code narrows by paths or by commits, not both")
    # one read per column, each narrowed by its own index: an OR of them by none
    columns = ("path", "former_paths", "anchored_path") if or_former_paths else ("path",)
    rows: list[LinkRow] = []
    for column in columns if paths is not None else ("path",):
        rows += await _for_code(
            ch or _default_client(),
            org,
            code_namespaces,
            {
                column: paths,
                "namespace": namespaces,
                "memory_key": memory_keys,
                "state": states,
                "checked_commit": checked_commits,
            },
        )
    # a moved link answers to its path, its former ones and its anchor
    return list({_key(r): r for r in rows}.values())


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
                f"hasAny({column}, {bound})" if column == "former_paths" else f"{column} IN {bound}"
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
    limit: int,
    offset: int = 0,
    ch: _CHClient | None = None,
) -> list[LinkRow]:
    """Up to `limit` live links of the namespace fingerprinted under another `version`,
    in key order, past the first `offset`."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT * {_LIVE} AND org = {{org:String}} AND code_namespace = {{cns:String}} "
        "AND state != 'pending' AND fingerprint_version != {v:String} "
        f"ORDER BY {_KEY} LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}",
        params={
            "org": org,
            "cns": code_namespace,
            "v": version,
            "lim": int(limit),
            "off": int(offset),
        },
    )
    return _links(rows)


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


async def policy_due(org: str, code_namespace: str, *, ch: _CHClient | None = None) -> list[str]:
    """The memories with a link of `code_namespace` whose policy has yet to run."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT DISTINCT toString(memory_id) AS mid {_LIVE} AND org = {{org:String}} "
        "AND code_namespace = {cns:String} AND policy_due = 1",
        params={"org": org, "cns": code_namespace},
    )
    return [str(r["mid"]) for r in rows]


async def code_namespaces(*, ch: _CHClient | None = None) -> list[tuple[str, str]]:
    """Every (org, code namespace) holding a live link."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT DISTINCT org, code_namespace {_LIVE} ORDER BY org, code_namespace"
    )
    return [(str(r["org"]), str(r["code_namespace"])) for r in rows]


_HEADS = """
SELECT org, namespace, agent, user_id, session_id, memory_key,
       toString(memory_id) AS mid, value, tags, files,
       toString(content_sha256) AS sha,
       CAST(status AS String) AS status, tombstoned_at,
       pinned, lock_mode, verified_by, verified_sha256, sensitivity
FROM loom.memory FINAL
"""


async def linked_keys(
    org: str, keys: Sequence[tuple[str, str]], *, ch: _CHClient | None = None
) -> set[tuple[str, str]]:
    """The (namespace, memory_key) pairs among `keys` that hold, or held, a link: a search
    reads memory heads only for these. Read through the memory_key index and without FINAL,
    so a key whose links are gone may still answer; the reads after it decide."""
    if not keys:
        return set()
    rows = await (ch or _default_client()).query_all(
        f"SELECT DISTINCT namespace, memory_key FROM {TABLE} WHERE org = {{org:String}} "
        "AND memory_key IN {mks:Array(String)} "
        "AND (namespace, memory_key) IN {keys:Array(Tuple(String, String))}",
        params={"org": org, "keys": sorted(set(keys)), "mks": sorted({k for _, k in keys})},
    )
    return {(str(r["namespace"]), str(r["memory_key"])) for r in rows}


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


QueuePosition = tuple[int, int, str, str, str, str, str, str]
_QUEUE_KEY = (
    "(toUnixTimestamp64Micro(queued_at), version, "
    "org, namespace, agent, user_id, session_id, memory_key)"
)
_POSITION = "Tuple(Int64, UInt64, String, String, String, String, String, String)"


# loom.memory_code_link_queue's TTL: a cursor older than this may have missed writes.
QUEUE_TTL_DAYS = 7


async def queued_memories(
    after: QueuePosition | None, limit: int, *, lag_s: int, ch: _CHClient | None = None
) -> tuple[list[dict[str, Any]], list[QueuePosition], QueuePosition]:
    """(head rows, queue positions, cutoff) of the memory writes queued after `after`,
    in insert order. A position is (queue time µs, version, identity): unique per write,
    so a page boundary never splits a bulk write that shares one queue time. `lag_s`
    leaves concurrent inserts time to become visible; `cutoff` is the position every
    write visible to this read precedes. Each head carries `pos`, its latest."""
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
    heads = await _heads(ch, list(newest))
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
    params: dict[str, Any] = {"cns": list(code_namespaces), "lim": int(limit)}
    # in each leg, beside the tuple (no key condition): bounds on the key's first columns,
    # loom.memory's identity and the links' org, so a page skips what came before; and
    # each leg's own order and limit, so it reads no further than one page
    memory_after = links_after = ""
    if after:
        params |= {"after": tuple(after), "org": after[0], "ns": after[1], "agent": after[2]}
        tail = (
            " AND (org, namespace, agent, user_id, session_id, memory_key) > "
            "{after:Tuple(String, String, String, String, String, String)}"
        )
        memory_after = (
            " AND org >= {org:String} AND (org > {org:String} OR (namespace >= {ns:String} "
            "AND (namespace > {ns:String} OR agent >= {agent:String})))" + tail
        )
        links_after = " AND org >= {org:String}" + tail
    keys = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key
        FROM (
            SELECT DISTINCT org, namespace, agent, user_id, session_id, memory_key
            FROM loom.memory
            WHERE tombstoned_at IS NULL
              AND (arrayExists(t -> startsWith(t, 'repo:'), tags)
                   OR namespace IN {{cns:Array(String)}}){memory_after}
            ORDER BY org, namespace, agent, user_id, session_id, memory_key
            LIMIT {{lim:UInt32}}
            UNION DISTINCT
            SELECT DISTINCT org, namespace, agent, user_id, session_id, memory_key
            {_LIVE}{links_after}
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


async def for_identities(
    idents: Sequence[tuple[str, ...]], *, ch: _CHClient | None = None
) -> list[LinkRow]:
    """Live links of these (org, namespace, agent, user_id, session_id, memory_key)."""
    if not idents:
        return []
    rows = await query_all_sliced(
        ch or _default_client(),
        f"SELECT * {_LIVE} AND (org, namespace, agent, user_id, session_id, memory_key) "
        "IN {ids:Array(Tuple(String, String, String, String, String, String))}",
        {"ids": sorted(set(idents))},
        "ids",
    )
    return _links(rows)


async def _heads(ch: _CHClient, idents: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    if not idents:
        return []
    return await query_all_sliced(
        ch,
        _HEADS + "WHERE (org, namespace, agent, user_id, session_id, memory_key) "
        "IN {ident:Array(Tuple(String, String, String, String, String, String))}",
        {"ident": idents},
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


async def embedding_identities(
    org: str, rows: Sequence[tuple[str, str, int]], *, ch: _CHClient | None = None
) -> dict[tuple[str, str, int], set[tuple[str, str, str]]]:
    """(namespace, memory_key, version) -> the (agent, user_id, session_id) of the
    memory_embeddings rows at that version: whose memory a search hit was read from."""
    if not rows:
        return {}
    found = await (ch or _default_client()).query_all(
        "SELECT DISTINCT namespace, memory_key, version, agent, user_id, session_id "
        "FROM loom.memory_embeddings WHERE org = {org:String} "
        "AND namespace IN {nss:Array(String)} AND memory_key IN {mks:Array(String)} "
        "AND version IN {vs:Array(UInt64)} "
        "AND (namespace, memory_key, version) IN {keys:Array(Tuple(String, String, UInt64))}",
        params={
            "org": org,
            "nss": sorted({r[0] for r in rows}),
            "mks": sorted({r[1] for r in rows}),
            "vs": sorted({r[2] for r in rows}),
            "keys": sorted(set(rows)),
        },
    )
    out: dict[tuple[str, str, int], set[tuple[str, str, str]]] = defaultdict(set)
    for r in found:
        key = (str(r["namespace"]), str(r["memory_key"]), int(r["version"]))
        out[key].add((str(r["agent"]), str(r["user_id"]), str(r["session_id"])))
    return dict(out)
