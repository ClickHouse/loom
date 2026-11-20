"""Lease-store and erasure-ledger seams for private queue writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from server.core import leases
from server.erasure.app import ledger_reads


def install(monkeypatch: Any) -> SimpleNamespace:
    state = SimpleNamespace(holders={}, reads=[], erasures=[], failure=None)

    async def take(name: str, **kw: Any) -> Any:
        if name in state.holders:
            return None
        lease = SimpleNamespace(name=name, retained=False)
        lease.let_expire = lambda: setattr(lease, "retained", True)
        state.holders[name] = lease
        return lease

    async def release(lease: Any) -> None:
        if not lease.retained:
            del state.holders[lease.name]

    async def ledger(*args: Any, **kw: Any) -> Any:
        state.reads.append(args)
        if state.failure:
            raise state.failure
        return state.erasures

    monkeypatch.setattr(leases, "take", take)
    monkeypatch.setattr(leases, "release_shielded", release)
    monkeypatch.setattr(leases, "work_left", lambda *args: 20.0)
    monkeypatch.setattr(ledger_reads, "unfinished_reaching", AsyncMock(side_effect=ledger))
    return state
