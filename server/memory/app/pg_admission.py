"""Semantic admission under the Postgres authority: what a write's verdict was reached
against, and whether it still holds at its commit.

A verdict is reached in ClickHouse, outside any transaction, against what is
published there: an admission captures each scope's published watermark
(`capture`), and its commit, which every commit in the namespace advances past,
compares the generations inside the transaction. When they moved, the commits in
the gap are read from Postgres:

- `memory.set` adds them to the peer rows its verdict was reached against
  (`absorb`) and reaches it again on those rows, without reading its peers anew;
- `memory.bulk_set` commits when none of them can be a peer of any entry
  (`gap_is_clear`) and starts over otherwise.

A gap row is ruled out by its type, its key, or a vector clearly farther than the
check reaches, measured on this process's copy of the vectors it committed or on
ClickHouse's copy once the row is published; a row near the reach is never ruled
out on the local copy. A policy generation that moved (a blacklist or access change)
always sends the write back to its check.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from server.core.errors import LoomServerError
from server.memory.store import embeddings, pg_memory
from server.memory.store.rows import IdentityKey, KeyInNamespace, identity_key, key_in_namespace
from server.settings import settings

# The vectors this process's writes committed, by (key, version).
_recent: OrderedDict[tuple[*IdentityKey, int], list[float]] = OrderedDict()
# Stored vectors are lower precision than these: a row this close to the reach is
# measured on ClickHouse's copy, as a fresh check would measure it.
_LOCAL_MARGIN = 0.02


class ProjectionBehindError(LoomServerError):
    """503 — earlier commits in this namespace are not yet in ClickHouse, so a read of
    ClickHouse that must see every write the namespace committed would miss them. Retry
    after `retry_after_s`."""

    code = "PROJECTION_BEHIND"
    status_code = 503

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retry_after_s=1)


def restart_on_policy(moved: dict[tuple[str, str], int]) -> None:
    """A policy generation in `moved` (a blacklist or access change since the capture)
    restarts the write from its guards, against patterns read afresh: HeadMoved."""
    from server.memory.app.blacklist_guard import invalidate_cache

    if any(scope == "*" for _, scope in moved):
        invalidate_cache()
        raise pg_memory.HeadMoved()


def remember(key: IdentityKey, version: int, vector: list[float]) -> None:
    _recent[(*key, int(version))] = vector
    while len(_recent) > settings.pg_admission_recent_vectors:
        _recent.popitem(last=False)


def _local_vector(row: dict[str, Any]) -> list[float] | None:
    return _recent.get((*identity_key(row), int(row["version"])))


def distances(vectors: list[list[float] | None], embedding: list[float]) -> list[float | None]:
    """Each vector's cosine distance from `embedding`; None for a missing one, 2.0 (out
    of any reach) for one of another width or with no norm."""
    import numpy as np

    out: list[float | None] = [None if v is None else 2.0 for v in vectors]
    q = np.asarray(embedding, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    same = [i for i, v in enumerate(vectors) if v is not None and len(v) == len(q)]
    if not same or qn == 0.0:
        return out
    m = np.asarray([vectors[i] for i in same], dtype=np.float32)
    norms = np.linalg.norm(m, axis=1) * qn
    sims = (m @ q) / np.where(norms > 0, norms, 1.0)
    for i, sim, n in zip(same, sims.tolist(), norms.tolist(), strict=True):
        if n > 0:
            out[i] = 1.0 - float(sim)
    return out


async def wait_searchable(
    org: str,
    namespace: str,
    through: int,
    *,
    deadline: float | None = None,
    owed: int = pg_memory.SEARCHED,
) -> None:
    """Return once every revision at or below `through` has the `owed` targets (by default
    both search tables) in ClickHouse; ProjectionBehindError after
    LOOM_PG_PROJECTION_WAIT_MS, or at `deadline` (monotonic) when the caller shares one
    budget over several. A writer's lease is LOOM_PG_REQUEST_LEASE_SECONDS: past it,
    what a writer that stopped left unpublished is published here."""
    from server.memory.app.pg_delivery import deliver_namespace

    if deadline is None:
        deadline = time.monotonic() + settings.pg_projection_wait_ms / 1000
    pause = 0.005
    while await pg_memory.search_backlog(org, namespace, through, owed):
        await deliver_namespace(org, namespace)
        if not await pg_memory.search_backlog(org, namespace, through, owed):
            return
        if time.monotonic() >= deadline:
            raise ProjectionBehindError(
                f"earlier writes to {namespace!r} have not reached search yet"
            )
        await asyncio.sleep(pause)
        pause = min(pause * 2, 0.05)


async def capture(org: str, namespace: str) -> dict[tuple[str, str], int]:
    """The generations a check in (org, namespace) runs at: everything through them is
    in ClickHouse, and the commit checks what commits later. A watermark held far back
    (a writer that stopped before publishing) is first brought up to date."""
    return await catch_up(org, namespace, await pg_memory.capture(org, namespace))


async def catch_up(
    org: str, namespace: str, generations: dict[tuple[str, str], int]
) -> dict[tuple[str, str], int]:
    """`generations`, or newer ones once a watermark too far behind for a commit to
    read its gap is brought up to date: only a write with a verdict to reach waits."""
    current = await pg_memory.generation(org, namespace)
    if current - generations[(org, namespace)] <= settings.pg_admission_gap_rows:
        return generations
    await wait_searchable(org, namespace, current)
    # Only the namespace's watermark moves: the policy stays the one the guards saw.
    fresh = await pg_memory.capture(org, namespace)
    return {**generations, (org, namespace): fresh[(org, namespace)]}


async def _gap(
    captured: dict[tuple[str, str], int], moved: dict[tuple[str, str], int], org: str, ns: str
) -> tuple[int, list[dict[str, Any]]] | None:
    """(the generation the gap ends at, every row it committed), or None when a
    policy scope moved, the gap is too long to read or a payload of it was cleared. A
    seq missing from it was erased, or was an erasure's own advance: nothing of it is
    left to be a peer."""
    if set(moved) - {(org, ns)}:
        return None
    through, after = moved[(org, ns)], captured[(org, ns)]
    if through - after > settings.pg_admission_gap_rows:
        return None
    rows = await pg_memory.revisions_since(org, ns, after, through)
    return None if rows is None else (through, rows)


async def _measure(
    org: str, namespace: str, through: int, rows: list[dict[str, Any]]
) -> list[list[float] | None]:
    """The rows' vectors as ClickHouse stores them, once it holds them: the embedder's own
    model's."""
    from server.embedding import get_embedder

    await wait_searchable(org, namespace, through)
    stored = await embeddings.live_vectors(
        org,
        namespace,
        list({key_in_namespace(r) for r in rows}),
        model=get_embedder().model_name,
    )
    return [stored.get(key_in_namespace(r)) for r in rows]


