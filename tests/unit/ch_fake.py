"""A recording stand-in for `server.core.ch.CHClient`.

Reads are answered from routes matched against the SQL text, every call is
recorded, and a read no route answers fails the test with the statement
rather than returning an empty result that a wrong query would also get:

    ch = RecordingCH().on("FROM loom.memory", [{"memory_key": "k"}])
    ch.install(monkeypatch)
    ...
    assert ch.inserted("memory")[0]["memory_key"] == "k"
    assert "FINAL" in ch.statements("FROM loom.memory")[0].sql
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from clickhouse_connect.driver.external import ExternalData

Rows = list[dict[str, Any]]
Answer = Rows | Callable[[str, dict[str, Any]], Rows]


@dataclass(frozen=True)
class Statement:
    kind: str  # query_one | query_all | execute | insert
    sql: str
    params: dict[str, Any]
    table: str | None = None
    rows: tuple[dict[str, Any], ...] = ()
    external_data: ExternalData | None = None


@dataclass
class _Route:
    needle: str | re.Pattern[str]
    answer: Answer
    times: int | None
    used: int = 0

    def matches(self, sql: str) -> bool:
        if self.times is not None and self.used >= self.times:
            return False
        if isinstance(self.needle, re.Pattern):
            return self.needle.search(sql) is not None
        return self.needle in sql

    def rows(self, sql: str, params: dict[str, Any]) -> Rows:
        self.used += 1
        rows = self.answer(sql, params) if callable(self.answer) else self.answer
        return [dict(r) for r in rows]


@dataclass
class RecordingCH:
    """`strict=False` answers an unrouted read with no rows instead of failing."""

    strict: bool = True
    calls: list[Statement] = field(default_factory=list)
    _routes: list[_Route] = field(default_factory=list)

    def on(
        self, needle: str | re.Pattern[str], answer: Answer, *, times: int | None = None
    ) -> RecordingCH:
        """Answer reads whose SQL contains `needle` (or matches the pattern).
        Routes are tried in the order added; `times` retires a route after that
        many answers, so a sequence of reads can get a sequence of answers."""
        self._routes.append(_Route(needle, answer, times))
        return self

    def _read(self, kind: str, sql: str, params: dict[str, Any] | None) -> Rows:
        params = dict(params or {})
        self.calls.append(Statement(kind, sql, params))
        for route in self._routes:
            if route.matches(sql):
                return route.rows(sql, params)
        if self.strict:
            head = " ".join(sql.split())[:300]
            known = [
                r.needle if isinstance(r.needle, str) else r.needle.pattern for r in self._routes
            ]
            raise AssertionError(f"unrouted {kind}: {head!r}; routes: {known}")
        return []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Rows:
        return self._read("query_all", sql, params)

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None, **_: Any
    ) -> dict[str, Any] | None:
        rows = self._read("query_one", sql, params)
        return rows[0] if rows else None

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        external_data: ExternalData | None = None,
        **_: Any,
    ) -> None:
        self.calls.append(
            Statement("execute", sql, dict(params or {}), external_data=external_data)
        )

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
        project_onto_table: bool | None = None,
        **_: Any,
    ) -> None:
        self.calls.append(
            Statement("insert", "", {}, table=table, rows=tuple(dict(r) for r in rows))
        )

    def statements(self, needle: str = "", *, kind: str | None = None) -> list[Statement]:
        """The recorded reads and executes whose SQL contains `needle`."""
        return [
            s
            for s in self.calls
            if s.kind != "insert" and needle in s.sql and (kind is None or s.kind == kind)
        ]

    def inserted(self, table: str) -> list[dict[str, Any]]:
        """Every row inserted into `table` (with or without the `loom.` prefix)."""
        names = {table, table.removeprefix("loom."), f"loom.{table.removeprefix('loom.')}"}
        return [r for s in self.calls if s.kind == "insert" and s.table in names for r in s.rows]

    def unused_routes(self) -> Sequence[str]:
        """Routes that answered nothing: the code no longer issues that read."""
        return [
            r.needle if isinstance(r.needle, str) else r.needle.pattern
            for r in self._routes
            if r.used == 0
        ]

    def install(self, monkeypatch: pytest.MonkeyPatch, *also: str) -> RecordingCH:
        """Serve `server.core.ch.get_client()` and any module that imported the name
        itself (`"server.memory.app.write"`)."""
        for target in ("server.core.ch", *also):
            monkeypatch.setattr(f"{target}.get_client", lambda: self)
        return self
