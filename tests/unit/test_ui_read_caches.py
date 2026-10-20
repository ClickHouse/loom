"""The two slow dashboard reads answer from a
process cache within their TTL."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import server.insights.app.agents as agents
from server.code.app import index_status
from server.insights.api.models_agents import AgentsConnectedInput
from server.settings import settings


@pytest.mark.asyncio
async def test_agents_connected_serves_the_same_scope_from_cache_for_a_minute(monkeypatch) -> None:
    agents.reset_connected_cache()
    calls: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None):
            calls.append(sql)
            now = datetime(2026, 9, 18, 12)
            return [
                {
                    "agent": "a",
                    "last_seen": now,
                    "first_seen": now,
                    "ops": 1,
                    "namespaces_touched": 1,
                    "primary_namespace": "ns",
                    "cost_usd": 0.0,
                }
            ]

    monkeypatch.setattr(agents, "get_client", lambda: _CH())
    inp = AgentsConnectedInput(org="acme", namespace="ns", window_hours=24)
    a = await agents.handle_agents_connected(inp)
    b = await agents.handle_agents_connected(inp)
    assert a is b and len(calls) == 1
    # a different namespace is a different question
    await agents.handle_agents_connected(
        AgentsConnectedInput(org="acme", namespace="other", window_hours=24)
    )
    assert len(calls) == 2
    # the TTL is the only thing that re-asks
    monkeypatch.setattr(agents.time, "monotonic", lambda: 1e12)
    await agents.handle_agents_connected(inp)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_repo_status_is_computed_once_per_repository_within_the_ttl(monkeypatch) -> None:
    index_status.reset_repo_status_cache()
    n = {"calls": 0}

    async def _status(cfg: Any):
        n["calls"] += 1
        return SimpleNamespace(org=cfg.org, namespace=cfg.namespace)

    monkeypatch.setattr(index_status, "_repo_status", _status)
    cfg = SimpleNamespace(org="acme", namespace="code-x")
    a = await index_status._repo_status_cached(cfg)
    b = await index_status._repo_status_cached(cfg)
    assert a is b and n["calls"] == 1
    await index_status._repo_status_cached(SimpleNamespace(org="acme", namespace="code-y"))
    assert n["calls"] == 2
    index_status.reset_repo_status_cache()
    await index_status._repo_status_cached(cfg)
    assert n["calls"] == 3
    index_status.reset_repo_status_cache()  # leave nothing for the next test file


@pytest.mark.asyncio
async def test_a_stale_status_is_served_at_once_and_refreshed_behind(monkeypatch) -> None:
    import asyncio

    index_status.reset_repo_status_cache()
    n = {"calls": 0}

    async def _status(cfg: Any):
        n["calls"] += 1
        return SimpleNamespace(org=cfg.org, namespace=cfg.namespace, v=n["calls"])

    monkeypatch.setattr(index_status, "_repo_status", _status)
    cfg = SimpleNamespace(org="acme", namespace="code-x")
    first = await index_status._repo_status_cached(cfg)
    assert first.v == 1
    # age the entry past the TTL
    key = (cfg.org, cfg.namespace)
    at, st = index_status._status_cache[key]
    index_status._status_cache[key] = (at - settings.code_status_ttl_s - 1, st)
    stale = await index_status._repo_status_cached(cfg)
    assert stale is first  # served at once, no wait
    await asyncio.sleep(0)  # let the background refresh run
    await asyncio.sleep(0)
    fresh = await index_status._repo_status_cached(cfg)
    assert fresh.v == 2 and n["calls"] == 2
    index_status.reset_repo_status_cache()
