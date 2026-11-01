"""The read memory.import decides each key's state from: the newest version of each key
across the sessions the token may read."""

from __future__ import annotations

from typing import Any

import pytest

import server.memory.store as memory_repo
from server.identity.domain.read_scope import ReadFilters


class _Recording:
    def __init__(self) -> None:
        self.reads: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.reads.append((sql, params))
        return []


@pytest.mark.asyncio
async def test_a_session_bounded_token_reads_only_its_own_session() -> None:
    """An `own_session` token answers deleted, revised or superseded only for keys in its
    own session: the read carries the token's user and session."""
    ch = _Recording()
    scope = ReadFilters(scope="own_session", user_id="u", session_id="s1")
    await memory_repo.key_heads_across_sessions(
        "test", "personal", "cursor", "u", ["import:k"], scope=scope, ch=ch
    )
    ((sql, params),) = ch.reads
    assert "user_id = {scope_uid:String}" in sql and "session_id = {scope_sid:String}" in sql
    assert (params["scope_uid"], params["scope_sid"]) == ("u", "s1")
    assert "select_sequential_consistency = 1" in sql


@pytest.mark.asyncio
async def test_no_keys_send_no_statement() -> None:
    ch = _Recording()
    scope = ReadFilters(scope="namespace", user_id="", session_id=None)
    assert (
        await memory_repo.key_heads_across_sessions("o", "n", "a", "-", [], scope=scope, ch=ch)
        == []
    )
    assert ch.reads == []
