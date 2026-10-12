"""Writes resolve the row they act on under the request's read scope.

A token pinned to user U that names `agent="*"` (or addresses a row by id)
must not delete, lock, touch, outcome or hash-probe user V's row: V's row is
simply not found. The store is faked here; it honours the scope it is given
the way `get_by_key` / `get_by_id` do, so each test shows the handler passed
the request's scope and not the whole namespace.
"""

from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.identity.domain.read_scope import (
    bind_request_read_filters,
    resolve_read_filters,
    row_in_read_scope,
)

_V_ROW = SimpleNamespace(
    memory_id="00000000-0000-0000-0000-00000000000v",
    memory_key="k",
    namespace="n",
    agent="bot",
    user_id="V",
    session_id="-",
    value="V's secret",
    pinned=False,
    lock_mode="",
    lock_until=None,
    status="active",
)


def _pinned(user: str = "U") -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        read_user_id=user,
    )


def _run_as(auth: AuthInfo | None, coro_fn: Any) -> Any:
    """Run `coro_fn()` with `auth` bound the way the transport binds it, in a
    fresh context so nothing leaks into other tests."""

    def go() -> Any:
        set_current_auth(auth)
        if auth is not None:
            bind_request_read_filters(
                auth,
                resolve_read_filters(requested=None, user_id=None, session_id=None, auth=auth),
            )
        return asyncio.run(coro_fn())

    return contextvars.Context().run(go)


@pytest.fixture
def store(monkeypatch):
    """A store holding only V's row, answering under the scope it is passed."""
    import server.memory.store as memory_repo

    calls: dict[str, list[Any]] = {"writes": [], "usage": []}

    async def get_by_key(identity, key, *, scope, **kw):
        ok = key == _V_ROW.memory_key and row_in_read_scope(
            scope, _V_ROW.user_id, _V_ROW.session_id
        )
        return _V_ROW if ok else None

    async def get_by_id(memory_id, *, scope, **kw):
        ok = memory_id == _V_ROW.memory_id and row_in_read_scope(
            scope, _V_ROW.user_id, _V_ROW.session_id
        )
        return _V_ROW if ok else None

    async def get_many_by_keys(identity, keys, *, scope, **kw):
        row = await get_by_key(identity, _V_ROW.memory_key, scope=scope)
        return [row] if row is not None and row.memory_key in keys else []

    async def record_usage_event(**kw):
        calls["usage"].append(kw)

    async def insert_versions(rows, **kw):
        calls["writes"].extend(rows)

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(memory_repo, "get_many_by_keys", get_many_by_keys)
    monkeypatch.setattr(memory_repo, "record_usage_event", record_usage_event)
    monkeypatch.setattr("server.memory.app.handlers.insert_memory_versions", insert_versions)
    monkeypatch.setattr("server.memory.app.lock.insert_memory_versions", insert_versions)
    return calls


def test_request_scope_is_the_whole_namespace_without_auth() -> None:
    from server.memory.app.provenance import request_scope

    assert contextvars.Context().run(request_scope).narrowed is False


def test_request_scope_binds_the_pinned_user() -> None:
    from server.memory.app.provenance import request_scope

    async def read():
        return request_scope()

    scope = _run_as(_pinned("U"), read)
    assert (scope.scope, scope.user_id) == ("own_user", "U")


def test_delete_with_wildcard_agent_does_not_find_another_users_row(store) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app.handlers import handle_memory_delete

    inp = MemoryDeleteInput(org="o", namespace="n", agent="*", user_id="U", memory_key="k")
    out = _run_as(_pinned("U"), lambda: handle_memory_delete(inp))
    assert out.found is False and out.tombstoned is False
    assert store["writes"] == []


def test_lock_with_wildcard_agent_does_not_find_another_users_row(store) -> None:
    from server.memory.api.models_lock import MemoryLockInput
    from server.memory.app.lock import handle_memory_lock

    inp = MemoryLockInput(
        org="o", namespace="n", agent="*", user_id="U", memory_key="k", mode="read_only", reason="r"
    )
    out = _run_as(_pinned("U"), lambda: handle_memory_lock(inp))
    assert out.locked is False and out.reason == "not_found"
    assert store["writes"] == []


def test_touch_with_wildcard_agent_does_not_find_another_users_row(store) -> None:
    from server.memory.api.models_crud import MemoryTouchInput
    from server.memory.app.handlers import handle_memory_touch

    inp = MemoryTouchInput(
        org="o", namespace="n", agent="*", user_id="U", memory_key="k", ttl_seconds=60
    )
    _run_as(_pinned("U"), lambda: handle_memory_touch(inp))
    assert store["writes"] == []


def test_if_match_probe_does_not_disclose_another_users_hash(store) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import _set_check_if_match

    inp = MemorySetInput(
        org="o",
        namespace="n",
        agent="*",
        user_id="U",
        memory_key="k",
        value="x",
        if_match_sha256="0" * 64,
    )
    w = SimpleNamespace(input=inp, existing=None)
    with pytest.raises(HTTPException) as exc:
        _run_as(_pinned("U"), lambda: _set_check_if_match(w))
    assert exc.value.status_code == 409
    assert exc.value.detail["current_sha256"] == ""


