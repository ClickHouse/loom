"""Row-level integrity invariants of the memory plane.

Four statements the state probe (`loom.integrity.*`, slow clock) and
`loom integrity-repair` share, as the erasure invariants do: the gauge and
the operator's closing line read the same SQL, so they cannot disagree.

* **dim-0 with a vector** — a `memory_embeddings` row whose `embedding` holds
  a vector while `embedding_dim = 0`. `embedding_dim > 0` is a predicate on
  every search leg (`memory/domain/search_sql.py`, `memory_search/`), so the
  row is stored, embedded, and never returned. A re-emit that carries the
  vector and defaults the dim produces one. The repair re-emits each with
  `embedding_dim = length(embedding)` — a new RMT version, never a mutation.
* **tombstone / status drift** — a `loom.memory` row with `tombstoned_at`
  set and `status != 'tombstoned'`. Readers key on `tombstoned_at`, so the
  row is dead; the status column says otherwise to everything that groups
  or filters by it. An `ALTER UPDATE` tombstone (`memory.clear`) leaves a
  row in this state.
* **active past `valid_to`** — an `active` row whose own stated validity
  ended and nothing retired it. Reported, not repaired: the right successor
  state is the temporal plane's call (valid_to semantics), not a blanket
  status flip.
* **high with an embedding** — a live `sensitivity = 'high'` memory with a
  live `memory_embeddings` row. A `high` row is never embedded; the
  sensitivity sweep tombstones such an embedding row.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client

# Rows search can never return: a vector is present, the dim says none.
DIM0_WITH_VECTOR_SQL = """
SELECT org, namespace, agent, user_id, session_id, memory_key, embedding_model,
       length(embedding) AS vector_len
FROM loom.memory_embeddings FINAL
WHERE tombstoned_at IS NULL AND embedding_dim = 0 AND length(embedding) > 0
"""

# Dead by tombstoned_at, alive by status.
TOMBSTONE_STATUS_DRIFT_SQL = """
SELECT org, namespace, memory_key, status, tombstoned_at
FROM loom.memory FINAL
WHERE tombstoned_at IS NOT NULL AND status != 'tombstoned'
"""

# Still active after its own end date.
ACTIVE_PAST_VALID_TO_SQL = """
SELECT org, namespace, memory_key, valid_to
FROM loom.memory FINAL
WHERE tombstoned_at IS NULL AND status = 'active'
  AND valid_to IS NOT NULL AND valid_to < now()
"""

# The sensitivity invariant as a count: a live `high` row with a live
# embedding row. `high` rows are never embedded; the embedding worker's
# missing-row reconcile is the path most likely to break this (it would
# re-embed a `high` row right after the sweep tombstoned its embedding).
HIGH_WITH_EMBEDDING_SQL = """
SELECT m.org AS org, m.namespace AS namespace, m.memory_key AS memory_key
FROM (SELECT org, namespace, agent, user_id, session_id, memory_key
      FROM loom.memory FINAL
      WHERE tombstoned_at IS NULL AND sensitivity = 'high') AS m
INNER JOIN (SELECT org, namespace, agent, user_id, session_id, memory_key
            FROM loom.memory_embeddings FINAL
            WHERE tombstoned_at IS NULL) AS e
  ON m.org = e.org AND m.namespace = e.namespace AND m.agent = e.agent
     AND m.user_id = e.user_id AND m.session_id = e.session_id
     AND m.memory_key = e.memory_key
