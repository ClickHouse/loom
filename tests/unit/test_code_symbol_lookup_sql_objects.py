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


@pytest.mark.asyncio
async def test_file_qualified_candidates_and_live_rows_bind_the_path_before_limit() -> None:
    class Rows(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            await super().query_all(sql, params)
            return (
                [{"tier": "treesitter", "qualified_name": "Type::method", "symbol_id": "s"}]
                if "SELECT DISTINCT" in sql
                else []
            )

    ch = Rows()
    await CodeRepo(ch).symbols_by_name("o", "n", "Type::method", path="src/Type.cpp", limit=1)
    assert len(ch.calls) == 2
    for sql, params in ch.calls:
        assert "path = {path:String}" in sql and params["path"] == "src/Type.cpp"
        assert "src/Type.cpp" not in sql
    assert "FROM loom.code_symbols" in ch.calls[0][0]
    assert ch.calls[1][0].index("path = {path:String}") < ch.calls[1][0].index("LIMIT")
    assert "tombstoned_at IS NULL" in ch.calls[1][0]


@pytest.mark.asyncio
async def test_file_qualified_fuzzy_candidates_keep_the_same_path_constraint() -> None:
    ch = _CH()
    await CodeRepo(ch).symbols_fuzzy("o", "n", "method", path="src/Type.cpp", limit=3)
    sql, params = ch.calls[0]
    assert "path = {path:String}" in sql and params["path"] == "src/Type.cpp"
    assert sql.index("path = {path:String}") < sql.index("LIMIT")
