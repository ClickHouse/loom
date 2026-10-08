"""memory.ask's own `loom.memory` reads carry the identity memory.search
would apply, and a token pinned to one end user pins the whole ask.

Pure unit: a fake ClickHouse client records each statement.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import server.core.ch as ch_mod
from server.auth.jwt import AuthInfo
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.ask import pipeline, reader, supersession
from server.memory.app.ask.completion import bridge_rows_by_key
from server.memory.app.ask.identity import memory_identity_where


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.calls.append((sql, dict(params or {})))
        return []


def _input(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        org="acme",
        namespace="support",
        agent="bot",
        scope="private",
        user_id="u1",
        session_id=None,
        run_id=None,
        session_ids=[],
        query="where does bernard arnault live",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    fake = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    return fake


def test_identity_where_mirrors_the_search_predicates() -> None:
    where, params = memory_identity_where(_input(session_id="s1", run_id="r1")) or ([], {})
    assert where == [
        "org = {org:String}",
        "namespace = {ns:String}",
        "agent = {agent:String}",
        "user_id = {user_id:String}",
        "session_id = {session_id:String}",
        "run_id = {run_id:String}",
    ]
    assert params == {
        "org": "acme",
        "ns": "support",
        "agent": "bot",
        "user_id": "u1",
        "session_id": "s1",
        "run_id": "r1",
    }
    # A wildcard agent and an unset user read across both, as memory.search does.
    where, _ = memory_identity_where(_input(agent="*", user_id=None)) or ([], {})
    assert where == ["org = {org:String}", "namespace = {ns:String}"]
    where, _ = memory_identity_where(_input(), alias="m") or ([], {})
    assert "m.user_id = {user_id:String}" in where
    # loom.memory holds private rows only: a shared ask reads nothing there.
    assert memory_identity_where(_input(scope="shared")) is None


def test_bridge_read_is_bounded_to_the_callers_user(ch: _CH) -> None:
    asyncio.run(bridge_rows_by_key(_input(), "Bernard Arnault"))
    [(sql, params)] = ch.calls
    assert "user_id = {user_id:String}" in sql and params["user_id"] == "u1"
    assert "agent = {agent:String}" in sql and params["pat"] == "%bernard_arnault%"


def test_mask_and_trust_reads_are_bounded_to_the_callers_user(ch: _CH) -> None:
    hits = [
        SimpleNamespace(memory_key="user_city", source="fact"),
        SimpleNamespace(memory_key="chunk:s:1", source="chunk"),
    ]
    asyncio.run(supersession.superseded_rows_for_pool(_input(), hits))
    asyncio.run(reader._trusted_in_pool(_input(), hits))
    (mask_sql, mask_params), (trust_sql, trust_params) = ch.calls
    assert "m.user_id = {user_id:String}" in mask_sql
    # the successor subquery reads under the same identity
    assert mask_sql.count("user_id = {user_id:String}") == 2
    assert "user_id = {user_id:String}" in trust_sql
    assert mask_params["user_id"] == trust_params["user_id"] == "u1"


def test_shared_scope_ask_issues_no_private_read(ch: _CH) -> None:
    shared = _input(scope="shared")
    hits = [SimpleNamespace(memory_key="k", source="chunk"), SimpleNamespace(memory_key="k2")]
    assert asyncio.run(bridge_rows_by_key(shared, "Bernard Arnault")) == []
    assert asyncio.run(supersession.superseded_rows_for_pool(shared, hits)) == []
    assert asyncio.run(reader._trusted_in_pool(shared, hits)) == []
    assert ch.calls == []


def _pinned_auth() -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org="acme",
        namespaces=("support",),
        roles=(),
        is_service_account=True,
        read_user_id="u1",
    )


def test_a_pinned_token_pins_the_ask_and_drops_precedent_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.auth.context as auth_ctx

    monkeypatch.setattr(auth_ctx, "current_auth", _pinned_auth)
    body = MemorySearchInput(org="acme", namespace="support", query="q")
    pinned, key = pipeline._pin_identity(body, "prec-key")
    assert pinned.user_id == "u1" and pinned.read_scope == "own_user"
    # The handler hashed the precedent shape without the pinned user.
    assert key == ""

    # A body that already names the pinned user is unchanged, precedent kept.
    named = MemorySearchInput(
        org="acme", namespace="support", query="q", user_id="u1", read_scope="own_user"
    )
    same, key = pipeline._pin_identity(named, "prec-key")
    assert same is named and key == "prec-key"

    with pytest.raises(PermissionError):
        pipeline._pin_identity(
            MemorySearchInput(org="acme", namespace="support", query="q", user_id="u2"), "k"
        )


def test_no_token_leaves_the_ask_as_the_body_says(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.context as auth_ctx

    monkeypatch.setattr(auth_ctx, "current_auth", lambda: None)
    body = MemorySearchInput(org="acme", namespace="support", query="q")
    same, key = pipeline._pin_identity(body, "prec-key")
    assert same is body and key == "prec-key"
