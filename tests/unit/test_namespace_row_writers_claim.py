"""The code and tracker registrars read and write a directory row under the namespace's
account claim, which every other writer that reads and rewrites the row holds
(`namespace.register`, `code.repo.set_access`), so neither writes back a row read before
another's change."""

from __future__ import annotations

from collections.abc import Iterator
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
    from server.tracker.store.source import qa_intel

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
    import asyncio

    import server.boot as boot
    import server.tracker.app.registry_sources as sources
    from server.tracker.store.source import qa_intel
    from server.workers import leader

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


def test_the_gate_reads_a_restricted_tracker_namespace_from_its_config(monkeypatch) -> None:
    """A narrowing still waiting for its claim leaves the row standard; the gate and the
    org-wide restricted set fail closed from the configuration meanwhile. An entry whose
    repositories cannot be read stays restricted, serving none, and errors no call."""
    import server.tracker.domain.sources as tracker_sources
    import server.tracker.store.source as tracker_store
    from server.identity.app import namespace_classification as nc
    from server.identity.app.directory import IdentityDirectory
    from server.settings import settings
    from tests.unit._tracker_config import tracker_sources_json

    tracker_store.reset_cache_for_tests()
    tracker_sources.reset_warnings_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG)
    configured = tracker_sources_json(
        {"Acme/private": "qa-p", "Acme/public": "qa-open"},
        restricted="qa-p",
        extra_namespaces={"qa-bad": {"source": "qa", "repos": 5, "restricted": True}},
    )
    monkeypatch.setattr(settings, "tracker_sources_json", configured)
    try:
        assert nc.is_restricted_cached(ORG, "qa-p")
        assert not nc.is_restricted_cached(ORG, "qa-open")
        assert not nc.is_restricted_cached("other-org", "qa-p")
        assert nc.config_classification(ORG, "memory-ns") == "standard"
        assert nc.config_classification(ORG, "qa-bad") == "restricted"
        assert tracker_sources.namespace_entries()["qa-bad"].repos == ()
        assert IdentityDirectory().restricted_config_namespaces(ORG) >= {"qa-p", "qa-bad"}
    finally:
        tracker_store.reset_cache_for_tests()
