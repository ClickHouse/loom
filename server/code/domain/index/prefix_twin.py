"""A Matryoshka-prefix twin of the chunk vectors.

The served embedding model is Matryoshka-trained: the first `dim` coordinates of one of
its vectors, renormalised, are a valid embedding of the same text in a smaller space. A
twin table keyed like `loom.code_chunks` holds that prefix for every served-label row
(`loom.code_chunks_prefix`; no fixed width, one label per width) under
the label `prefix:<dim>:<served model>`, mapped by `LOOM_CODE_VECTOR_TABLES` like any
second-model table. The vector leg shortlists on the twin — 1 KB a row at the served 512
dims instead of 6 KB — and the served vectors rescore the shortlist exactly by key.

Two writers keep the twin the served rows' mirror: every chunk insert fans
a projected copy of each served-label row out to its twin (tombstones and
placeholders included, so the twin never keeps a row the served table
dropped), and `loom code vector-twin` fills a namespace's live rows in one
INSERT ... SELECT inside ClickHouse, no re-embed and no provider call (the
store's `prefix_twin` module).
"""

from __future__ import annotations

from typing import Any

from server.core.vectors.prefix import parse_prefix_label, project

# The twin's columns: the sort key, what the scope predicates and the
# shortlist read need, the vector, and the row lifecycle columns.
TWIN_COLUMNS: tuple[str, ...] = (
    "org",
    "namespace",
    "embedding_model",
    "path",
    "chunk_id",
    "scope",
    "kind",
    "start_line",
    "end_line",
    "embedding",
    "embedding_dim",
    "indexed_commit",
    "tombstoned_at",
    "created_at",
    "version",
)


def prefix_twins_for(base_label: str) -> list[tuple[str, int, str]]:
    """The configured twins of a served label: `(twin label, dim, table)` for
    every `prefix:<dim>:<base_label>` in the vector-table map."""
    from server.core.vectors.code_tables import mapping

    out: list[tuple[str, int, str]] = []
    for label, (table, _dim) in mapping().items():
        parsed = parse_prefix_label(label)
        if parsed is not None and parsed[1] == base_label:
            out.append((label, parsed[0], table))
    return out


def twin_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The projected twin rows of a chunk insert, grouped by twin table.
    Rows whose label has no configured twin contribute nothing."""
    by_table: dict[str, list[dict[str, Any]]] = {}
    cache: dict[str, list[tuple[str, int, str]]] = {}
    for r in rows:
        base = str(r.get("embedding_model") or "")
        twins = cache.get(base)
        if twins is None:
            twins = cache[base] = prefix_twins_for(base)
        for label, dim, table in twins:
            vec = project(list(r.get("embedding") or []), dim)
            t = {c: r.get(c) for c in TWIN_COLUMNS if c in r}
            t["embedding_model"] = label
            t["embedding"] = vec
            t["embedding_dim"] = dim if vec else 0
            by_table.setdefault(table, []).append(t)
    return by_table
