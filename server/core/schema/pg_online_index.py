"""The one nontransactional Postgres migration form: a declared concurrent index."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_ID = r"[a-z_][a-z0-9_]{0,62}"
_DECLARATION = re.compile(
    rf"-- loom:online-index (?P<schema>{_ID})\.(?P<name>{_ID}) ON "
    rf"(?P<table_schema>{_ID})\.(?P<table>{_ID}) \((?P<columns>{_ID}(?:, {_ID})*)\)\n"
)
_CREATE = re.compile(
    rf"CREATE\s+INDEX\s+CONCURRENTLY\s+(?P<name>{_ID})\s+ON\s+"
    rf"(?P<schema>{_ID})\.(?P<table>{_ID})\s+USING\s+btree\s*"
    rf'\((?P<columns>{_ID}\s+COLLATE\s+"C"(?:\s*,\s*{_ID}\s+COLLATE\s+"C")*)\)\s*;\s*'
)


@dataclass(frozen=True)
class OnlineIndex:
    schema: str
    name: str
    table: str
    columns: tuple[str, ...]
    sql: str

    @property
    def qualified(self) -> str:
        return f'"{self.schema}"."{self.name}"'


def parse(sql: str) -> OnlineIndex | None:
    """Refuse malformed directives before a connection or ledger write is made."""
    if "loom:online-index" not in sql:
        return None
    declaration = _DECLARATION.match(sql)
    create = _CREATE.fullmatch(sql[declaration.end() :]) if declaration else None
    if declaration is None or create is None:
        raise ValueError("invalid online-index migration declaration or SQL")
    columns = tuple(declaration["columns"].split(", "))
    actual = tuple(part.split()[0] for part in create["columns"].split(","))
    if (
        declaration["schema"] != declaration["table_schema"]
        or any(declaration[key] != create[key] for key in ("schema", "name", "table"))
        or columns != actual
        or len(set(columns)) != len(columns)
    ):
        raise ValueError("online-index SQL does not match its declaration")
    return OnlineIndex(
        declaration["schema"],
        declaration["name"],
        declaration["table"],
        columns,
        sql[declaration.end() :],
    )


_CATALOG = """
SELECT c.relkind::text AS relkind, tn.nspname AS table_schema, t.relname AS table_name,
       am.amname, i.indisvalid, i.indisready, i.indislive,
       NOT i.indisunique AND NOT i.indisexclusion AND i.indpred IS NULL
       AND i.indexprs IS NULL AND i.indnkeyatts = i.indnatts
       AND NOT i.indisreplident AND c.reloptions IS NULL AS plain,
       ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(attnum, n)
             JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
             ORDER BY k.n) AS columns,
       ARRAY(SELECT co.collname::text FROM unnest(i.indcollation) WITH ORDINALITY k(oid, n)
             JOIN pg_collation co ON co.oid = k.oid
             JOIN pg_namespace ns ON ns.oid = co.collnamespace AND ns.nspname = 'pg_catalog'
             ORDER BY k.n) AS collations,
       ARRAY(SELECT v FROM unnest(i.indoption) v) AS options,
       ARRAY(SELECT op.opcdefault FROM unnest(i.indclass) WITH ORDINALITY k(oid, n)
             JOIN pg_opclass op ON op.oid = k.oid ORDER BY k.n) AS default_opclasses,
       EXISTS(SELECT 1 FROM pg_constraint WHERE conindid = c.oid)
       OR EXISTS(SELECT 1 FROM pg_depend WHERE classid = 'pg_class'::regclass
                 AND objid = c.oid AND deptype IN ('e', 'x'))
       OR EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid = c.oid) AS dependent
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_index i ON i.indexrelid = c.oid
LEFT JOIN pg_class t ON t.oid = i.indrelid
LEFT JOIN pg_namespace tn ON tn.oid = t.relnamespace
LEFT JOIN pg_am am ON am.oid = c.relam
WHERE n.nspname = $1 AND c.relname = $2
"""


async def ready(conn: Any, index: OnlineIndex) -> bool | None:
    """Absent, matching incomplete, or matching ready; foreign objects always refuse."""
    row = await conn.fetchrow(_CATALOG, index.schema, index.name)
    if row is None:
        return None
    expected = {
        "relkind": "i",
        "table_schema": index.schema,
        "table_name": index.table,
        "amname": "btree",
        "plain": True,
        "columns": list(index.columns),
        "collations": ["C"] * len(index.columns),
        "options": [0] * len(index.columns),
        "default_opclasses": [True] * len(index.columns),
        "dependent": False,
    }
    differences = {key: row[key] for key, value in expected.items() if row[key] != value}
    if differences:
        raise ValueError(
            f"online-index {index.qualified} has a different definition or dependency: "
            f"{differences}"
        )
    return bool(row["indisvalid"] and row["indisready"] and row["indislive"])


async def build(conn: Any, index: OnlineIndex, *, recorded: bool) -> None:
    state = await ready(conn, index)
    if recorded and state is not True:
        raise ValueError(f"recorded online-index {index.qualified} is absent or invalid")
    if state is True:
        return
    if state is False:
        await conn.execute(f"DROP INDEX CONCURRENTLY {index.qualified}")
    await conn.execute(index.sql)
    if await ready(conn, index) is not True:
        raise ValueError(f"online-index {index.qualified} did not become valid and ready")
