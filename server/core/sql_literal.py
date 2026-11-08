"""ClickHouse string literals for the few statements whose text must name a
value (access statements take no query parameters)."""

from __future__ import annotations


def sql_string(value: str) -> str:
    """`value` as a ClickHouse string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
