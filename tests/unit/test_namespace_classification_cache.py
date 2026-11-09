"""The classification lookup is an authorization input: one
statement per org, a short TTL, the last good snapshot on error, and a raise
— never an empty set — when there is nothing to serve."""

from __future__ import annotations

import pytest

import server.identity.app.namespace_classification as nc
from server.auth.directory import ClassificationUnavailable


class _Client:
    def __init__(self, rows=None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls = 0

    async def query_all(self, sql, params=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ch down")
        assert "classification = 'restricted'" in sql and "FINAL" in sql
        assert params == {"org": "clickhouse"}
        return list(self.rows)


@pytest.fixture(autouse=True)
def _fresh():
    nc.reset()
    yield
    nc.reset()


@pytest.mark.asyncio
async def test_one_statement_per_org_within_ttl(monkeypatch) -> None:
    import server.core.ch as ch

    client = _Client(rows=[{"namespace": "code-example-private"}, {"namespace": ""}])
    monkeypatch.setattr(ch, "get_client", lambda: client)
    assert await nc.restricted_set("clickhouse") == frozenset({"code-example-private"})
    assert await nc.restricted_set("clickhouse") == frozenset({"code-example-private"})
    assert client.calls == 1


@pytest.mark.asyncio
async def test_error_serves_last_good_snapshot(monkeypatch) -> None:
    import server.core.ch as ch

    client = _Client(rows=[{"namespace": "tracker-ops-restricted"}])
    monkeypatch.setattr(ch, "get_client", lambda: client)
    assert await nc.restricted_set("clickhouse") == frozenset({"tracker-ops-restricted"})
    client.fail = True
    nc._cache["clickhouse"] = (0.0, nc._cache["clickhouse"][1])  # expire
    assert await nc.restricted_set("clickhouse") == frozenset({"tracker-ops-restricted"})


@pytest.mark.asyncio
async def test_error_without_snapshot_raises(monkeypatch) -> None:
    import server.core.ch as ch

    monkeypatch.setattr(ch, "get_client", lambda: _Client(fail=True))
    with pytest.raises(ClassificationUnavailable):
        await nc.restricted_set("clickhouse")


@pytest.mark.asyncio
async def test_is_restricted_consults_config_first(monkeypatch) -> None:
    import server.code.domain.index.repos as repos
    import server.core.ch as ch

    class _Cfg:
        classification = "restricted"

    monkeypatch.setattr(repos, "config_entry", lambda org, ns: _Cfg() if ns == "priv" else None)
    client = _Client(rows=[])
    monkeypatch.setattr(ch, "get_client", lambda: client)
    assert await nc.is_restricted("clickhouse", "priv") is True
    assert client.calls == 0  # config decided without I/O
    assert await nc.is_restricted("clickhouse", "other") is False
    assert client.calls == 1


@pytest.mark.asyncio
async def test_note_restricted_updates_a_live_snapshot(monkeypatch) -> None:
    import server.core.ch as ch

    client = _Client(rows=[])
    monkeypatch.setattr(ch, "get_client", lambda: client)
    assert await nc.restricted_set("clickhouse") == frozenset()
    nc.note_restricted("clickhouse", "new-ns")
    assert "new-ns" in await nc.restricted_set("clickhouse")
    nc.note_restricted("acme", "x")  # no snapshot: nothing to update, no error
