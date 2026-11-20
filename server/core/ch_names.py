"""The databases Loom's statements name, and the one place that says where they are.

SQL in `server/` and `schemas/migrations/` is written against two aliases: `loom`, the
schema, and `loom_telemetry`, what Loom derives from its own tables. `qualify` maps
each to its configured database (`LOOM_CH_DATABASE`, `LOOM_CH_TELEMETRY_DATABASE`)
where a statement reaches the driver, so a migration file's text, and the checksum
its ledger keeps, stay as written. At the defaults it returns the statement as it
is. DDL read back from ClickHouse (`create_table_query`, `SHOW CREATE`) names the
configured databases; `unqualify` turns it back into the aliases the code parses. A
name passed as a value (`system.tables` filters, rows read back from it) goes through
`physical`, `logical` or `split_table`; ClickStack's OTel tables are `otel_table(name)`.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from server.settings import settings

# The aliases the SQL is written against.
SCHEMA = "loom"
TELEMETRY = "loom_telemetry"
# The read-only reader user when LOOM_CH_READER_USER names none.
DEFAULT_READER = "loom_reader"

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


@functools.cache
def _tokens(names: tuple[str, ...], keeper: str) -> re.Pattern[str]:
    """What a statement holds, for mapping the database `names`: a KeeperMap path
    under `keeper` (its keys live in Keeper, outside any database, so two schemas on
    one service must not share them); a string literal (doubled quote or backslash
    escapes) or a comment, left alone; a name that qualifies another (`loom.memory`,
    `` `loom`.memory ``, `"loom".memory`) or follows DATABASE (`CREATE DATABASE IF
    NOT EXISTS loom_telemetry`); any other quoted identifier, left alone."""
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    name = rf"(?:`(?P<bt{{0}}>{alt})`|\"(?P<dq{{0}}>{alt})\"|(?P<bare{{0}}>{alt}))"
    path = re.escape(keeper) if keeper else r"(?!)"
    return re.compile(
        rf"""(?P<keeper>\bKeeperMap\s*\(\s*')/{path}/(?P<path>(?:[^'\\]|\\.|'')*')
        |'(?:[^'\\]|\\.|'')*'
        |--[^\n]*
        |/\*.*?\*/
        |(?<![\w.`"$]){name.format(1)}(?=\.)
        |(?P<ddl>\b(?i:DATABASE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?)){name.format(2)}(?![\w.`"$])
        |`(?:[^`\\]|\\.|``)*`
        |"(?:[^"\\]|\\.)*\"""",
        re.S | re.X,
    )


def _translate(sql: str, pairs: list[tuple[str, str]]) -> str:
    """`sql` with each database name replaced by its pair's second; the first pair is
    the schema's, whose KeeperMap paths move with it."""
    mapping = {old: new for old, new in pairs if old != new}
    if not mapping or not any(old in sql for old in mapping):
        return sql
    keeper = pairs[0][0] if pairs[0][0] in mapping else ""

    def swap(m: re.Match[str]) -> str:
        if m["keeper"] is not None:
            return f"{m['keeper']}/{mapping[keeper]}/{m['path']}"
        # A name is written as `quote` spells it, however the statement quoted it.
        for i, prefix in (("1", ""), ("2", m["ddl"] or "")):
            old = m[f"bt{i}"] or m[f"dq{i}"] or m[f"bare{i}"]
            if old is not None:
                return prefix + quote(mapping[old])
        return m.group(0)

    return _tokens(tuple(mapping), keeper).sub(swap, sql)


def qualify(sql: str) -> str:
    """`sql` with every `loom.` and `loom_telemetry.` qualifier naming its configured
    database, and a KeeperMap path under `/loom/` under the schema's database. String
    literals, comments and other quoted identifiers are left as written."""
    return _translate(sql, [(SCHEMA, database()), (TELEMETRY, telemetry_database())])


def unqualify(sql: str) -> str:
    """DDL read back from ClickHouse, naming the aliases where it names the
    configured databases: the inverse of `qualify`."""
    return _translate(sql, [(database(), SCHEMA), (telemetry_database(), TELEMETRY)])


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
# The driver methods that take an insert target first, positionally or as `table`.
_INSERT_METHODS = frozenset({"insert", "create_insert_context", "insert_df", "insert_arrow"})


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
        if keyword is None and name in _INSERT_METHODS:
            keyword = "table"
        if keyword is None:
            return attr

        def run(*args: Any, **kwargs: Any) -> Any:
            if args and isinstance(args[0], str):
                args = (qualify(args[0]), *args[1:])
            elif isinstance(kwargs.get(keyword), str):
                kwargs[keyword] = qualify(kwargs[keyword])
            if keyword == "table" and kwargs.get("database"):
                kwargs["database"] = physical(str(kwargs["database"]))
            return attr(*args, **kwargs)

        return run

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)
