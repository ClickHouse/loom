"""The code and tracker registrars read and write a directory row under the namespace's
account claim, which every other writer of the whole row holds (`namespace.register`,
`code.repo.set_access`), so neither writes back a row read before another's change."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server.code.app.index.registry as code_registry
import server.tracker.app.registry as tracker_registry
from server.auth import grant_claims
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
def claims(monkeypatch: pytest.MonkeyPatch) -> MemoryClaims:
    claims = MemoryClaims()
    monkeypatch.setattr(grant_claims, "take", claims.take)
    code_registry.reset_for_tests()
    tracker_registry.reset_for_tests()
    return claims


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
    claims: MemoryClaims,
) -> None:
    client = _Client(claims)
    cfg = repo_config(org=ORG, namespace="code-p", repo="ClickHouse/p", git_branch="main")
    async with grant_claims.account_change(ORG, "code-p", wait_s=0):
        assert await code_registry.ensure_registered(cfg, ch=client, sync=False, account_held=True)


@pytest.mark.asyncio
async def test_the_tracker_registrar_writes_under_the_account_claim(
    claims: MemoryClaims, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.tracker.store.source import qa_intel

    client = _Client(claims)
    monkeypatch.setattr(qa_intel, "configured", lambda: True)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces",
        lambda: {"qa-p": SimpleNamespace(org=ORG, restricted=True)},
    )
    assert await tracker_registry.ensure_configured_tracker_namespaces(ch=client) == ["qa-p"]
    assert client.held_at_insert == [{_account("qa-p")}]
