"""What a user-scope step reads before it mutates.

ClickHouse refuses a mutation whose WHERE holds a subquery on a replicated table
(code 36), so a step reads the subquery of the predicate its census row counts with
and binds what it found, a slice at a time (`ch_params.chunks`, which measures a tuple
as the driver renders it).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.ch_params import chunks
from server.core.repo_base import _CHClient
from server.erasure.store.predicates import USER_KEY_LIST, USER_KEYS_SUBQ


async def user_key_pairs(org: str, user_id: str, *, ch: _CHClient) -> list[tuple[str, str]]:
    """The user's (namespace, memory_key) pairs, tombstoned rows included."""
    rows = await ch.query_all(
        f"SELECT DISTINCT namespace, memory_key FROM {USER_KEYS_SUBQ}",
        params={"org": org, "uid": user_id},
    )
    return sorted((str(r["namespace"]), str(r["memory_key"])) for r in rows)


async def user_keys(org: str, user_id: str, *, ch: _CHClient) -> list[str]:
    """The user's memory keys in any namespace of the org, tombstoned rows included."""
    row = await ch.query_one(f"SELECT {USER_KEY_LIST} AS keys", params={"org": org, "uid": user_id})
    return sorted(str(k) for k in (row or {}).get("keys") or [])


def slices_or_one_empty[T](values: Sequence[T]) -> list[list[T]]:
    """`values` in slices (`chunks`), or one empty slice when there are none: for a
    predicate that also matches rows by another column, which must run even then."""
    return list(chunks(values)) or [[]]


def hashes(rows: Sequence[dict[str, Any]]) -> list[int]:
    """The sorted `h` of `rows`, for an `Array(UInt64)` parameter. A step binds the
    `cityHash64` of a key that holds stored free text (an entity name, a URI), which
    nothing bounds: the key itself can outgrow a form field."""
    return sorted({int(r["h"]) for r in rows})
