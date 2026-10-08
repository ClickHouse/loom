"""A worker whose table is empty runs at boot.

`WorkerSpec.first_pass_due` lets a worker say its output is missing so it
runs at boot instead of after the boot stagger — `crc32(name) % interval`,
which for a daily worker is hours on a deployment whose processes live hours,
so a worker with a long interval might otherwise never run.

The rule here is the generic form of that hook: a worker declares the tables it
fills (`WorkerSpec.owned_tables`) and the namespaces it fills them for
(`WorkerSpec.due_scope`); its first pass is due when any owned table holds
no row for a namespace in scope. One statement per table — the namespaces
the table holds, grouped on the table's own ORDER BY prefix — never one per
namespace; the set of due namespaces is returned with a reason per
namespace, as `due_namespaces` does for the graph-stats pass, so the check
and the log name the same namespaces. A table the store cannot read (missing
behind a pending migration, a slow answer) reads as due — the pass runs and
the table tells; the supervisor's 30 s bound and fail-quiet wrapper still
apply around the whole check.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# (org, namespace) pairs a worker fills its tables for.
Scope = Callable[[], Sequence[tuple[str, str]]]


@dataclass(frozen=True)
class OwnedTable:
    """A table a worker's pass fills, and the columns that name a namespace
    in it (`tracker_namespace` on the tracker plane, `namespace` on the code plane)."""

    table: str
    namespace_column: str = "namespace"
    org_column: str = "org"

    @property
    def short(self) -> str:
        return self.table.rsplit(".", 1)[-1]

    def held_sql(self) -> str:
        """The namespaces of `scope` this table holds at least one row for.
        Plain WHERE on the key prefix, no FINAL (an emptiness test reads the
        same with or without ReplacingMergeTree duplicates)."""
        org = self.org_column if self.org_column == "org" else f"{self.org_column} AS org"
        ns = (
            self.namespace_column
            if self.namespace_column == "namespace"
            else f"{self.namespace_column} AS namespace"
        )
        return (
            f"SELECT {org}, {ns} "
            f"FROM {self.table} "
            f"WHERE {self.org_column} IN {{orgs:Array(String)}} "
            f"AND {self.namespace_column} IN {{namespaces:Array(String)}} "
            f"GROUP BY org, namespace"
        )


async def held_namespaces(
    owned: OwnedTable,
    scope: Sequence[tuple[str, str]],
    *,
    ch: Any = None,
) -> set[tuple[str, str]]:
    """The (org, namespace) pairs of `scope` that `owned` holds a row for.
    Raises when the store cannot answer (the caller decides what that means)."""
    if not scope:
        return set()
    client = ch if ch is not None else _client()
    rows = await client.query_all(
        owned.held_sql(),
        {"orgs": sorted({o for o, _n in scope}), "namespaces": sorted({n for _o, n in scope})},
    )
    wanted = set(scope)
    return {(str(r.get("org") or ""), str(r.get("namespace") or "")) for r in rows or []} & wanted


async def empty_namespaces(
    owned_tables: Sequence[OwnedTable],
    scope: Sequence[tuple[str, str]],
    *,
    ch: Any = None,
) -> dict[str, str]:
    """`"org/namespace" -> "<table>_empty"` for every namespace in `scope`
    that some owned table holds no row for; `"<table>_unknown"` when the
    table could not be read (treated as due). The first empty table names
    the reason; one statement per table, whatever the scope's size."""
    out: dict[str, str] = {}
    pairs = list(dict.fromkeys(scope))
    for owned in owned_tables:
        remaining = [p for p in pairs if f"{p[0]}/{p[1]}" not in out]
        if not remaining:
            break
        try:
            held = await held_namespaces(owned, remaining, ch=ch)
        except Exception as e:  # noqa: BLE001 — unknown state: the pass runs, the table tells
            log.warning(
                "first_pass: %s could not be read (%s: %s); treating %d namespace(s) as due",
                owned.table,
                type(e).__name__,
                str(e)[:200],
                len(remaining),
            )
            for org, ns in remaining:
                out[f"{org}/{ns}"] = f"{owned.short}_unknown"
            continue
        for org, ns in remaining:
            if (org, ns) not in held:
                out[f"{org}/{ns}"] = f"{owned.short}_empty"
    return out


def due_hook(
    worker: str,
    owned_tables: Sequence[OwnedTable],
    scope: Scope,
) -> Callable[[], Awaitable[bool]]:
    """The `first_pass_due` a spec with owned tables derives: True when any
    namespace in scope is missing from any owned table, each one logged with
    its reason."""

    async def _due() -> bool:
        due = await empty_namespaces(owned_tables, list(scope()))
        for label, why in due.items():
            log.info("%s: first pass due for %s: %s", worker, label, why)
        return bool(due)

    _due.__name__ = f"{worker}_first_pass_due"
    return _due


def _client() -> Any:
    from server.core.ch import get_client

    return get_client()


__all__ = ["OwnedTable", "Scope", "due_hook", "empty_namespaces", "held_namespaces"]
