"""The databases Loom's statements name, and the one place that says where they are.

SQL in `server/` and `schemas/migrations/` is written against two aliases: `loom`, the
schema, and `loom_telemetry`, what Loom derives from its own tables. `qualify` maps
each to its configured database (`LOOM_CH_DATABASE`, `LOOM_CH_TELEMETRY_DATABASE`)
where a statement reaches the driver, so a migration file's text, and the checksum
its ledger keeps, stay as written. At the defaults it returns the statement as it
is. A name passed as a value (`system.tables` filters, rows read back from it) goes
through `physical` or `logical`; ClickStack's OTel tables are `otel_table(name)`.
"""

from __future__ import annotations

import re
from typing import Any

from server.settings import settings

# The aliases the SQL is written against.
SCHEMA = "loom"
TELEMETRY = "loom_telemetry"

# What a statement holds that is not SQL to map: a string literal (doubled quote or
# backslash escapes), a comment, a quoted identifier; else an alias that qualifies a
# name (`loom.memory`, `` `loom`.memory ``) or follows DATABASE
# (`CREATE DATABASE IF NOT EXISTS loom_telemetry`), not part of another identifier.
# A KeeperMap path under `/loom/` is the schema's too: its keys live in Keeper, outside
# any database, and two schemas on one service must not share them.
_TOKENS = re.compile(
    r"""(?P<keeper>\bKeeperMap\s*\(\s*')/loom/(?P<path>(?:[^'\\]|\\.|'')*')
    |'(?:[^'\\]|\\.|'')*'
    |"(?:[^"\\]|\\.)*"
    |--[^\n]*
    |/\*.*?\*/
    |(?<![\w.`$])(?P<q>`?)(?P<alias>loom_telemetry|loom)(?P=q)(?=\.)
    |(?P<ddl>\bDATABASE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?)
     (?P<dq>`?)(?P<db>loom_telemetry|loom)(?P=dq)(?![\w.`$])""",
    re.S | re.X | re.I,
)
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def database() -> str:
    """The database Loom's schema lives in."""
    return str(settings.ch_database)


def telemetry_database() -> str:
    """The database Loom's derived telemetry (`sli_minute`) lives in."""
    return str(settings.ch_telemetry_database)


def otel_database() -> str:
    """The database ClickStack's OTel tables live in, which the exporter writes to."""
    return str(settings.clickstack_database) or "default"


def otel_table(name: str) -> str:
    """`otel_traces` → `<otel database>.otel_traces`, quoted when it must be."""
    return f"{quote(otel_database())}.{name}"


def quote(name: str) -> str:
    """A database name as SQL spells it: bare when it is a plain identifier."""
    return name if _PLAIN.fullmatch(name) else f"`{name}`"


def physical(db: str) -> str:
    """The database an alias names in this deployment; any other name as it is."""
    return {SCHEMA: database(), TELEMETRY: telemetry_database()}.get(db, db)


def logical(db: str) -> str:
    """The alias a configured database is known by in code; any other name as it is."""
    return {database(): SCHEMA, telemetry_database(): TELEMETRY}.get(db, db)


def split_table(table: str) -> tuple[str, str]:
    """`loom.memory` → (database, name) as `system.tables` and `system.parts` name
    them; a bare name is a schema table."""
    db, _, name = table.rpartition(".")
    return (physical(db) if db else database(), name)


def _mapped() -> dict[str, str]:
    return {
        alias: target
        for alias, target in ((SCHEMA, database()), (TELEMETRY, telemetry_database()))
        if target != alias
    }


def qualify(sql: str) -> str:
    """`sql` with every `loom.` and `loom_telemetry.` qualifier naming its configured
    database, and a KeeperMap path under `/loom/` under the schema's database. Other
    string literals, comments and quoted identifiers are left as written."""
    mapped = _mapped()
    if not mapped or ("loom" not in sql):
        return sql

    def swap(m: re.Match[str]) -> str:
        if m["keeper"] is not None:
            if SCHEMA not in mapped:
                return m.group(0)
            return f"{m['keeper']}/{mapped[SCHEMA]}/{m['path']}"
        alias, backticked, prefix = m["alias"], m["q"], ""
        if m["db"] is not None:
            alias, backticked, prefix = m["db"], m["dq"], m["ddl"]
        if alias is None or alias not in mapped:
            return m.group(0)
        name = f"`{mapped[alias]}`" if backticked else quote(mapped[alias])
        return prefix + name

    return _TOKENS.sub(swap, sql)


# The driver methods that take a statement first, positionally or by name.
_SQL_METHODS = {
    "query": "query",
    "command": "cmd",
    "raw_query": "query",
    "raw_stream": "query",
    "query_df": "query",
    "query_arrow": "query",
    "query_column_block_stream": "query",
    "query_row_block_stream": "query",
    "query_rows_stream": "query",
}


class NamedClient:
    """A clickhouse-connect client, sync or async, whose statements and insert targets
    name the configured databases: `qualify` applied where each call reaches the
    driver. Every other attribute is the client's own."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        keyword = _SQL_METHODS.get(name)
        if keyword is not None:

            def run(*args: Any, **kwargs: Any) -> Any:
                if args and isinstance(args[0], str):
                    args = (qualify(args[0]), *args[1:])
                elif isinstance(kwargs.get(keyword), str):
                    kwargs[keyword] = qualify(kwargs[keyword])
                return attr(*args, **kwargs)

            return run
        if name == "insert":

            def insert(*args: Any, **kwargs: Any) -> Any:
                if args and isinstance(args[0], str):
                    args = (qualify(args[0]), *args[1:])
                elif isinstance(kwargs.get("table"), str):
                    kwargs["table"] = qualify(kwargs["table"])
                if kwargs.get("database"):
                    kwargs["database"] = physical(str(kwargs["database"]))
                return attr(*args, **kwargs)

            return insert
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)
