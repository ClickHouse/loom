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
from server.memory.app.auto_resolution import (
    VALUE_UPDATE,
    RetirementTracker,
    record_or_owe,
    retired_entry,
)
from server.memory.domain.conflicts import values_differ

log = logging.getLogger(__name__)

# Alphanumeric tokens that contain at least one letter ("5k" counts, "2023" does not).
# a token is read from its first character only
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
    peers: list[dict[str, Any]],
) -> list[UpdateLink]:
    """Pure half: which peers form a value-update pair with the new row, and
    which side is older. `peers` rows carry memory_id, memory_key, value,
    valid_at, cos. Only a pair dated on different days is an update; the later
    `valid_at` is the newer side."""
    out: list[UpdateLink] = []
    n_at = _as_dt(new_at)
    for p in peers:
        if str(p.get("memory_key") or "") == new_key:
            continue
        if not is_value_update(new_text, str(p.get("value") or "")):
            continue
        p_at = _as_dt(p.get("valid_at"))
        # An update happens ACROSS time: the newer side must be dated strictly
        # later (by calendar day). Two rows on the same date, or two undated
        # rows, are an enumeration, not an update ("Article 12 …" and
        # "Article 13 …", two products' refill-pack prices, "key 08222 is
        # 251" and "key 17256 is 123"). Same-day rows fall to the conflict
        # detector's domain.
        if n_at is None or p_at is None or n_at.date() == p_at.date():
            continue
        new_is_newer = n_at > p_at
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


# The most value-update peers weighed per row.
_PEERS_PER_ROW = 8


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
    """The row's nearest live peers of its type at cosine `min_cosine` or more, inside
    the writer's read scope, nearest first: the conflict check's two-stage read
    (`conflicts.nearest_peer_rows`): codes-first, falling back to the full-width join
    only past `_PEER_MAX_K` candidates."""
    from server.memory.app.conflicts import nearest_peer_rows

    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "mt": memory_type,
        "self_key": memory_key,
        "emb": embedding,
        "dim": len(embedding),
        "max_dist": 1.0 - float(min_cosine),
    }
    rows = await nearest_peer_rows(ch, params, request_scope(), limit=_PEERS_PER_ROW)
    return [
        {
            "memory_id": str(r["memory_id"]),
            "memory_key": r["memory_key"],
            "value": r["value"],
            "valid_at": r["valid_at"],
            "cos": 1.0 - float(r["dist"]),
        }
        for r in rows
    ]


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
    memory_type, value, valid_at), find value-update peers and
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

    async def _one(row: dict[str, Any]) -> list[UpdateLink]:
        key = str(row.get("memory_key") or "")
        emb = _eligible(row)
        if not emb:
            return []
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
            peers,
        )

    results = await asyncio.gather(*(_one(r) for r in rows))
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
