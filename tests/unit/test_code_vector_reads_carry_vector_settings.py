"""Every code-plane vector-ranked read carries the same
`vector_search_settings()` fragment the memory plane carries, so a
`Quantized(...)` codec on the column turns the scan codes-first."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.store.index import CodeRepo


class _StubCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return []


def _norm(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.asyncio
async def test_chunk_vector_leg_ends_with_the_settings_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.vectors import search_settings as search_sql
    from server.settings import settings

    monkeypatch.setattr(settings, "vector_search_quantized_codes", True)
    monkeypatch.setattr(search_sql, "quantized_codes_supported", lambda: True)
    ch = _StubCH()
    await CodeRepo(ch).vector_search_chunks("o", "n", "m", [0.1, 0.2], pool=200)  # type: ignore[arg-type]
    sql = _norm(ch.calls[0][0])
    assert sql.endswith(
        "ORDER BY distance ASC LIMIT {pool:UInt32} " + search_sql.vector_search_settings()
    )
    assert "vector_search_use_quantized_codes = 1" in sql


@pytest.mark.asyncio
async def test_fragment_is_codec_free_when_the_server_does_not_know_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.vectors import search_settings as search_sql

    monkeypatch.setattr(search_sql, "quantized_codes_supported", lambda: False)
    ch = _StubCH()
    await CodeRepo(ch).vector_search_chunks("o", "n", "m", [0.1], pool=5)  # type: ignore[arg-type]
    sql = _norm(ch.calls[0][0])
    assert "SETTINGS vector_search_index_fetch_multiplier" in sql
    assert "vector_search_use_quantized_codes" not in sql


@pytest.mark.asyncio
async def test_answer_cache_lookup_carries_the_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app import answers_cache
    from server.core.vectors import search_settings as search_sql
    from server.settings import settings

    monkeypatch.setattr(settings, "vector_search_quantized_codes", True)
    monkeypatch.setattr(search_sql, "quantized_codes_supported", lambda: True)
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    assert await answers_cache._cache_lookup(repo, "o", "n", "m", [0.1]) == []
    sql = _norm(ch.calls[0][0])
    assert (
        "ORDER BY distance ASC LIMIT {k:UInt32} SETTINGS vector_search_index_fetch_multiplier"
        in sql
    )
    assert "vector_search_use_quantized_codes = 1" in sql
