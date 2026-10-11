"""The statements that move stored entity text to a new canonical form, one (org,
namespace) at a time. Every step is safe to repeat: a graph row is copied under its new
text only where the copy is not already there (`EXCEPT ALL`) before the old row is
deleted; `loom.memory` and its mirror re-emit a row one version up, so a write that
lands meanwhile still wins; entity degrees are rebuilt wherever they disagree with the
edges."""

from __future__ import annotations

from collections import defaultdict
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

Scope = tuple[str, str]

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
_SCOPE = "org = {org:String} AND namespace = {ns:String}"
_LIVE_NAMING = (
    f"FINAL WHERE {_SCOPE} AND tombstoned_at IS NULL AND hasAny(entities, {{olds:Array(String)}})"
)


def _chunks(values: Sequence[str]) -> list[list[str]]:
    return [list(values[i : i + _CHUNK]) for i in range(0, len(values), _CHUNK)]


def _naming(cols: Sequence[str], key: str) -> str:
    return "(" + " OR ".join(f"{c} IN {{{key}:Array(String)}}" for c in cols) + ")"


async def stored_values(*, ch: _CHClient | None = None) -> dict[Scope, set[str]]:
    """(org, namespace) -> every entity text its graph and memory rows hold."""
    ch = ch or _default_client()
    reads = [
        f"SELECT DISTINCT org, namespace, toString({col}) AS v FROM {table}"
        for table, cols, _pair in GRAPH_TABLES
        for col in cols
    ]
    reads += [
        f"SELECT DISTINCT org, namespace, arrayJoin(entities) AS v FROM {table}"
        for table in MEMORY_TABLES
    ]
    out: dict[Scope, set[str]] = defaultdict(set)
    for sql in reads:
        for r in await ch.query_all(sql):
            out[(str(r["org"]), str(r["namespace"]))].add(str(r["v"]))
    return dict(out)


async def naming_texts(
    scope: Scope, values: Sequence[str], *, ch: _CHClient | None = None
) -> dict[str, list[str]]:
    """value -> the values of the scope's live memories whose entities name it."""
    ch = ch or _default_client()
    out: dict[str, list[str]] = defaultdict(list)
    for part in _chunks(values):
        rows = await ch.query_all(
            "SELECT arrayJoin(arrayIntersect(entities, {olds:Array(String)})) AS v, value "
            f"FROM loom.memory {_LIVE_NAMING}",
            params={"org": scope[0], "ns": scope[1], "olds": part},
        )
        for r in rows:
            out[str(r["v"])].append(str(r["value"]))
    return dict(out)


async def count_graph(
    table: str,
    cols: Sequence[str],
    scope: Scope,
    olds: Sequence[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    return await _count(f"{table} WHERE {_SCOPE} AND {_naming(cols, 'olds')}", scope, olds, ch)


async def count_memory(
    table: str, scope: Scope, olds: Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await _count(f"{table} {_LIVE_NAMING}", scope, olds, ch)


async def _count(source: str, scope: Scope, olds: Sequence[str], ch: _CHClient | None) -> int:
    ch = ch or _default_client()
    n = 0
    for part in _chunks(olds):
        row = await ch.query_one(
            f"SELECT count() AS n FROM {source}",
            params={"org": scope[0], "ns": scope[1], "olds": part},
        )
        n += int((row or {}).get("n") or 0)
    return n


async def rename_graph(
    table: str,
    cols: Sequence[str],
    pair: bool,
    scope: Scope,
    mapping: dict[str, str],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Copy the scope's rows naming a key of `mapping` under its value, then delete the
    old rows. A copy an interrupted run already made is not made again; a pair whose two
    ends become one entity is dropped, as graph writes never make a self-edge.
    `mapping`'s values are canonical, so no copy names a key and the delete never meets
    one."""
    ch = ch or _default_client()
    swap = "{olds:Array(String)}, {news:Array(String)}"
    mapped = {c: f"transform(toString({c}), {swap})" for c in cols}
    if pair:
        a, b = cols
        mapped = {a: f"least({mapped[a]}, {mapped[b]})", b: f"greatest({mapped[a]}, {mapped[b]})"}
    replace = ", ".join(f"{expr} AS {c}" for c, expr in mapped.items())
    distinct = f" WHERE {cols[0]} != {cols[1]}" if pair else ""
    old_rows = f"{_SCOPE} AND {_naming(cols, 'olds')}"
    copies = (
        f"SELECT * FROM (SELECT * REPLACE ({replace}) FROM "
        f"(SELECT * FROM {table} WHERE {old_rows})){distinct}"
    )
    made = f"SELECT * FROM {table} WHERE {_SCOPE} AND {_naming(cols, 'news')}"
    olds = sorted(mapping)
    for part in _chunks(olds):
        params = {"org": scope[0], "ns": scope[1], "olds": part, "news": [mapping[o] for o in part]}
        await ch.execute(
            f"INSERT INTO {table} SELECT * FROM ({copies} EXCEPT ALL {made})", params=params
        )
        await ch.execute(f"DELETE FROM {table} WHERE {old_rows} {_SYNC}", params=params)


async def unsynced_degrees(*, ch: _CHClient | None = None) -> list[Scope]:
    """The namespaces whose `loom.entity_degree` does not add up to their edges (each edge
    counts `co_occur` at both ends): a rename moved them, or a rebuild was cut short."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT org, namespace FROM
          (SELECT org, namespace, 2 * sum(co_occur) AS e FROM loom.entity_edges
           GROUP BY org, namespace) AS x
        FULL OUTER JOIN
          (SELECT org, namespace, sum(degree) AS d FROM loom.entity_degree
           GROUP BY org, namespace) AS y
        USING (org, namespace)
        WHERE e != d
        ORDER BY org, namespace
        """
    )
    return [(str(r["org"]), str(r["namespace"])) for r in rows]


async def rebuild_degrees(scope: Scope, *, ch: _CHClient | None = None) -> None:
    """The scope's `loom.entity_degree` recomputed from its edges."""
    ch = ch or _default_client()
    params = {"org": scope[0], "ns": scope[1]}
    await ch.execute(f"DELETE FROM loom.entity_degree WHERE {_SCOPE} {_SYNC}", params=params)
    await ch.execute(
        "INSERT INTO loom.entity_degree SELECT org, namespace, "
        "arrayJoin([entity_a, entity_b]) AS entity, co_occur AS degree "
        f"FROM loom.entity_edges WHERE {_SCOPE}",
        params=params,
    )


async def reemit_memory(
    scope: Scope,
    olds: Sequence[str],
    rename: Callable[[list[str]], list[str]],
    *,
    limit: int = 1000,
    ch: _CHClient | None = None,
) -> dict[str, int]:
    """Re-emit the scope's live `loom.memory` and `loom.memory_embeddings` rows naming
    `olds` with `rename(entities)`, one version above the row read."""
    ch = ch or _default_client()
    written = dict.fromkeys(MEMORY_TABLES, 0)
    for part in _chunks(olds):
        params = {"org": scope[0], "ns": scope[1], "olds": part}
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
