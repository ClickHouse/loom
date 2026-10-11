"""The statements that move stored entity text to a new canonical form: the graph tables
rewrite the changed rows under their new text (the text is in their sort keys) and drop
the old ones; `loom.memory` and its mirror re-emit each row one version up, so a write
that lands meanwhile still wins."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from server.core.repo_base import _CHClient, _default_client
from server.memory.store.inserts import insert_embedding_rows, insert_memory_versions
from server.memory.store.reads import memory_rows_by_identity_keys
from server.memory.store.rows import (
    EMBEDDING_PERSISTED_COLS,
    build_embedding_row,
    build_memory_version_row,
)
from server.memory.store.sql import _identity_key

# (table, entity text columns, whether the two columns are an undirected pair in order)
GRAPH_TABLES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("loom.entity_canonical_map", ("mention_text", "canonical_text"), False),
    ("loom.entity_communities", ("entity",), False),
    ("loom.entity_edge_memories", ("entity_a", "entity_b"), True),
    ("loom.entity_edges", ("entity_a", "entity_b"), True),
    ("loom.entity_summaries", ("entity",), False),
    ("loom.entity_triple_memories", ("subject", "object"), False),
    ("loom.entity_triples", ("subject", "object"), False),
    ("loom.entity_vectors", ("entity_text",), False),
    ("loom.derived_facts", ("subject", "object", "category"), False),
)
MEMORY_TABLES = ("loom.memory", "loom.memory_embeddings")
_CHUNK = 500
_SYNC = "SETTINGS lightweight_deletes_sync = 2"


def _chunks(olds: Sequence[str]) -> list[list[str]]:
    return [list(olds[i : i + _CHUNK]) for i in range(0, len(olds), _CHUNK)]


async def graph_values(*, ch: _CHClient | None = None) -> set[str]:
    ch = ch or _default_client()
    out: set[str] = set()
    for table, cols, _pair in GRAPH_TABLES:
        for col in cols:
            rows = await ch.query_all(f"SELECT DISTINCT toString({col}) AS v FROM {table}")
            out.update(str(r["v"]) for r in rows)
    return out


async def memory_values(*, ch: _CHClient | None = None) -> set[str]:
    ch = ch or _default_client()
    out: set[str] = set()
    for table in MEMORY_TABLES:
        rows = await ch.query_all(f"SELECT DISTINCT arrayJoin(entities) AS v FROM {table}")
        out.update(str(r["v"]) for r in rows)
    return out


def _naming(cols: Sequence[str]) -> str:
    return " OR ".join(f"{c} IN {{olds:Array(String)}}" for c in cols)


async def count_graph(
    table: str, cols: Sequence[str], olds: Sequence[str], *, ch: _CHClient | None = None
) -> int:
    ch = ch or _default_client()
    n = 0
    for part in _chunks(olds):
        row = await ch.query_one(
            f"SELECT count() AS n FROM {table} WHERE {_naming(cols)}", params={"olds": part}
        )
        n += int((row or {}).get("n") or 0)
    return n


async def rename_graph(
    table: str,
    cols: Sequence[str],
    pair: bool,
    olds: Sequence[str],
    news: Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> list[tuple[str, str]]:
    """Rewrite `table`'s rows naming `olds` under `news` and drop the old rows; the
    (org, namespace) pairs touched. `news` hold no value of `olds` (canonical text is a
    fixed point), so the delete never meets a rewritten row."""
    ch = ch or _default_client()
    swap = "{olds:Array(String)}, {news:Array(String)}"
    mapped = {c: f"transform(toString({c}), {swap})" for c in cols}
    if pair:
        a, b = cols
        mapped = {a: f"least({mapped[a]}, {mapped[b]})", b: f"greatest({mapped[a]}, {mapped[b]})"}
    replace = ", ".join(f"{expr} AS {c}" for c, expr in mapped.items())
    touched: set[tuple[str, str]] = set()
    by_old = dict(zip(olds, news, strict=True))
    for part in _chunks(olds):
        params = {"olds": part, "news": [by_old[o] for o in part]}
        where = _naming(cols)
        rows = await ch.query_all(
            f"SELECT DISTINCT org, namespace FROM {table} WHERE {where}", params=params
        )
        if not rows:
            continue
        touched.update((str(r["org"]), str(r["namespace"])) for r in rows)
        await ch.execute(
            f"INSERT INTO {table} SELECT * REPLACE ({replace}) "
            f"FROM (SELECT * FROM {table} WHERE {where})",
            params=params,
        )
        await ch.execute(f"DELETE FROM {table} WHERE {where} {_SYNC}", params=params)
    return sorted(touched)


async def rebuild_degrees(
    namespaces: Sequence[tuple[str, str]], *, ch: _CHClient | None = None
) -> None:
    """`loom.entity_degree` of these namespaces recomputed from their edges: the edge
    rewrite added the new names' degrees but cannot take the old rows' back."""
    if not namespaces:
        return
    ch = ch or _default_client()
    params = {"nss": [tuple(n) for n in namespaces]}
    scope = "(org, namespace) IN {nss:Array(Tuple(String, String))}"
    await ch.execute(f"DELETE FROM loom.entity_degree WHERE {scope} {_SYNC}", params=params)
    await ch.execute(
        "INSERT INTO loom.entity_degree SELECT org, namespace, "
        "arrayJoin([entity_a, entity_b]) AS entity, co_occur AS degree "
        f"FROM loom.entity_edges WHERE {scope}",
        params=params,
    )


_LIVE_NAMING = "FINAL WHERE tombstoned_at IS NULL AND hasAny(entities, {olds:Array(String)})"


async def count_memory(table: str, olds: Sequence[str], *, ch: _CHClient | None = None) -> int:
    ch = ch or _default_client()
    n = 0
    for part in _chunks(olds):
        row = await ch.query_one(
            f"SELECT count() AS n FROM {table} {_LIVE_NAMING}", params={"olds": part}
        )
        n += int((row or {}).get("n") or 0)
    return n


async def reemit_memory(
    olds: Sequence[str],
    rename: Callable[[list[str]], list[str]],
    *,
    limit: int = 1000,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Re-emit every live `loom.memory` and `loom.memory_embeddings` row naming `olds`
    with `rename(entities)`, one version above the row read."""
    ch = ch or _default_client()
    written = dict.fromkeys(MEMORY_TABLES, 0)
    for part in _chunks(olds):
        params = {"olds": part}
        while keys := [
            _identity_key(r)
            for r in await ch.query_all(
                "SELECT org, namespace, agent, user_id, session_id, memory_key "
                f"FROM loom.memory {_LIVE_NAMING} LIMIT {int(limit)}",
                params=params,
            )
        ]:
            existing = await memory_rows_by_identity_keys(keys, ch=ch)
            rows = [
                build_memory_version_row(
                    m, entities=rename(list(m.entities or [])), version=int(m.version) + 1
                )
                for m in existing.values()
            ]
            if not rows:
                break
            await insert_memory_versions(rows, ch=ch)
            written["loom.memory"] += len(rows)
        while emb := await ch.query_all(
            f"SELECT * FROM loom.memory_embeddings {_LIVE_NAMING} LIMIT {int(limit)}",
            params=params,
        ):
            await insert_embedding_rows(
                [
                    build_embedding_row(
                        **(
                            {k: v for k, v in r.items() if k in EMBEDDING_PERSISTED_COLS}
                            | {
                                "entities": rename(list(r["entities"] or [])),
                                "version": int(r["version"]) + 1,
                            }
                        )
                    )
                    for r in emb
                ],
                ch=ch,
            )
            written["loom.memory_embeddings"] += len(emb)
    return written
