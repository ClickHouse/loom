"""A schema-qualified SQL object is found by its bare
name and by the dotted name a reader writes."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.store.index import CodeRepo


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return []


@pytest.mark.asyncio
async def test_bare_name_reaches_a_schema_qualified_object() -> None:
    ch = _CH()
    await CodeRepo(ch).symbols_by_name("o", "n", "xml_dictionary_table")
    sql, params = ch.calls[0]
    assert params["bare"] == "xml_dictionary_table"
    # `mod::test.xml_dictionary_table` ends with `.xml_dictionary_table`
    assert params["dot_suffix"] == ".xml_dictionary_table"
    assert "endsWith(qualified_name, {dot_suffix:String})" in sql
    assert "OR qualified_name = {bare:String}" in sql


@pytest.mark.asyncio
async def test_dotted_name_keeps_its_written_form_as_a_suffix() -> None:
    ch = _CH()
    await CodeRepo(ch).symbols_by_name("o", "n", "test.xml_dictionary_table")
    sql, params = ch.calls[0]
    # the Python normalisation still applies (Cls.method → Cls::method) …
    assert params["full"] == "test::xml_dictionary_table"
    # … and the SQL form the reader typed is tried as written
    assert params["sql_suffix"] == "::test.xml_dictionary_table"
    assert "endsWith(qualified_name, {sql_suffix:String})" in sql
    # a dotted query is a qualified one: no bare-equality widening
    assert "OR qualified_name = {bare:String}" not in sql
    assert "dot_suffix" not in sql


@pytest.mark.asyncio
async def test_qualified_cpp_lookup_is_unchanged() -> None:
    ch = _CH()
    await CodeRepo(ch).symbols_by_name("o", "n", "IColumn::insert")
    sql, params = ch.calls[0]
    assert (params["bare"], params["full"], params["suffix"]) == (
        "insert",
        "IColumn::insert",
        "::IColumn::insert",
    )
    assert params["sql_suffix"] == "::IColumn::insert"  # harmless: equals the :: suffix
    assert "dot_suffix" not in sql and "OR qualified_name = {bare:String}" not in sql
