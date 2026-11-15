"""What the log-table tests share (`cell_scan.LOG_TABLES`, in
tests/unit/test_cell_scan_tables.py and tests/integration/test_cell_scan.py): the tables a
request writes with the org it named, before any check, and the tables views fill from
them."""

from __future__ import annotations

from collections.abc import Iterable

# The tables a request writes with the org it named, before any check: `LOG_TABLES` holds
# them and every table a view fills from one of them.
LOGGED = frozenset({"loom.agent_events", "loom.code_search_trace"})


def closure(roots: Iterable[str], views: Iterable[tuple[set[str], str]]) -> set[str]:
    """`roots` and every table a view fills from one of them, through any chain of views
    (each view as its sources and its target)."""
    views = list(views)
    out = set(roots)
    while grown := {to for sources, to in views if sources & out} - out:
        out |= grown
    return out