def set_gap_check(
    *, memory_type: str, memory_key: str, embedding: list[float], reach: float, peer_ids: set[str]
) -> Callable[[list[dict[str, Any]]], bool]:
    """For `memory.set`'s commit (`Admission.gap_check`): a gap clears when no row of it
    is one of the verdict's peers, and every row that could be one (it counts as a
    peer, same type, another key) is out of reach on this process's copy."""
    from server.memory.app.conflicts import could_be_peer

    def check(rows: list[dict[str, Any]]) -> bool:
        if any(str(r["memory_id"]) in peer_ids for r in rows):
            return False
        near = [
            r
            for r in rows
            if r["memory_type"] == memory_type
            and r["memory_key"] != memory_key
            and could_be_peer(r)
        ]
        return all(
            d is not None and d > reach + _LOCAL_MARGIN
            for d in distances([_local_vector(r) for r in near], embedding)
        )

    return check


def batch_gap_check(
    entries: list[tuple[str, str, list[float], float]],
) -> Callable[[list[dict[str, Any]]], bool]:
    """For a batch's commit: a gap clears when every row of another key and the same
    type as an entry is out of that entry's reach on this process's copy."""

    def check(rows: list[dict[str, Any]]) -> bool:
        return _clear([_local_vector(r) for r in rows], rows, entries)

    return check


