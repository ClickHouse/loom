"""The code namespace registry writes the classification on a
fresh row and narrows an existing standard row when the config says
restricted — mirroring every column — and never widens one."""

from __future__ import annotations

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
