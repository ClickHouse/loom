"""The one-statement fill of a namespace's Matryoshka-prefix twin from its
served rows (see `server.code.domain.index.prefix_twin`)."""

from __future__ import annotations

from typing import Any

from server.code.domain.index.prefix_twin import TWIN_COLUMNS
from server.core.vectors.code_tables import code_vector_table, mapping
from server.core.vectors.prefix import parse_prefix_label


def fill_sql(table: str, dim: int) -> str:
    """The one-statement fill of a namespace's twin from its served rows:
    live, embedded rows not yet in the twin, projected inside ClickHouse.
    The column list and the select list have the same arity; no select
    expression is aliased to a column name (a `... AS embedding_model` would
    shadow the column in WHERE and select nothing)."""
    cols = ", ".join(TWIN_COLUMNS)
    return f"""INSERT INTO {table} ({cols})
SELECT org, namespace, {{label:String}}, path, chunk_id, scope, kind, start_line, end_line,
       CAST(if(L2Norm(head) = 0, head, arrayMap(x -> x / L2Norm(head), head)) AS Array(BFloat16)),
       toUInt16({{dim:UInt16}}), indexed_commit, tombstoned_at, created_at, version
FROM (
    SELECT org, namespace, path, chunk_id, scope, kind, start_line, end_line,
           indexed_commit, tombstoned_at, created_at, version,
           arraySlice(CAST(embedding AS Array(Float32)), 1, {{dim:UInt16}}) AS head
    FROM loom.code_chunks FINAL
    WHERE org = {{org:String}} AND namespace = {{ns:String}}
      AND embedding_model = {{base:String}} AND embedding_dim > 0
      AND tombstoned_at IS NULL
      AND (path, chunk_id) NOT IN (
          SELECT path, chunk_id FROM {table} FINAL
          WHERE org = {{org:String}} AND namespace = {{ns:String}}
            AND embedding_model = {{label:String}} AND tombstoned_at IS NULL
      )
)"""


def is_prefix_twin(label: str) -> bool:
    """Whether `label` names a configured prefix twin (LOOM_CODE_VECTOR_TABLES)."""
    return parse_prefix_label(label) is not None and label in mapping()


async def fill_twin(ch: Any, org: str, ns: str, label: str) -> int:
    """Fill `label`'s twin for one namespace; returns the twin's live row
    count afterwards. Raises when the label is not a configured prefix twin."""
    parsed = parse_prefix_label(label)
    if parsed is None or label not in mapping():
        raise ValueError(f"{label!r} is not a configured prefix twin (LOOM_CODE_VECTOR_TABLES)")
    dim, base = parsed
    table = code_vector_table(label)
    params = {"org": org, "ns": ns, "label": label, "base": base, "dim": dim}
    await ch.execute(fill_sql(table, dim), params)
    row = await ch.query_one(
        f"""
        SELECT count() AS n FROM {table} FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND embedding_model = {{label:String}} AND embedding_dim > 0
          AND tombstoned_at IS NULL
        """,
        params,
    )
    return int(row["n"]) if row else 0
