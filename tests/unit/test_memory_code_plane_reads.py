"""The memory reads the code plane serves from: code.search's recent-directory
tie-break reads the user's files through memory, one parent per directory."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import code_plane_reads


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        return self.rows


@pytest.mark.asyncio
async def test_user_file_dirs_are_each_parent_once_in_path_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([{"f": "a/b/x.py"}, {"f": "a/b/y.py"}, {"f": "a/c.py"}, {"f": "top.py"}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    assert await code_plane_reads.user_file_dirs("o", "u", None) == ["a/b", "a", "top.py"]
    ((sql, params),) = ch.calls
    assert "FROM loom.memory FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "uid": "u", "lim": 200}


@pytest.mark.asyncio
async def test_user_file_dirs_read_only_the_callers_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.search import handler
    from tests.unit.principals import acting_as, principal

    ch = _CH([{"f": "a/x.py"}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _mine(org: str, subject: str | None = None) -> list[str]:
        return ["ns1"]

    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _mine)
    monkeypatch.setattr(handler, "_RECENT_DIR_CACHE", {})
    with acting_as(principal("viewer", org="o", namespaces=("ns1",))):
        assert await handler._user_recent_dirs("o", "u") == ["a"]
    ((sql, params),) = ch.calls
    assert "namespace IN {nss:Array(String)}" in sql and params["nss"] == ["ns1"]
    # A caller with no readable namespace reads nothing.
    assert await code_plane_reads.user_file_dirs("o", "u", ()) == []
    assert len(ch.calls) == 1


@pytest.mark.asyncio
async def test_lesson_reads_scope_to_the_org_and_the_lesson_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    await code_plane_reads.lesson_corpus("o", ("l1", "l2"), cap=50)
    await code_plane_reads.lessons_matching("o", ["l1"], token="parse", paths=["a.py"], limit=6)
    (corpus_sql, corpus), (match_sql, match) = ch.calls
    assert "loom.memory_embeddings FINAL" in corpus_sql and corpus == {
        "org": "o",
        "nss": ["l1", "l2"],
        "cap": 50,
    }
    assert "hasToken(content_excerpt_lc" in match_sql and match["tok"] == "parse"
    assert match["nss"] == ["l1"] and match["paths"] == ["a.py"] and match["limit"] == 6
