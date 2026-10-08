"""Which namespaces this process wrote moments ago, and the graph write marks
an insert into an entity-graph table sets. `observe_insert` is registered on
the ClickHouse client (`server.core.ch.add_insert_observer`) by
`server/wiring.py`; the search path reads `written_recently`.
"""

from __future__ import annotations

import time
from typing import Any

# Which namespaces were written moments ago, in this process. ClickHouse's
# own guidance for read-after-write on shared-storage replicas is session
# affinity first ("do the write/read using the same session or a sticky
# connection") and `select_sequential_consistency` only for reads that land
# on a random node. Loom's HTTP pool spreads requests over the service's
# replicas, so affinity is not available; this registry gives the search
# path the next best thing — pay the Keeper round-trip only for the reads
# that follow a write within the replication window, on that namespace.
# Bounded, process-local (a second process falls back to the empty-pool
# retry), never raises.
_RECENT_WRITES: dict[tuple[str, str], float] = {}
_RECENT_WRITES_MAX = 4096
_MEMORY_TABLES_PREFIX = "loom.memory"


def note_recent_write(org: str, namespace: str, *, now: float | None = None) -> None:
    if not org or not namespace:
        return

    if len(_RECENT_WRITES) >= _RECENT_WRITES_MAX:
        # Drop the oldest quarter; a stale entry only costs one sequential read.
        oldest = sorted(_RECENT_WRITES, key=lambda k: _RECENT_WRITES[k])
        for k in oldest[: _RECENT_WRITES_MAX // 4]:
            _RECENT_WRITES.pop(k, None)
    _RECENT_WRITES[(org, namespace)] = time.monotonic() if now is None else now


def written_recently(
    org: str, namespace: str, within_s: float, *, now: float | None = None
) -> bool:
    """True when `namespace` saw a memory-table insert from this process
    less than `within_s` seconds ago."""
    if within_s <= 0:
        return False

    t = _RECENT_WRITES.get((org, namespace))
    if t is None:
        return False
    return ((time.monotonic() if now is None else now) - t) < within_s


# The entity-graph tables: an insert into one marks its namespace's graph as written
# (server/memory/app/graph_write_marks.py), which retires the graph walks remembered before it.
GRAPH_TABLES = frozenset(
    {
        "loom.entity_edges",
        "loom.entity_edge_memories",
        "loom.entity_triples",
        "loom.entity_triple_memories",
    }
)


def namespaces_of_rows(
    table: str, rows: list[dict[str, Any]], *, graph: bool = False
) -> set[tuple[str, str]]:
    """(org, namespace) pairs a memory-table insert touches (a graph-table insert
    with `graph`); empty for other tables or rows without both columns. Looks at
    the first rows only — a bulk insert writes one namespace."""
    wanted = table in GRAPH_TABLES if graph else table.startswith(_MEMORY_TABLES_PREFIX)
    if not wanted or not rows:
        return set()
    out: set[tuple[str, str]] = set()
    for r in rows[:64]:
        org, ns = r.get("org"), r.get("namespace")
        if org and ns:
            out.add((str(org), str(ns)))
    return out


def observe_insert(table: str, rows: list[dict[str, Any]]) -> None:
    """After a successful insert: note a memory-table write, and a graph write
    for the entity-graph tables."""
    for org, ns in namespaces_of_rows(table, rows):
        note_recent_write(org, ns)
    graph_namespaces = namespaces_of_rows(table, rows, graph=True)
    if graph_namespaces:
        from server.memory.app.graph_write_marks import marks

        for org, ns in graph_namespaces:
            marks().note_write(org, ns)
