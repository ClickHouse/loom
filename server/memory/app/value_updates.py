"""Value-update supersession on the extraction write path.

A knowledge update — "my personal best is 27:12" on the 23rd, "hoping to beat
my personal best of 25:50" on the 30th — arrives through memory.set_from_messages
as two memories under two keys, both `active`. The point-write conflict check
never runs on that path, and it would not have fired anyway: the pair's
cosine is 0.82 against a 0.85 threshold, and across 86 chat-history bench namespaces
(21,155 memories) the 99th-percentile nearest neighbour is 0.856. Without this
pass the corpus carries no conflict or supersession links at all, and the
reader answers with the older value.

What distinguishes an update from a mere neighbour is not closeness but
shape: the same subject and attribute words, a VALUE token that differs
(number, time, date, money, percentage), and a later date. That is a
deterministic test, so it runs without an LLM: for each memory a batch
writes, look up its near neighbours (cosine >= `min_cosine`, same
namespace and type, different key), keep the ones that are the same
statement with a different value, and mark the OLDER side of each pair
`status='superseded'`, `superseded_by=<newer id>`, `invalid_at=<newer
valid_at>`. Both rows stay retrievable — search includes `superseded` — but
memory.ask's conflict-pair reader now names the pair and
tells the synthesizer which is newer, and as-of replay excludes the older
row after the update.

Fail-open everywhere: a lookup or write error links nothing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.memory.app.auto_resolution import (
    VALUE_UPDATE,
    RetirementTracker,
    record_or_owe,
    retired_entry,
)
from server.memory.domain.conflicts import values_differ
from server.memory.store.conflict_candidates import vector_param, vector_params

log = logging.getLogger(__name__)

# Alphanumeric tokens that contain at least one letter ("5k" counts, "2023" does not).
_WORD_RE = re.compile(r"(?<![a-z0-9])[a-z0-9]*[a-z][a-z0-9']*")
_STOP = frozenset(
    "the a an and or of to in on at for with from by is are was were be been being "
    "has have had do does did this that these those it its they them their my me i "
    "we our you your he she his her user users recently just now currently still "
    "also very really about into over after before again new old".split()
)


def content_tokens(text: str) -> set[str]:
    """Lowercase alphabetic tokens minus stopwords and minus value tokens —
    the words that say WHAT is being measured."""
    words = set(_WORD_RE.findall((text or "").lower()))
    return {w for w in words if len(w) >= 2 and w not in _STOP}


def is_value_update(a: str, b: str, *, min_overlap: float = 0.5) -> bool:
    """True when `a` and `b` state the same attribute with different values:
    both carry value tokens, the value sets differ, and the content words
    overlap by at least `min_overlap` (Jaccard). Pure.

    The day written into each row does not count as a value.
    "Started a solo camping trip to Yosemite on 2023-05-15" and "got back
    from a solo camping trip to Yosemite on 2023-05-17" share half their
    words and differ only in the day; read as one attribute updated, the
    start would be retired and "how many days did I spend" would lose its
    first date. Two rows that differ only in their dates are two events."""
    if not values_differ(a, b):
        return False
    ca, cb = content_tokens(a), content_tokens(b)
    if not ca or not cb:
        return False
    jac = len(ca & cb) / len(ca | cb)
    return jac >= min_overlap


def _as_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(v)[:19])
    except ValueError:
        return None


@dataclass
class UpdateLink:
    older_id: str
    newer_id: str
    older_key: str
    newer_key: str
    cosine: float


@dataclass
class UpdateReport:
    checked: int = 0
    # The links whose older row retired, as the re-read confirmed.
    links: list[UpdateLink] = field(default_factory=list)
    # Those and the ones the re-read could not confirm: the triple follow re-checks them.
    follow_links: list[UpdateLink] = field(default_factory=list)
    errors: int = 0


def decide_links(
    new_id: str,
    new_key: str,
    new_text: str,
    new_at: Any,
    new_created: Any,
    peers: list[dict[str, Any]],
) -> list[UpdateLink]:
    """Pure half: which peers form a value-update pair with the new row, and
    which side is older. `peers` rows carry memory_id, memory_key, value,
    valid_at, created_at, cos. Ordering: valid_at first, created_at as the
    tie-break; an undated side is treated as older than a dated one."""
    out: list[UpdateLink] = []
    n_at, n_cr = _as_dt(new_at), _as_dt(new_created)
    for p in peers:
        if str(p.get("memory_key") or "") == new_key:
            continue
        if not is_value_update(new_text, str(p.get("value") or "")):
            continue
        p_at, p_cr = _as_dt(p.get("valid_at")), _as_dt(p.get("created_at"))
        # An update happens ACROSS time: the newer side must be dated strictly
        # later (by calendar day). Two rows on the same date, or two undated
        # rows, are an enumeration, not an update ("Article 12 …" and
        # "Article 13 …", two products' refill-pack prices, "key 08222 is
        # 251" and "key 17256 is 123"). Same-day rows fall to the conflict
        # detector's domain.
        if n_at is None or p_at is None or n_at.date() == p_at.date():
            continue
        new_is_newer = n_at > p_at
        del n_cr, p_cr
        pid = str(p.get("memory_id") or "")
        cos = float(p.get("cos") or 0.0)
        if new_is_newer:
            out.append(UpdateLink(pid, new_id, str(p.get("memory_key") or ""), new_key, cos))
        else:
            out.append(UpdateLink(new_id, pid, new_key, str(p.get("memory_key") or ""), cos))
    return out


def _supersession_update(
    newer_id: str, invalid_at: datetime | None
) -> Callable[[Any], dict[str, Any]]:
    """Column overrides that retire the older side of a value-update pair."""

    def _update(_row: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"status": "superseded", "superseded_by": newer_id}
        if invalid_at is not None:
            out["invalid_at"] = invalid_at
        return out

    return _update


def _peers_sql_many(n: int, in_scope: str = "") -> str:
    """Batched form of `_peers`: one namespace read for `n`
    freshly written rows, one `cosineDistance` term per row (an `arrayMap`
    over the vector column replicates it per row per fact), `LIMIT 8 BY qidx`
    for the per-row `LIMIT 8`."""
    terms = ", ".join(
        f"({i}, 1 - cosineDistance(e.embedding, {vector_param(i)}))" for i in range(1, n + 1)
    )
    return f"""
        SELECT hit.1 - 1 AS qidx,
               toString(m.memory_id) AS memory_id, m.memory_key AS memory_key,
               m.value AS value, m.valid_at AS valid_at, m.created_at AS created_at,
               hit.2 AS cos
        FROM loom.memory m FINAL
        JOIN loom.memory_embeddings e FINAL
          ON  e.org = m.org AND e.namespace = m.namespace
          AND e.memory_key = m.memory_key
          AND e.user_id = m.user_id AND e.session_id = m.session_id
          AND e.agent = m.agent
        ARRAY JOIN arrayFilter(
            x -> x.2 >= {{min_cos:Float32}}
                 AND {{types:Array(String)}}[x.1] = m.memory_type
                 AND {{keys:Array(String)}}[x.1] != m.memory_key,
            [{terms}]) AS hit
        WHERE m.org = {{org:String}}
          AND m.namespace = {{ns:String}}
          AND m.tombstoned_at IS NULL
          AND m.status IN ('active', 'conflicted')
          AND e.tombstoned_at IS NULL
          AND e.embedding_dim = {{dim:UInt16}}
          AND length(e.embedding) = {{dim:UInt16}}{in_scope}
        ORDER BY qidx ASC, cos DESC
        LIMIT 8 BY qidx
        """


async def _peers_many(
    ch: Any,
    *,
    org: str,
    namespace: str,
    items: list[tuple[str, str, list[float]]],
    min_cosine: float,
) -> dict[int, list[dict[str, Any]]]:
    """Peers for many rows in one statement; keyed by index into `items`
    (`(memory_type, memory_key, embedding)`); every item gets a key."""
    if not items:
        return {}
    dim = len(items[0][2])
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        **vector_params([it[2] for it in items]),
        "types": [it[0] for it in items],
        "keys": [it[1] for it in items],
        "dim": dim,
        "min_cos": float(min_cosine),
    }
    # A write retires only rows inside the writer's read scope.
    in_scope = scope_and(request_scope(), params, "m")
    rows = await ch.query_all(_peers_sql_many(len(items), in_scope), params=params)
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in range(len(items))}
    for r in rows:
        q = int(r.get("qidx") or 0)
        if 0 <= q < len(items):
            out[q].append(dict(r))
    return out


async def _peers(
    ch: Any,
    *,
    org: str,
    namespace: str,
    memory_type: str,
    memory_key: str,
    embedding: list[float],
    min_cosine: float,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "mt": memory_type,
        "self_key": memory_key,
        "emb": embedding,
        "dim": len(embedding),
        "min_cos": float(min_cosine),
    }
    in_scope = scope_and(request_scope(), params, "m")
    rows = await ch.query_all(
        f"""
        SELECT toString(m.memory_id) AS memory_id, m.memory_key AS memory_key,
               m.value AS value, m.valid_at AS valid_at, m.created_at AS created_at,
               1 - cosineDistance(e.embedding, {{emb:Array(Float32)}}) AS cos
        FROM loom.memory m FINAL
        JOIN loom.memory_embeddings e FINAL
          ON  e.org = m.org AND e.namespace = m.namespace
          AND e.memory_key = m.memory_key
          AND e.user_id = m.user_id AND e.session_id = m.session_id
          AND e.agent = m.agent
        WHERE m.org = {{org:String}}
          AND m.namespace = {{ns:String}}
          AND m.memory_type = {{mt:String}}
          AND m.memory_key != {{self_key:String}}
          AND m.tombstoned_at IS NULL
          AND m.status IN ('active', 'conflicted')
          AND e.tombstoned_at IS NULL
          AND e.embedding_dim = {{dim:UInt16}}
          AND length(e.embedding) = {{dim:UInt16}}{in_scope}
        HAVING cos >= {{min_cos:Float32}}
        ORDER BY cos DESC
        LIMIT 8
        """,
        params=params,
    )
    return [dict(r) for r in rows]


async def link_value_updates(
    *,
    org: str,
    namespace: str,
    rows: list[dict[str, Any]],
    embeddings_by_key: dict[str, list[float]],
    agent: str,
    min_cosine: float = 0.75,
    concurrency: int = 6,
    ch: Any = None,
) -> UpdateReport:
    """For every freshly written memory row (dicts with memory_id, memory_key,
    memory_type, value, valid_at, created_at), find value-update peers and
    write the supersession onto the older side (`agent`: the write's, named on an owed
    record). Returns what was linked."""
    import server.memory.store as memory_repo

    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    report = UpdateReport()
    sem = asyncio.Semaphore(max(1, concurrency))

    def _eligible(row: dict[str, Any]) -> list[float] | None:
        emb = embeddings_by_key.get(str(row.get("memory_key") or ""))
        if not emb:
            return None
        # A row the bulk verdicts already stamped `duplicate` restates a
        # canonical twin in the same batch; the twin does the linking.
        if str(row.get("status") or "") == "duplicate":
            return None
        return emb

    # Peers for a batch of rows come from one statement
    # (`_peers_many`); the per-row statement remains for batch = 0 and for a
    # batch that failed. Rows are grouped by embedding dimension so one
    # statement's `dim` holds for all of them.
    from server.settings import settings as _s

    batch = int(_s.memory_write_candidate_batch)
    prefetched: dict[int, list[dict[str, Any]]] = {}
    if batch > 0:
        by_dim: dict[int, list[int]] = {}
        for i, row in enumerate(rows):
            emb = _eligible(row)
            if emb:
                by_dim.setdefault(len(emb), []).append(i)

        async def _batch(idxs: list[int]) -> None:
            items = [
                (
                    str(rows[i].get("memory_type") or "semantic"),
                    str(rows[i].get("memory_key") or ""),
                    _eligible(rows[i]) or [],
                )
                for i in idxs
            ]
            async with sem:
                got = await _peers_many(
                    ch, org=org, namespace=namespace, items=items, min_cosine=min_cosine
                )
            for j, i in enumerate(idxs):
                prefetched[i] = got.get(j, [])

        jobs = [
            _batch(idxs[k : k + batch])
            for idxs in by_dim.values()
            for k in range(0, len(idxs), batch)
        ]
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("value_updates: batched peer lookup failed (per-row): %s", res)

    async def _one(i: int, row: dict[str, Any]) -> list[UpdateLink]:
        key = str(row.get("memory_key") or "")
        emb = _eligible(row)
        if not emb:
            return []
        peers = prefetched.get(i)
        if peers is None:
            async with sem:
                try:
                    peers = await _peers(
                        ch,
                        org=org,
                        namespace=namespace,
                        memory_type=str(row.get("memory_type") or "semantic"),
                        memory_key=key,
                        embedding=emb,
                        min_cosine=min_cosine,
                    )
                except Exception as e:  # noqa: BLE001 — never block an ingest
                    log.warning("value_updates: peer lookup failed (open): %s", e)
                    report.errors += 1
                    return []
        return decide_links(
            str(row.get("memory_id") or ""),
            key,
            str(row.get("value") or ""),
            row.get("valid_at"),
            row.get("created_at"),
            peers,
        )

    results = await asyncio.gather(*(_one(i, r) for i, r in enumerate(rows)))
    report.checked = len(rows)
    links = [ln for sub in results for ln in sub]
    # One write per older row; if several newer rows supersede it, the latest
    # newer id wins (the callback reads the newer row's date from the link).
    by_older: dict[str, UpdateLink] = {}
    newer_at: dict[str, Any] = {str(r.get("memory_id") or ""): r.get("valid_at") for r in rows}
    for ln in links:
        by_older[ln.older_id] = ln
    retired: list[dict[str, Any]] = []
    if by_older:
        from server.memory.domain.lock import refuses_retirement

        # As the other automatic paths: a locked, pinned or no-longer-live older row is
        # left as it is (the peer query reads neither the lock nor the pin).
        tracker = RetirementTracker(refuses_retirement)
        try:
            for older_id, ln in by_older.items():
                inv = _as_dt(newer_at.get(ln.newer_id))
                await memory_repo.write_versions_by_ids(
                    org,
                    [older_id],
                    _supersession_update(ln.newer_id, inv),
                    namespace=namespace,
                    ch=ch,
                    skip=tracker.skip,
                    # Read-your-writes: a lagging replica's row would be rebuilt over a
                    # revision.
                    strong=True,
                    on_written=tracker.written,
                    on_unconfirmed=tracker.unconfirmed,
                )
        except Exception as e:  # noqa: BLE001 — fails open: counted in report.errors
            log.warning("value_updates: supersession write failed (open): %s", e)
            report.errors += 1
        # Only a row whose superseded version reached loom.memory: a skipped, vanished or
        # overtaken one retired nothing, and a failed mirror after the insert did. One the
        # re-read could not confirm is recorded as such and still followed (the follow
        # re-checks it); the triple plane must not close a live row's triples.
        report.links = [ln for ln in by_older.values() if ln.older_id in tracker.ids]
        maybe = tracker.ids | tracker.unconfirmed_ids
        report.follow_links = [ln for ln in by_older.values() if ln.older_id in maybe]
        for r in tracker.retired(tracker.rows):
            ln = by_older[str(r["memory_id"])]
            retired.append(
                retired_entry(r, superseded_by=ln.newer_id, superseded_by_key=ln.newer_key)
            )
    await record_or_owe(
        org=org, namespace=namespace, retired=retired, reason=VALUE_UPDATE, agent=agent
    )
    return report