"""

GAUGES: tuple[str, ...] = (
    "dim0_with_vector",
    "tombstone_status_drift",
    "active_past_valid_to",
    "high_with_embedding",
)


def integrity_counts_sql() -> str:
    """The four counts as one statement — the probe embeds it, the CLI runs it."""
    return (
        f"SELECT (SELECT count() FROM ({DIM0_WITH_VECTOR_SQL})) AS dim0_with_vector, "
        f"(SELECT count() FROM ({TOMBSTONE_STATUS_DRIFT_SQL})) AS tombstone_status_drift, "
        f"(SELECT count() FROM ({ACTIVE_PAST_VALID_TO_SQL})) AS active_past_valid_to, "
        f"(SELECT count() FROM ({HIGH_WITH_EMBEDDING_SQL})) AS high_with_embedding"
    )


async def integrity_counts(*, ch: _CHClient | None = None) -> dict[str, int]:
    ch = ch or _default_client()
    row = await ch.query_one(integrity_counts_sql())
    return {k: int((row or {}).get(k) or 0) for k in GAUGES}


def _scope(org: str | None, namespace: str | None) -> tuple[str, dict[str, Any]]:
    where = ""
    params: dict[str, Any] = {}
    if org:
        where += " AND org = {org:String}"
        params["org"] = org
    if namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = namespace
    return where, params


async def dim0_with_vector_rows(
    *,
    limit: int,
    org: str | None = None,
    namespace: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Every persisted column of up to `limit` dim-0-with-vector rows, so the
    repair can re-emit them whole (omitting a column lands its DEFAULT under
    FINAL). `SELECT *` excludes the MATERIALIZED lexical columns."""
    ch = ch or _default_client()
    where, params = _scope(org, namespace)
    params["lim"] = int(limit)
    return await ch.query_all(
        f"""
        SELECT * FROM loom.memory_embeddings FINAL
        WHERE tombstoned_at IS NULL AND embedding_dim = 0 AND length(embedding) > 0
          {where}
        ORDER BY org, namespace, memory_key
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def repair_dim0_rows(
    rows: list[dict[str, Any]], *, ch: _CHClient | None = None, dry_run: bool = False
) -> int:
    """Re-emit each row with `embedding_dim = length(embedding)` at its version
    + 1: the vector is the truth, the dim was the omission. Returns rows
    re-emitted (or, with `dry_run`, the number that would be)."""
    from server.memory.store import (
        EMBEDDING_PERSISTED_COLS,
        build_embedding_row,
        insert_embedding_rows,
        reemit_version,
    )

    out: list[dict[str, Any]] = []
    for r in rows:
        vec = list(r.get("embedding") or [])
        if not vec:
            continue
        out.append(
            build_embedding_row(
                **(
                    {c: val for c, val in r.items() if c in EMBEDDING_PERSISTED_COLS}
                    | {"embedding": vec, "embedding_dim": len(vec), "version": reemit_version(r)}
                )
            )
        )
    if dry_run or not out:
        return len(out)
    ch = ch or _default_client()
    await insert_embedding_rows(out, ch=ch)
    from server.memory.store.embeddings import retire_copies_under_unembedded_heads

    await retire_copies_under_unembedded_heads(out, ch=ch)
    return len(out)


async def drift_sample(
    *,
    limit: int,
    org: str | None = None,
    namespace: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Per (org, namespace, status) counts of the tombstone/status drift, for
    the CLI's listing — where the `ALTER UPDATE` tombstones sit."""
    ch = ch or _default_client()
    where, params = _scope(org, namespace)
    params["lim"] = int(limit)
    return await ch.query_all(
        f"""
        SELECT org, namespace, status, count() AS n
        FROM loom.memory FINAL
        WHERE tombstoned_at IS NOT NULL AND status != 'tombstoned' {where}
        GROUP BY org, namespace, status
        ORDER BY n DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


__all__ = [
    "ACTIVE_PAST_VALID_TO_SQL",
    "DIM0_WITH_VECTOR_SQL",
    "GAUGES",
    "HIGH_WITH_EMBEDDING_SQL",
    "TOMBSTONE_STATUS_DRIFT_SQL",
    "dim0_with_vector_rows",
    "drift_sample",
    "integrity_counts",
    "integrity_counts_sql",
    "repair_dim0_rows",
]
