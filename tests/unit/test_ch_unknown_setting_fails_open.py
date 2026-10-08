"""A query-level setting one replica does not know is dropped, not fatal.

In a mixed-version cluster an older replica rejects a newer setting (e.g.
`vector_search_use_quantized_codes`, code 115) even though the boot probe,
answered by a newer replica, said the setting exists; a pooled connection
that lands on the older replica would fail every vector read. The client turns
the setting off for the process and re-runs the query without the fragment.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

import server.core.ch as ch_mod
from server.core import ch_errors
from server.core.vectors import quantized_setting

_ERR = (
    "Received ClickHouse exception, code: 115, server response: Code: 115. "
    "DB::Exception: Setting vector_search_use_quantized_codes is neither a builtin "
    "setting nor started with the prefix 'custom_' registered for user-defined settings."
)


def _client(responses: list[Any]) -> Any:
    c = ch_mod.CHClient.__new__(ch_mod.CHClient)
    c.reconnects = 0
    c.seen: list[str] = []

    async def _reconnect(seen: int | None = None) -> None:
        c.reconnects += 1

    class _Inner:
        async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
            c.seen.append(sql)
            r = responses.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r

    c._reconnect = _reconnect  # type: ignore[method-assign]
    c._client = _Inner()
    return c


def test_strip_removes_only_the_fragment() -> None:
    sql = (
        "SELECT k FROM t ORDER BY d ASC LIMIT 5 SETTINGS hnsw_candidate_list_size_for_search = 64, "
        "vector_search_index_fetch_multiplier = 2.0, vector_search_use_quantized_codes = 1"
    )
    out = ch_errors.strip_quantized_setting(sql)
    assert "vector_search_use_quantized_codes" not in out
    assert out.endswith("vector_search_index_fetch_multiplier = 2.0")
    assert ch_errors.strip_quantized_setting("SELECT 1") == "SELECT 1"


@pytest.mark.asyncio
async def test_code_115_on_the_setting_turns_it_off_and_retries_without_it(monkeypatch) -> None:
    quantized_setting.set_quantized_codes_supported(True)
    ok = SimpleNamespace(column_names=["k"], result_rows=[("a",)])
    c = _client([DatabaseError(_ERR), ok])
    rows = await c.query_all(
        "SELECT k FROM t SETTINGS hnsw_candidate_list_size_for_search = 64, "
        "vector_search_use_quantized_codes = 1"
    )
    assert rows == [{"k": "a"}]
    assert len(c.seen) == 2 and "quantized" not in c.seen[1] and "quantized" in c.seen[0]
    assert quantized_setting.quantized_codes_supported() is False
    quantized_setting.set_quantized_codes_supported(None)


@pytest.mark.asyncio
async def test_code_115_without_the_fragment_in_the_sql_is_raised() -> None:
    """A 115 the query text cannot explain is somebody else's bug: surface it."""
    c = _client([DatabaseError(_ERR)])
    with pytest.raises(DatabaseError):
        await c.query_one("SELECT 1")
    assert len(c.seen) == 1


@pytest.mark.asyncio
async def test_other_errors_still_propagate_untouched() -> None:
    c = _client([DatabaseError("Code: 43. DB::Exception: illegal type")])
    with pytest.raises(DatabaseError):
        await c.query_all("SELECT k FROM t SETTINGS vector_search_use_quantized_codes = 1")
    assert len(c.seen) == 1
