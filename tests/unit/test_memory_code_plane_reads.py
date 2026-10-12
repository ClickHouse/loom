"""The memory reads the code plane serves from: code.search's recent-directory
tie-break reads the user's files through memory, one parent per directory."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.domain.read_scope import (
    WHOLE_NAMESPACE,
    ReadFilters,
    bind_request_read_filters,
)
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
    dirs = await code_plane_reads.user_file_dirs("o", "u", None, scope=WHOLE_NAMESPACE)
    assert dirs == ["a/b", "a", "top.py"]
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
    assert await code_plane_reads.user_file_dirs("o", "u", (), scope=WHOLE_NAMESPACE) == []
    assert len(ch.calls) == 1


def _recent_dirs_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _CH]:
    from server.code.app.search import handler

    ch = _CH([{"f": "a/x.py"}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _mine(org: str, subject: str | None = None) -> list[str]:
        return ["ns1"]

    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _mine)
    monkeypatch.setattr(handler, "_RECENT_DIR_CACHE", {})
    return handler, ch


@pytest.mark.asyncio
async def test_recent_dirs_read_only_the_own_session_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.principals import acting_as, principal

    handler, ch = _recent_dirs_setup(monkeypatch)
    auth = principal("viewer", org="o", namespaces=("ns1",))
    try:
        with acting_as(auth):
            bind_request_read_filters(auth, ReadFilters("own_session", "u", "s1"))
            assert await handler._user_recent_dirs("o", "u") == ["a"]
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    ((sql, params),) = ch.calls
    assert "session_id = {scope_sid:String}" in sql
    assert params["scope_sid"] == "s1" and params["scope_uid"] == "u"


@pytest.mark.asyncio
async def test_two_sessions_never_share_a_recent_dirs_cache_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.principals import acting_as, principal

    handler, ch = _recent_dirs_setup(monkeypatch)
    auth = principal("viewer", org="o", namespaces=("ns1",))
    try:
        with acting_as(auth):
            for sid in ("s1", "s2"):
                bind_request_read_filters(auth, ReadFilters("own_session", "u", sid))
                await handler._user_recent_dirs("o", "u")
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    assert [params["scope_sid"] for _, params in ch.calls] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_an_unresolved_read_scope_personalizes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import IdentityIncompleteError
    from tests.unit.principals import acting_as, principal

    handler, ch = _recent_dirs_setup(monkeypatch)
    auth = principal("viewer", org="o", namespaces=("ns1",))
    try:
        with acting_as(auth):
            bind_request_read_filters(auth, IdentityIncompleteError("no session_id"))
            assert await handler._user_recent_dirs("o", "u") == []
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    assert ch.calls == []


def test_code_search_runs_as_the_pinned_user_whatever_the_body_says() -> None:
    from server.code.api.models_search import CodeSearchInput
    from server.core.errors import AuthInsufficientError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, bind_request_read_filters
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates
    from tests.unit.principals import principal

    op = OPERATIONS_BY_NAME["code.search"]
    pinned = principal("viewer", org="o", namespaces=("ns1",), read_user_id="u1")
    try:
        out = gates.read_scope(op, pinned, CodeSearchInput(query="q"))
        assert out.user_id == "u1"  # the user whose recent files break ties
        with pytest.raises(AuthInsufficientError):
            gates.read_scope(op, pinned, CodeSearchInput(query="q", user_id="u2"))
        body = CodeSearchInput(query="q", user_id="u2")
        unpinned = principal("viewer", org="o", namespaces=("ns1",))
        assert gates.read_scope(op, unpinned, body) is body
    finally:
        bind_request_read_filters(None, WHOLE_NAMESPACE)


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
