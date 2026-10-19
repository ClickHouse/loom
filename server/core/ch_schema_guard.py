"""The client's schema guard: statements that name a table a pending
migration creates are refused before they are sent."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from server.core import request_work

log = logging.getLogger(__name__)


# The read-side skew guard. A statement that names a table a
# PENDING migration creates (this build's file, absent from the service's
# `loom._migrations`) is refused here with `SchemaPendingError` instead of
# raising code 60 inside a leg — a leg that treats a failed read as additive
# continues without it, and the request's ledger row says which table
# it went without (`args.schema_skipped`). The pending set comes from the
# same ledger read `/v1/health` already makes (`schema_status`, one per
# minute per process); DDL is never refused (a migrator creates what is
# pending). One warning per (table, process).
_SCHEMA_GUARD_TTL_S = 60.0
# A branch server is a handful of files ahead of the service. A ledger read
# that reports nothing applied, or more pending files than this, is not skew
# but an anomaly (an empty or foreign `_migrations`, a fake in a test) and
# would otherwise refuse every statement for a TTL: the guard goes idle
# instead, with one warning.
_SCHEMA_GUARD_MAX_PENDING = 25
_schema_guard: dict[str, Any] = {"at": None, "tables": {}, "refreshing": False}
_SCHEMA_GUARD_WARNED: set[str] = set()


def schema_guard_reset() -> None:
    """Forget the pending set and the warnings (after migrations, and in tests)."""
    _schema_guard["at"], _schema_guard["tables"], _schema_guard["refreshing"] = None, {}, False
    _SCHEMA_GUARD_WARNED.clear()


async def pending_tables(*, now: float | None = None) -> dict[str, str]:
    """`{table: migration}` a pending migration would create; {} when
    nothing is pending or the ledger is unreadable. Refreshed
    at most once per TTL; a refresh in flight (or the ledger read itself,
    which comes back through this client) sees the previous answer."""
    t = time.monotonic() if now is None else now
    at = _schema_guard["at"]
    cached: dict[str, str] = _schema_guard["tables"]
    if _schema_guard["refreshing"] or (at is not None and t - float(at) < _SCHEMA_GUARD_TTL_S):
        return cached
    _schema_guard["refreshing"] = True
    tables: dict[str, str] = {}
    try:
        from server.core.schema.status import pending_created_tables, schema_status

        status = await schema_status()
        pending = status.get("pending") or []
        applied = int(status.get("applied") or 0)
        if pending and (applied == 0 or len(pending) > _SCHEMA_GUARD_MAX_PENDING):
            if "__anomaly__" not in _SCHEMA_GUARD_WARNED:
                _SCHEMA_GUARD_WARNED.add("__anomaly__")
                log.warning(
                    "ch: schema guard idle — the ledger reports %d applied and %d pending "
                    "migrations, which is not a branch ahead of the service",
                    applied,
                    len(pending),
                )
            tables = {}
        else:
            tables = pending_created_tables(pending)
    except Exception:  # noqa: BLE001 — the guard fails open
        tables = {}
    finally:
        _schema_guard["refreshing"] = False
    _schema_guard["at"], _schema_guard["tables"] = t, tables
    return tables


def _qualified(table: str) -> str:
    """`table` as a statement names it: a bare name is a `loom` table."""
    return table if "." in table else f"loom.{table}"


def _statement_names_table(sql: str, table: str) -> bool:
    database, _, name = _qualified(table).partition(".")
    pattern = rf"\b{re.escape(database)}\.`?{re.escape(name)}`?(?![A-Za-z0-9_])"
    return re.search(pattern, sql or "") is not None


def pending_table_named(
    sql: str, tables: dict[str, str], *, insert_table: str | None = None
) -> str | None:
    """The first pending table `sql` names (or the INSERT target), else None."""
    if not tables:
        return None
    if insert_table is not None:
        named = insert_table.replace("`", "").removeprefix("loom.")
        return named if named in tables else None
    for table in tables:
        if _statement_names_table(sql, table):
            return table
    return None


async def guard_pending_schema(
    op_name: str, sql: str, cls: str, *, insert_table: str | None = None
) -> None:
    """Refuse a read or write against a pending table."""
    if cls == "ddl":
        return
    tables = await pending_tables()
    hit = pending_table_named(sql, tables, insert_table=insert_table)
    if hit is None:
        return
    migration = tables[hit]
    if hit not in _SCHEMA_GUARD_WARNED:
        _SCHEMA_GUARD_WARNED.add(hit)
        log.warning(
            "ch %s: %s is created by pending migration %s on this service; refusing "
            "statements that name it until the migration runs (one line per table)",
            op_name,
            _qualified(hit),
            migration,
        )
    try:
        request_work.note_schema_skipped(hit)
    except Exception:  # noqa: BLE001 — bookkeeping never masks the SchemaPendingError below
        pass
    from server.core.errors import SchemaPendingError

    raise SchemaPendingError(
        f"{_qualified(hit)} is created by pending migration {migration}; "
        "the service has not applied it",
        table=hit,
        migration=migration,
    )


__all__ = ["guard_pending_schema", "pending_table_named", "pending_tables", "schema_guard_reset"]
