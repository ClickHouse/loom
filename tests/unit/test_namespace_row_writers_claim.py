"""The code and tracker registrars read and write a directory row under the namespace's
account claim, which every other writer that reads and rewrites the row holds
(`namespace.register`, `code.repo.set_access`), so neither writes back a row read before
another's change."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

import server.boot as boot
import server.code.app.index.registry as code_registry
import server.tracker.app.registry as tracker_registry
import server.tracker.app.registry_sources as sources
from server.auth import grant_claims
from server.tracker.store.source import qa_intel
from server.workers import leader
from tests.unit.claims_fake import MemoryClaims
from tests.unit.code_fixtures import repo_config

ORG = "clickhouse"


class _Client:
    def __init__(self, claims: MemoryClaims) -> None:
        self.claims = claims
        self.held_at_insert: list[set[str]] = []

    async def query_one(self, sql, params=None):
        return None

    async def insert(self, table, rows):
        self.held_at_insert.append(set(self.claims.held))


@pytest.fixture
def claims(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryClaims]:
    claims = MemoryClaims()
    monkeypatch.setattr(grant_claims, "take", claims.take)
    code_registry.reset_for_tests()
    tracker_registry.reset_for_tests()
    yield claims
    code_registry.reset_for_tests()
    tracker_registry.reset_for_tests()


def _account(ns: str) -> str:
    return grant_claims._account(ORG, ns)[0]


@pytest.mark.asyncio
async def test_the_code_registrar_writes_under_the_account_claim(claims: MemoryClaims) -> None:
    client = _Client(claims)
    cfg = repo_config(org=ORG, namespace="code-p", repo="ClickHouse/p", git_branch="main")
    assert await code_registry.ensure_registered(cfg, ch=client, sync=False)
    assert client.held_at_insert and all(_account("code-p") in h for h in client.held_at_insert)
    assert claims.taken[0] == _account("code-p")  # before the admins claim


@pytest.mark.asyncio
async def test_a_caller_holding_the_account_claim_registers_under_it(
    claims: MemoryClaims, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grant_claims, "WAIT_S", 0.2)  # a wait on its own claim fails fast
    client = _Client(claims)
    cfg = repo_config(org=ORG, namespace="code-p", repo="ClickHouse/p", git_branch="main")
    async with grant_claims.account_change(ORG, "code-p", wait_s=0):
        assert await code_registry.ensure_registered(cfg, ch=client, sync=False, account_held=True)
        assert client.held_at_insert and all(_account("code-p") in h for h in client.held_at_insert)


@pytest.mark.asyncio
async def test_the_tracker_registrar_writes_under_the_account_claim(
    claims: MemoryClaims, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client(claims)
    monkeypatch.setattr(qa_intel, "configured", lambda: True)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces",
        lambda: {"qa-p": SimpleNamespace(org=ORG, restricted=True)},
    )
    assert await tracker_registry.ensure_configured_tracker_namespaces(ch=client) == ["qa-p"]
    assert client.held_at_insert == [{_account("qa-p")}]


@pytest.mark.asyncio
async def test_a_namespace_skipped_at_boot_is_registered_on_a_later_try(
    claims: MemoryClaims, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its account claim busy at boot, then a failed pass: the narrowing must not wait for
    the next boot, and the retry ends once this process no longer leads."""
    refreshes: list[int] = []

    async def _refresh(**_):
        refreshes.append(1)
        if len(refreshes) == 2:
            raise TimeoutError("ClickHouse did not answer")

    client = _Client(claims)
    leads = [True]
    monkeypatch.setattr(qa_intel, "configured", lambda: True)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces",
        lambda: {"qa-p": SimpleNamespace(org=ORG, restricted=True)},
    )
    monkeypatch.setattr(sources, "refresh_db_sources", _refresh)
    monkeypatch.setattr(leader, "leads_deployment", lambda: leads[0])
    monkeypatch.setattr(boot, "_tracker_retry_s", lambda: 0.01)
    monkeypatch.setattr(grant_claims, "WAIT_S", 0.01)
    monkeypatch.setattr(boot, "_TRACKER_RETRY", None)
    claims.held.add(_account("qa-p"))  # another change holds it
    await boot.register_tracker_namespaces(client)  # skipped once the wait is spent
    assert client.held_at_insert == []
    claims.held.discard(_account("qa-p"))
    await asyncio.wait_for(boot._TRACKER_RETRY, timeout=5)
    assert len(refreshes) == 3 and client.held_at_insert == [{_account("qa-p")}]

    # A running retry ends once the process no longer leads, writing nothing.
    tracker_registry.reset_for_tests()
    claims.held.add(_account("qa-p"))
    monkeypatch.setattr(boot, "_TRACKER_RETRY", None)
    await boot.register_tracker_namespaces(client)
    leads[0] = False
    await asyncio.wait_for(boot._TRACKER_RETRY, timeout=5)
    assert client.held_at_insert == [{_account("qa-p")}]

    finished = boot._TRACKER_RETRY
    await boot.register_tracker_namespaces(client)  # not leading: no retry starts
    assert boot._TRACKER_RETRY is finished
