"""Checks the migration runner makes before a file, dry runs included: each refuses the
run, naming what stands in its way, where the file alone could not say why it must not
run. Keyed by the file's name without its number, so a renumbered file keeps its check."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from server.core.ch_names import SCHEMA, physical
from server.core.naming import MIRROR_REFUSED, mirror_name_sql

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client

# Each table where a namespace is registered or holds content: the condition under which
# its row is live, and whether it is read FINAL (a ReplacingMergeTree). A ledger row only
# names a namespace, and a chunk belongs to a document read here. The namespace census
# also counts state a new owner of a name would inherit (grants, hooks, open erasures);
# this asks only whether the name is in use.
_LIVE_ROWS = {
    "namespaces": ("active", True),
    "code_repos": ("active", True),
    "knowledge_sources": ("active", True),
    "memory": ("tombstoned_at IS NULL", True),
    "documents": ("superseded_at IS NULL", True),
    "conversation_threads": ("tombstoned_at IS NULL", True),
    "conversations": ("tombstoned_at IS NULL", False),
}


def no_mirror_names(ch: Client) -> None:
    """Once the gate reserves `acl-<24 hex>` for mirrors, a namespace already of that form
    would be cut off from every token: refuse while one is registered or holds live rows
    in ClickHouse. A scan of the namespace column finds the names; only their rows are
    read whole, by key, to tell whether any is live."""
    db = physical(SCHEMA)
    # A dry run against a new server has not made the tables yet: nothing to find there.
    present = ch.query(
        "SELECT name FROM system.tables "
        "WHERE database = {db:String} AND name IN {names:Array(String)}",
        parameters={"db": db, "names": list(_LIVE_ROWS)},
    ).result_rows
    # Each live namespace of the form, with the tables it holds rows in.
    found: dict[str, list[str]] = {}
    for (table,) in present:
        live, final = _LIVE_ROWS[table]
        # The client names the configured database for each `loom.` table.
        read = f"loom.{table}" + (" FINAL" if final else "")
        rows = ch.query(
            f"SELECT DISTINCT org, namespace FROM {read} WHERE (org, namespace) IN "
            f"(SELECT org, namespace FROM loom.{table} WHERE {mirror_name_sql()}) "
            f"AND {live}"
        ).result_rows
        for org, namespace in rows:
            found.setdefault(f"{org}/{namespace}", []).append(table)
    if found:
        raise RuntimeError(
            f"{MIRROR_REFUSED}, and these exist: {_listed(found)}. With the "
            "build before this one, erase what each holds (erasure.request), remove its code "
            "repository (code.repo.remove) and delete it (namespace.delete), then migrate again"
        )


def _listed(found: dict[str, list[str]]) -> str:
    """The first ten namespaces with their tables, and how many more there are."""
    names = sorted(found)
    shown = ", ".join(f"{name} ({', '.join(found[name])})" for name in names[:10])
    return shown if len(names) <= 10 else f"{shown} and {len(names) - 10} more"


PRECHECKS: dict[str, Callable[[Client], None]] = {
    "mirror_names_reserved.sql": no_mirror_names,
}
