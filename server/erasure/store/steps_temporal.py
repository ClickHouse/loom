"""Erasure steps for the bi-temporal journal and the version mirrors it reads.

History is the point of these tables, so an erasure removes the rows outright:
a tombstone would leave the content in every retained slice a historical read
can reach."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.feature_modes import bitemporal_on
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import count_then_mutate

_NS = "org = {org:String} AND namespace = {ns:String}"
_UID = "org = {org:String} AND user_id = {uid:String}"
# Vectors carry no user column: a user's are the ones its assertions pointed at,
# collected before anything is deleted, and that no other assertion still uses
# (another user may hold the same text).
_USER_VECTORS = (
    "org = {org:String} AND (namespace, toString(embed_input_sha256)) "
    "IN {pairs:Array(Tuple(String, String))} "
    "AND (namespace, embed_input_sha256) NOT IN (SELECT namespace, embed_input_sha256 "
    "FROM loom.memory_temporal_assertions WHERE org = {org:String})"
)
_PAIRS_PER_DELETE = 1000
_PROMOTED_FROM_USER = (
    "org = {org:String} AND arrayExists(x -> splitByChar('/', x)[4] = {uid:String}, promoted_from)"
)

# The journal first, then what is derived from it. A projection writer re-checks
# its commits after it writes (replay, the current-row reconciler, the vector
# fill) and removes what one landing after this delete left; reads never serve
# such leftovers either way (the erasure cut, `memory.store.temporal.Erased`).
_TEMPORAL_TABLES = (
    "loom.memory_temporal_commits",
    "loom.memory_temporal_assertions",
    "loom.memory_temporal_vectors",
)
# The projection watermarks go; the `published` row, a bare sequence number, stays
# so the namespace never hands out a sequence a token or receipt already names.
_WATERMARKS = f"{_NS} AND projection != 'published'"


# The tables migration 039 creates, which exist only once the journal was turned on.
JOURNAL_TABLES = (
    *_TEMPORAL_TABLES,
    "loom.memory_temporal_watermarks",
    "loom.shared_versions",
)


async def journal_present() -> bool:
    """Whether the journal's tables exist: it is on, or an earlier period with it on
    applied its migration (their rows are erased whenever they exist). Read from the
    migration ledger the server caches: an erasure with the journal never turned on
    makes no query for it."""
    from server.core.schema.status import feature_migrated

    return bitemporal_on() or await feature_migrated("memory_bitemporal_enabled")


def journal_known() -> bool:
    """`journal_present` without a read: the flag, or the ledger as last cached."""
    from server.core.schema.status import feature_migrated_cached

    return bitemporal_on() or feature_migrated_cached("memory_bitemporal_enabled")


async def present_tables(tables: Sequence[str], ch: _CHClient | None = None) -> set[str]:
    """Which of `tables` (the journal's) the database has: all, or none."""
    return set(tables) if await journal_present() else set()


# The saga steps that erase the journal's tables, skipped while they do not exist.
JOURNAL_STEPS = frozenset({"memory_temporal.delete", "history_mirrors.delete"})


async def _delete(table: str, where: str, params: dict[str, Any], ch: _CHClient | None) -> int:
    return await count_then_mutate(
        table,
        count_where=where,
        mutation="DELETE",
        mutation_where=where,
        params=params,
        final=False,
        ch=ch,
    )


async def delete_temporal_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """The namespace's journal, assertions and vectors. Its projection watermarks
    restart at the published head (nothing below it survives), so the next write
    projects and is reconciled inline rather than waiting on a gap; the `erased`
    row keeps that head, below which no missing sequence can still land."""
    params: dict[str, Any] = {"org": org, "ns": namespace}
    have = await present_tables(JOURNAL_TABLES, ch)
    n = 0
    for t in _TEMPORAL_TABLES:
        if t in have:
            n += await _delete(t, _NS, params, ch)
    if "loom.memory_temporal_watermarks" not in have:
        return n
    n += await _delete("loom.memory_temporal_watermarks", _WATERMARKS, params, ch)
    nss = [namespace] if isinstance(namespace, str) else list(namespace)
    await (ch or _default_client()).execute(
        "INSERT INTO loom.memory_temporal_watermarks (org, namespace, projection, applied_seq) "
        "SELECT org, namespace, p, max(applied_seq) FROM loom.memory_temporal_watermarks "
        "ARRAY JOIN ['assertions', 'current', 'erased'] AS p "
        "WHERE org = {org:String} AND namespace IN {nss:Array(String)} "
        "AND projection = 'published' GROUP BY org, namespace, p",
        params={"org": org, "nss": nss},
    )
    return n


async def delete_temporal_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """A user's journal commits, assertions and vectors, in that order."""
    params: dict[str, Any] = {"org": org, "uid": user_id}
    have = await present_tables(JOURNAL_TABLES, ch)
    rows: list[dict[str, Any]] = []
    if "loom.memory_temporal_assertions" in have:
        rows = await (ch or _default_client()).query_all(
            "SELECT DISTINCT namespace, toString(embed_input_sha256) AS sha "
            f"FROM loom.memory_temporal_assertions WHERE {_UID}",
            params=params,
        )
    n = 0
    for t in ("loom.memory_temporal_commits", "loom.memory_temporal_assertions"):
        if t in have:
            n += await _delete(t, _UID, params, ch)
    if "loom.memory_temporal_vectors" not in have:
        return n
    pairs = [(str(r["namespace"]), str(r["sha"])) for r in rows]
    for i in range(0, len(pairs), _PAIRS_PER_DELETE):
        chunk = pairs[i : i + _PAIRS_PER_DELETE]
        n += await _delete(
            "loom.memory_temporal_vectors", _USER_VECTORS, {**params, "pairs": chunk}, ch
        )
    return n


async def delete_history_mirrors_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """The shared-memory version mirror."""
    if "loom.shared_versions" not in await present_tables(JOURNAL_TABLES, ch):
        return 0
    return await _delete("loom.shared_versions", _NS, {"org": org, "ns": namespace}, ch)


async def delete_history_mirrors_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """The shared versions promoted from the user's rows."""
    if "loom.shared_versions" not in await present_tables(JOURNAL_TABLES, ch):
        return 0
    params: dict[str, Any] = {"org": org, "uid": user_id}
    return await _delete("loom.shared_versions", _PROMOTED_FROM_USER, params, ch)
