"""The code namespace registry writes the classification on a
fresh row and narrows an existing standard row when the config says
restricted — mirroring every column — and never widens one."""

from __future__ import annotations

import dataclasses

import pytest

import server.code.app.index.registry as registry
import server.identity.app.namespace_classification as nc
from tests.unit.code_fixtures import repo_config


class _Client:
    def __init__(self, existing=None) -> None:
        self.existing = existing
        self.inserted: list[tuple[str, list[dict]]] = []

    async def query_one(self, sql, params=None):
        return self.existing

    async def insert(self, table, rows):
        self.inserted.append((table, rows))


def _cfg(classification="standard", org_readable=False):
    return repo_config(
        org="clickhouse",
        namespace="code-p",
        repo="ClickHouse/example-private",
        git_branch="master",
        classification=classification,
        org_readable=org_readable,
    )


@pytest.fixture(autouse=True)
def _reset():
    registry.reset_for_tests()
    nc.reset()
    yield
    registry.reset_for_tests()
    nc.reset()


@pytest.mark.asyncio
async def test_fresh_restricted_row_is_private_and_restricted() -> None:
    client = _Client()
    assert await registry.ensure_registered(_cfg("restricted", org_readable=True), ch=client)
    ((_table, rows),) = client.inserted
    row = rows[0]
    assert row["classification"] == "restricted"
    assert row["visibility"] == "private"  # org_readable never makes a restricted row public
    assert row["kind"] == "code"


@pytest.mark.asyncio
async def test_fresh_standard_row_carries_standard() -> None:
    client = _Client()
    await registry.ensure_registered(_cfg("standard", org_readable=True), ch=client)
    row = client.inserted[0][1][0]
    # Every code row is private; join_policy carries openness.
    assert row["classification"] == "standard" and row["visibility"] == "private"


@pytest.mark.asyncio
async def test_existing_standard_row_is_narrowed_in_full() -> None:
    existing = {
        "org": "clickhouse",
        "namespace": "code-p",
        "owner_email": "ops@x",
        "description": "d",
        "tags": ["code"],
        "active": True,
        "kind": "code",
        "visibility": "public",
        "classification": "standard",
        "retention_days": 7,
        "contact_email": "c@x",
        "password_hash": "h",
    }
    client = _Client(existing=existing)
    nc._cache["clickhouse"] = (1e12, frozenset())
    assert await registry.ensure_registered(_cfg("restricted"), ch=client) is False
    ((_table, rows),) = client.inserted
    row = rows[0]
    assert row["classification"] == "restricted" and row["visibility"] == "private"
    # every other column rides along (ReplacingMergeTree resets what is omitted)
    for k in ("owner_email", "retention_days", "contact_email", "password_hash", "tags"):
        assert row[k] == existing[k]
    assert "code-p" in nc._cache["clickhouse"][1]  # enforced in-process at once


@pytest.mark.asyncio
async def test_existing_restricted_row_is_never_widened() -> None:
    existing = {
        "org": "clickhouse",
        "namespace": "code-p",
        "classification": "restricted",
        "visibility": "private",
    }
    client = _Client(existing=existing)
    await registry.ensure_registered(_cfg("standard", org_readable=True), ch=client)
    assert client.inserted == []


@pytest.mark.asyncio
async def test_existing_row_already_restricted_is_left_alone() -> None:
    existing = {"org": "clickhouse", "namespace": "code-p", "classification": "restricted"}
    client = _Client(existing=existing)
    await registry.ensure_registered(_cfg("restricted"), ch=client)
    assert client.inserted == []


@pytest.mark.parametrize("access", ["standard", "restricted"])
@pytest.mark.asyncio
async def test_a_maintainer_s_widening_reaches_the_row_only_while_its_access_row_says_so(
    monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    """The snapshot says a maintainer opened it; the access row read now decides, since a
    restriction may have landed after the snapshot was read."""
    existing = {"org": "clickhouse", "namespace": "code-p", "classification": "restricted"}

    class _Access(_Client):
        async def query_one(self, sql, params=None):
            if "loom.code_repos" in sql:
                return {"classification": access}
            return self.existing

    client = _Access(existing=existing)
    cfg = dataclasses.replace(_cfg("standard"), access_source="registry")
    held = _hold_admins(monkeypatch, client)
    await registry.ensure_registered(cfg, ch=client)
    widened = [r["classification"] for _, rows in client.inserted for r in rows]
    assert widened == (["standard"] if access == "standard" else [])
    assert held == (["code-p"] if access == "standard" else [])


def _hold_admins(monkeypatch: pytest.MonkeyPatch, client: _Client) -> list[str]:
    """The namespaces whose admins claim was held at each directory write."""
    from contextlib import asynccontextmanager

    from server.auth import grant_claims

    real, holding, held = grant_claims.admins_change, [], []

    @asynccontextmanager
    async def _admins(org: str, ns: str):
        async with real(org, ns):
            holding.append(ns)
            yield
            holding.remove(ns)

    write = client.insert

    async def _insert(table, rows):
        held.extend(holding)
        await write(table, rows)

    monkeypatch.setattr(grant_claims, "admins_change", _admins)
    monkeypatch.setattr(client, "insert", _insert)
    return held


@pytest.mark.parametrize("existing", [None, {"classification": "standard"}])
@pytest.mark.asyncio
async def test_the_registrar_reads_and_writes_the_row_under_the_admins_claim(
    monkeypatch: pytest.MonkeyPatch, existing: dict | None
) -> None:
    """`code.repo.set_access` restricts under the same claim: a read made before it, or
    on a lagging replica, must not open, widen or recreate a restricted row."""
    from server.core.ch import sequential_reads_active

    reads: list[bool] = []

    class _Fresh(_Client):
        async def query_one(self, sql, params=None):
            reads.append(sequential_reads_active())
            if "loom.code_repos" in sql:
                return None  # no access row: the operator's entry says public
            return dict(self.existing) if self.existing else None

    client = _Fresh(existing=existing)
    held = _hold_admins(monkeypatch, client)
    await registry.ensure_registered(dataclasses.replace(_cfg("standard"), public=True), ch=client)
    assert reads == ([True] if existing is None else [True, True])
    assert [r["join_policy"] for _, rows in client.inserted for r in rows] == ["open"]
    assert held == ["code-p"]


@pytest.mark.parametrize("join_policy", ["open", "approval"])
@pytest.mark.asyncio
async def test_a_page_branch_opens_only_while_its_registry_row_says_open(join_policy: str) -> None:
    """The snapshot may predate a maintainer closing it to approval."""

    class _Registry(_Client):
        async def query_one(self, sql, params=None):
            if "loom.code_repos" in sql:
                return {"classification": "standard", "join_policy": join_policy}
            return {"classification": "standard", "join_policy": "approval"}

    client = _Registry()
    cfg = dataclasses.replace(_cfg("standard"), public=True, source="registry")
    await registry.ensure_registered(cfg, ch=client)
    opened = [r["join_policy"] for _, rows in client.inserted for r in rows]
    assert opened == (["open"] if join_policy == "open" else [])
