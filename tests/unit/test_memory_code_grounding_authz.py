"""memory.ask's code-grounding hop reads the code namespace as the current
principal: no right to read it → all four legs are no-ops
and the ask proceeds; a restricted target that passes leaves one audit row."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.memory.app import code_grounding as g
from server.operations.base import Role
from server.settings import Settings, settings

ORG, CODE_NS = "o", "code-x"


def _settings() -> Settings:
    return settings.model_copy(
        update=dict(
            memory_ask_code_namespace=CODE_NS,
            memory_ask_code_org=ORG,
            code_org=ORG,
            memory_ask_pr_temporal_gate=True,
            memory_ask_code_enrichment=True,
            memory_ask_pr_grounding=True,
            memory_ask_code_staleness=True,
        )
    )


def _input() -> SimpleNamespace:
    return SimpleNamespace(org=ORG, namespace="mem", code_namespace="", code_org="", as_of=None)


@pytest.fixture
def legs(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Every leg's downstream read is counted; none must fire when denied."""
    calls = {"search": 0, "symbol": 0, "prs": 0}
    import server.code.app.search.handler as hs
    import server.code.app.symbol as hy
    import server.code.domain.idents as cs
    import server.code.store.index as cr

    monkeypatch.setattr(cs, "embedded_idents", lambda q: {"FooBar"})

    async def fake_search(inp: Any) -> Any:
        calls["search"] += 1
        return SimpleNamespace(abstain=True, results=[], as_of={})

    async def fake_symbol(inp: Any) -> Any:
        calls["symbol"] += 1
        return SimpleNamespace(definitions=[])

    class _Repo:
        async def prs_by_number(self, org, ns, numbers):
            calls["prs"] += 1
            return {}

    monkeypatch.setattr(hs, "handle_code_search", fake_search)
    monkeypatch.setattr(hy, "handle_code_symbol", fake_symbol)
    monkeypatch.setattr(cr, "CodeRepo", _Repo)
    yield calls
    set_current_auth(None)
    nc.reset()


def _mem(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key="k", memory_type="semantic", content_excerpt=text, created_at=None, valid_at=None
    )


@pytest.mark.asyncio
async def test_unauthorized_principal_makes_every_leg_a_noop(legs, monkeypatch) -> None:
    async def _deny(*a, **k):
        return False

    monkeypatch.setattr(authz, "row_in_scope", _deny)
    set_current_auth(
        AuthInfo(
            subject="b@x",
            org=ORG,
            namespaces=("mem",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    pool = [_mem("see PR #12345 and FooBar")]
    out = await g.apply_code_grounding("FooBar?", _input(), pool, _settings())
    assert out == pool
    assert legs == {"search": 0, "symbol": 0, "prs": 0}


@pytest.mark.asyncio
async def test_authorized_principal_runs_the_legs_once_resolved(legs, monkeypatch) -> None:
    async def _allow(*a, **k):
        return True

    monkeypatch.setattr(authz, "row_in_scope", _allow)
    set_current_auth(
        AuthInfo(
            subject="a@x",
            org=ORG,
            namespaces=("mem", CODE_NS),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    pool = [_mem("see PR #12345 and FooBar")]
    await g.apply_code_grounding("FooBar?", _input(), pool, _settings())
    assert legs["search"] >= 1 and legs["prs"] >= 1


@pytest.mark.asyncio
async def test_restricted_target_leaves_one_audit_row(legs, monkeypatch) -> None:
    async def _allow(*a, **k):
        return True

    monkeypatch.setattr(authz, "row_in_scope", _allow)
    nc._cache[ORG] = (1e12, frozenset({CODE_NS}))
    rows: list[Any] = []

    async def _emit(event_type, **kw):
        rows.append((event_type, kw["namespace"], kw["subject"]["op"]))
        return "h"

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    set_current_auth(
        AuthInfo(
            subject="a@x",
            org=ORG,
            namespaces=(CODE_NS,),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    await g.apply_code_grounding("FooBar?", _input(), [_mem("PR #1")], _settings())
    assert rows == [("namespace.restricted_access", CODE_NS, "memory.ask.grounding")]


@pytest.mark.asyncio
async def test_auth_disabled_grounds_as_before(legs) -> None:
    set_current_auth(None)
    await g.apply_code_grounding("FooBar?", _input(), [_mem("PR #1")], _settings())
    assert legs["search"] >= 1


@pytest.mark.asyncio
async def test_each_leg_authorizes_when_called_alone(legs, monkeypatch) -> None:
    async def _deny(*a, **k):
        return False

    monkeypatch.setattr(authz, "row_in_scope", _deny)
    set_current_auth(
        AuthInfo(
            subject="b@x",
            org=ORG,
            namespaces=("mem",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    assert await g._inject_code_context("FooBar?", _input(), _settings()) == []
    assert await g._inject_pr_facts("PR #5", [], _input(), _settings()) == []
    pool = [_mem("PR #5")]
    assert (
        await g._gate_future_prs(
            pool,
            SimpleNamespace(
                **{**vars(_input()), "as_of": __import__("datetime").datetime(2026, 1, 1)}
            ),
            _settings(),
        )
        == pool
    )
    assert legs == {"search": 0, "symbol": 0, "prs": 0}