async def absorb(
    *,
    captured: dict[tuple[str, str], int],
    moved: dict[tuple[str, str], int],
    org: str,
    namespace: str,
    memory_type: str,
    memory_key: str,
    embedding: list[float],
    max_distance: float,
    peers: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], int], list[dict[str, Any]]] | None:
    """The generations and peer rows a `memory.set` verdict is reached again at: each
    key the gap committed replaces what `peers` held for it, and joins them when it is
    a peer (same type, another key, within `max_distance` on ClickHouse's copy). None
    when the write must read its peers anew: a policy change, a long gap, or a peer
    dropped from a full list (a farther row may take its place)."""
    from server.memory.app.conflicts import could_be_peer, trim_peers
    from server.memory.store.conflict_candidates import PEER_LIMIT

    gap = await _gap(captured, moved, org, namespace)
    if gap is None:
        return None
    through, rows = gap
    newest: dict[KeyInNamespace, dict[str, Any]] = {}
    for r in rows:
        ident = key_in_namespace(r)
        if ident not in newest or int(r["version"]) >= int(newest[ident]["version"]):
            newest[ident] = r
    # Every peer the gap wrote is replaced by its newest row, a delete, a retirement or
    # an unpin included; only a row that is still a peer joins again.
    kept = [p for p in peers if key_in_namespace(p) not in newest]
    if len(peers) >= PEER_LIMIT and len(kept) < len(peers):
        return None
    candidates = [
        r
        for r in newest.values()
        if r["memory_type"] == memory_type and r["memory_key"] != memory_key and could_be_peer(r)
    ]
    local = distances([_local_vector(r) for r in candidates], embedding)
    candidates = [
        r
        for r, d in zip(candidates, local, strict=True)
        if d is None or d <= max_distance + _LOCAL_MARGIN
    ]
    if candidates:
        stored = distances(await _measure(org, namespace, through, candidates), embedding)
        for r, d in zip(candidates, stored, strict=True):
            if d is None:
                return None  # published, yet no vector: read the peers anew
            if d <= max_distance:
                kept.append(_peer_row(r, d))
    return {**captured, (org, namespace): through}, trim_peers(kept)


async def gap_is_clear(
    *,
    captured: dict[tuple[str, str], int],
    moved: dict[tuple[str, str], int],
    org: str,
    namespace: str,
    entries: list[tuple[str, str, list[float], float]],
) -> dict[tuple[str, str], int] | None:
    """For a batch: the generations to commit at when no row of the gap can be a peer
    of any entry (`(memory_type, memory_key, embedding, max_distance)`), else None and
    the batch starts over. A batch keeps no list of its verdicts' peers, so a row that
    left them (a delete, a retirement) is judged by its vector too, and one with none
    starts the batch over."""
    gap = await _gap(captured, moved, org, namespace)
    if gap is None:
        return None
    through, rows = gap
    if not _clear([_local_vector(r) for r in rows], rows, entries):
        if not _clear(await _measure(org, namespace, through, rows), rows, entries):
            return None
    return {**captured, (org, namespace): through}


def _clear(
    vectors: list[list[float] | None],
    rows: list[dict[str, Any]],
    entries: list[tuple[str, str, list[float], float]],
) -> bool:
    """Whether every row is out of every entry's reach by the margin; a row with no
    vector to measure is not. One matrix product for the whole gap."""
    pairs = [
        (i, j)
        for i, r in enumerate(rows)
        for j, (memory_type, memory_key, _, _) in enumerate(entries)
        if r["memory_type"] == memory_type and r["memory_key"] != memory_key
    ]
    if not pairs:
        return True
    width = len(entries[0][2])
    if any(len(e[2]) != width for e in entries):
        return False
    for i in {i for i, _ in pairs}:
        v = vectors[i]
        if v is None or len(v) != width:
            return False
    import numpy as np

    # A row no entry pairs with may hold no vector, or a stub's empty one: it is filled
    # only to keep the matrix whole, and its distances are never read.
    m = _unit(
        np.asarray(
            [v if v is not None and len(v) == width else [0.0] * width for v in vectors],
            dtype=np.float32,
        )
    )
    e = _unit(np.asarray([x[2] for x in entries], dtype=np.float32))
    dist = 1.0 - m @ e.T
    reach = np.asarray([x[3] for x in entries], dtype=np.float32) + _LOCAL_MARGIN
    return all(dist[i, j] > reach[j] for i, j in pairs)


def _unit(m: Any) -> Any:
    import numpy as np

    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms > 0, norms, 1.0)


def _peer_row(r: dict[str, Any], dist: float) -> dict[str, Any]:
    """A committed row in the shape `check_on_commit` reads its peers in."""
    return {
        "memory_id": str(r["memory_id"]),
        "memory_key": r["memory_key"],
        "value": r["value"],
        "pinned": int(bool(r.get("pinned"))),
        "agent": r["agent"],
        "user_id": r["user_id"],
        "session_id": r["session_id"],
        "run_id": r.get("run_id") or "-",
        "valid_at": r.get("valid_at"),
        "dist": dist,
    }
