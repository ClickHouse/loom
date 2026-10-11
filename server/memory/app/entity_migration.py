"""Move every stored entity to the current canonical form, once after the normaliser
changes. The graph tables hold only canonical text, so each value is mapped by
`recanonical` (a stem an earlier rule cut, `respons`, becomes `response`). `loom.memory`
also holds text writers gave only lowercased, so a value the graph names takes the
graph's mapping and any other is canonicalised as written."""

from __future__ import annotations

from typing import Any

from server.memory.domain.entity_sentinels import NONE_SENTINEL, PARSE_FAILED_SENTINEL
from server.memory.domain.graph_quality import canonical_entity_text, recanonical
from server.memory.store import entity_migration as store

_SENTINELS = {NONE_SENTINEL, PARSE_FAILED_SENTINEL}


def _changes(values: set[str], to: Any) -> dict[str, str]:
    out = {}
    for v in values - _SENTINELS:
        new = to(v)
        if new and new != v:
            out[v] = new
    return out


async def migrate_entities(*, apply: bool, ch: Any = None) -> dict[str, int]:
    """Rows per table whose entities change; with `apply`, rewritten."""
    graph = _changes(await store.graph_values(ch=ch), recanonical)
    memory = graph | _changes(await store.memory_values(ch=ch) - set(graph), canonical_entity_text)
    counts: dict[str, int] = {}
    olds = sorted(graph)
    news = [graph[o] for o in olds]
    touched: set[tuple[str, str]] = set()
    for table, cols, pair in store.GRAPH_TABLES:
        counts[table] = await store.count_graph(table, cols, olds, ch=ch)
        if apply and counts[table]:
            done = await store.rename_graph(table, cols, pair, olds, news, ch=ch)
            if table == "loom.entity_edges":
                touched.update(done)
    if apply:
        await store.rebuild_degrees(sorted(touched), ch=ch)
    mem_olds = sorted(memory)
    for table in store.MEMORY_TABLES:
        counts[table] = await store.count_memory(table, mem_olds, ch=ch)
    if apply and mem_olds:

        def rename(entities: list[str]) -> list[str]:
            return list(dict.fromkeys(memory.get(e, e) for e in entities))

        counts |= await store.reemit_memory(mem_olds, rename, ch=ch)
    return counts
