"""What a user-scope step reads before it mutates.

ClickHouse refuses a mutation whose WHERE holds a subquery on a replicated table
(code 36), so a step reads the subquery of the predicate its census row counts with
and binds what it found, a slice at a time (`ch_params.chunks`, which measures a tuple
as the driver renders it).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

from server.core.ch_params import FIELD_BYTES, chunks
from server.core.repo_base import _CHClient
from server.erasure.store.predicates import (
    SUMMARY_ROWS_FROM,
    USER_KEYS_SUBQ,
    USER_QUOTED_KEY_LIST,
)


async def user_key_pairs(org: str, user_id: str, *, ch: _CHClient) -> list[tuple[str, str]]:
    """The user's (namespace, memory_key) pairs, tombstoned rows included."""
    rows = await ch.query_all(
        f"SELECT DISTINCT namespace, memory_key FROM {USER_KEYS_SUBQ}",
        params={"org": org, "uid": user_id},
    )
    return sorted((str(r["namespace"]), str(r["memory_key"])) for r in rows)


async def summary_slices(org: str, user_id: str, *, ch: _CHClient) -> list[dict[str, Any]]:
    """The bound values of `CONSOLIDATED_IN`, a slice of the user's (namespace, key) at a
    time. Pairs are measured without the `src:` prefix they are bound with: half a field
    leaves room for it."""
    pairs = await user_key_pairs(org, user_id, ch=ch)
    return [consolidated_params(part) for part in chunks(pairs, limit=FIELD_BYTES // 2)]


def consolidated_params(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """The bound values of `CONSOLIDATED_IN` for a slice of the user's (namespace, key)."""
    tagged = [(ns, "src:" + key) for ns, key in pairs]
    return {"src": sorted({tag for _, tag in tagged}), "pairs": tagged}


class SummaryRows(NamedTuple):
    """The summaries built from the user's memories, tombstoned rows included, as the
    columns their derived rows name them by."""

    keys: list[tuple[str, str]]  # (namespace, memory_key)
    identities: list[tuple[str, str, str, str]]  # (namespace, agent, user_id, memory_key)
    ids: list[str]  # memory_id


async def user_summary_rows(org: str, user_id: str, *, ch: _CHClient) -> SummaryRows:
    rows = await ch.query_all(
        "SELECT DISTINCT toString(namespace) AS ns, agent, user_id, memory_key, "
        f"toString(memory_id) AS id {SUMMARY_ROWS_FROM}",
        params={"org": org, "uid": user_id},
    )
    found = [
        (str(r["ns"]), str(r["agent"]), str(r["user_id"]), str(r["memory_key"]), str(r["id"]))
        for r in rows
    ]
    return SummaryRows(
        keys=sorted({(ns, key) for ns, _, _, key, _ in found}),
        identities=sorted({(ns, a, u, key) for ns, a, u, key, _ in found}),
        ids=sorted({i for *_, i in found}),
    )


async def user_quoted_keys(org: str, user_id: str, *, ch: _CHClient) -> list[str]:
    """The keys an answer that quoted the user's memories names: the user's own and
    those of the summaries built from them, tombstoned rows included."""
    row = await ch.query_one(
        f"SELECT {USER_QUOTED_KEY_LIST} AS keys", params={"org": org, "uid": user_id}
    )
    return sorted({str(k) for k in (row or {}).get("keys") or []})


def slices_or_one_empty[T](values: Sequence[T]) -> list[list[T]]:
    """`values` in slices (`chunks`), or one empty slice when there are none: for a
    predicate that also matches rows by another column, which must run even then."""
    return list(chunks(values)) or [[]]


def hashes(rows: Sequence[dict[str, Any]]) -> list[int]:
    """The sorted `h` of `rows`, for an `Array(UInt64)` parameter. A step binds the
    `cityHash64` of a key that holds stored free text (an entity name, a URI), which
    nothing bounds: the key itself can outgrow a form field."""
    return sorted({int(r["h"]) for r in rows})