@pytest.mark.parametrize("by", ["id", "key"])
def test_outcome_on_another_users_row_is_not_recorded(store, monkeypatch, by) -> None:
    import server.core.ch as ch_mod
    from server.memory.api.models_temporal import MemoryOutcomeInput
    from server.memory.app.ops import handle_memory_outcome

    class _NoCH:
        def __getattr__(self, name):
            raise AssertionError("no counter may be read or written for a foreign row")

    monkeypatch.setattr(ch_mod, "get_client", lambda: _NoCH())
    target = {"memory_id": _V_ROW.memory_id} if by == "id" else {"memory_key": "k"}
    inp = MemoryOutcomeInput(org="o", namespace="n", outcome="negative", weight=100, **target)
    out = _run_as(_pinned("U"), lambda: handle_memory_outcome(inp))
    assert out.recorded is False and out.quarantined is False
    assert store["usage"] == []


def test_outcome_by_id_in_another_namespace_is_not_recorded(store, monkeypatch) -> None:
    import server.core.ch as ch_mod
    from server.memory.api.models_temporal import MemoryOutcomeInput
    from server.memory.app.ops import handle_memory_outcome

    monkeypatch.setattr(ch_mod, "get_client", lambda: None)
    inp = MemoryOutcomeInput(
        org="o", namespace="other", memory_id=_V_ROW.memory_id, outcome="positive"
    )
    out = _run_as(None, lambda: handle_memory_outcome(inp))
    assert out.recorded is False
    assert store["usage"] == []


def test_outcome_by_id_in_scope_is_recorded(store, monkeypatch) -> None:
    import server.core.ch as ch_mod
    from server.memory.api.models_temporal import MemoryOutcomeInput
    from server.memory.app.ops import handle_memory_outcome

    class _CH:
        async def query_one(self, sql, params=None):
            return {"evidence_count": 1}

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(
        "server.memory.app.recall_credit.note_closer_from_request", lambda *a, **k: None
    )
    inp = MemoryOutcomeInput(org="o", namespace="n", memory_id=_V_ROW.memory_id, outcome="positive")
    out = _run_as(_pinned("V"), lambda: handle_memory_outcome(inp))
    assert out.recorded is True
    assert [e["event_type"] for e in store["usage"]] == ["evidence"]


def test_run_credit_lookup_carries_the_scope() -> None:
    from server.identity.domain.read_scope import ReadFilters
    from server.tasks.store.runs import live_memories_by_key

    seen: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql, params=None):
            seen.update(sql=sql, params=params)
            return []

    scope = ReadFilters(scope="own_user", user_id="U", session_id=None)
    asyncio.run(live_memories_by_key(_CH(), "o", "n", ["k"], scope=scope))
    assert "user_id = {scope_uid:String}" in seen["sql"]
    assert seen["params"]["scope_uid"] == "U"


class _SqlCH:
    """Records every statement; answers nothing."""

    def __init__(self) -> None:
        self.sql: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.sql.append((sql, dict(params or {})))
        return []


def _scoped_to_u(ch: _SqlCH) -> bool:
    return any("user_id = {scope_uid:String}" in s and p.get("scope_uid") == "U" for s, p in ch.sql)


def test_same_key_supersession_reads_only_the_writers_rows() -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    ch = _SqlCH()
    rows = [{"memory_key": "k", "memory_id": "m"}]
    _run_as(
        _pinned("U"),
        lambda: supersede_same_key_rows(
            org="o", namespace="n", new_rows=rows, ch=ch, policy="newest_wins"
        ),
    )
    assert _scoped_to_u(ch)


def test_value_update_peers_are_the_writers_rows() -> None:
    from server.memory.app.value_updates import _peers, _peers_many

    ch = _SqlCH()
    _run_as(
        _pinned("U"),
        lambda: _peers_many(
            ch, org="o", namespace="n", items=[("semantic", "k", [0.1])], min_cosine=0.5
        ),
    )
    _run_as(
        _pinned("U"),
        lambda: _peers(
            ch,
            org="o",
            namespace="n",
            memory_type="semantic",
            memory_key="k",
            embedding=[0.1],
            min_cosine=0.5,
        ),
    )
    assert len(ch.sql) == 2
    assert all("m.user_id = {scope_uid:String}" in s for s, _ in ch.sql)


def test_recall_echo_keys_are_the_callers_keys(monkeypatch) -> None:
    import server.core.ch as ch_mod
    from server.memory.app.pipeline import _existing_keys_named
    from server.memory.app.pipeline_state import WorkflowState

    ch = _SqlCH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    state = WorkflowState(org="o", namespace="n", user_id="U")
    _run_as(_pinned("U"), lambda: _existing_keys_named(state, {"k"}))
    assert _scoped_to_u(ch)


def test_run_retention_touches_only_the_callers_rows() -> None:
    from server.memory.app.run_retention import load_run_rows

    ch = _SqlCH()
    _run_as(_pinned("U"), lambda: load_run_rows(ch, org="o", namespace="n", run_id="r", limit=5))
    assert _scoped_to_u(ch)


def test_bulk_dedup_keeper_is_in_the_callers_session(monkeypatch) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import _bulk_identity_dupes
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_bulk_dedup_scope", "identity")
    auth = AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        read_scope="own_session",
    )
    inp = MemoryBulkSetInput(
        org="o",
        namespace="n",
        agent="a",
        user_id="U",
        session_id="S",
        entries=[{"memory_key": "k", "value": "v"}],
    )
    ch = _SqlCH()

    def go() -> Any:
        set_current_auth(auth)
        bind_request_read_filters(
            auth, resolve_read_filters(requested=None, user_id="U", session_id="S", auth=auth)
        )
        return asyncio.run(_bulk_identity_dupes(ch, inp))

    contextvars.Context().run(go)
    sql, params = ch.sql[0]
    assert "session_id = {scope_sid:String}" in sql and params["scope_sid"] == "S"
