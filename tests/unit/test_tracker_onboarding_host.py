"""The tracker onboarding host gate: every address a registrant's host resolves
to must be globally reachable, with an IPv4 address carried inside IPv6
unwrapped first, and the probe connects to the address it vetted."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any

import pytest

import server.tracker.app.onboarding as ob
from server.settings import settings

REFUSED = [
    "127.0.0.1",
    "10.1.2.3",
    "100.73.16.50",  # CGNAT / tailnet
    "169.254.169.254",  # link-local metadata
    "198.18.0.1",  # benchmarking 198.18/15
    "198.19.255.254",
    "192.0.0.8",  # IETF protocol assignments 192.0.0/24
    "0.0.0.0",
    "224.0.0.1",
    "239.1.1.1",
    "::1",
    "::",
    "fd00::1",  # ULA
    "fe80::1",  # link-local
    "ff02::1",
    "::ffff:127.0.0.1",  # IPv4-mapped
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "::7f00:1",  # IPv4-compatible 127.0.0.1
    "::a00:1",  # IPv4-compatible 10.0.0.1
    "64:ff9b::7f00:1",  # NAT64 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 169.254.169.254
]


def _resolving(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> None:
    def _gai(host: str, *a: Any, **kw: Any) -> list[Any]:
        if host not in table:
            raise socket.gaierror("no such host")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, 1, 6, "", (ip, 0))
            for ip in table[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _gai)


@pytest.fixture(autouse=True)
def _public_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tracker_self_service_allow_private_hosts", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", REFUSED)
async def test_refuses_every_non_global_address(monkeypatch, address: str) -> None:
    _resolving(monkeypatch, {"h.example": [address]})
    assert "private or local" in await ob.host_refusal("h.example")


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["3.16.142.177", "2606:4700::1111", "64:ff9b::808:808"])
async def test_accepts_a_public_address(monkeypatch, address: str) -> None:
    _resolving(monkeypatch, {"h.example": [address]})
    assert await ob.host_refusal("h.example") == ""


@pytest.mark.asyncio
async def test_one_private_answer_among_public_ones_refuses(monkeypatch) -> None:
    _resolving(monkeypatch, {"h.example": ["3.16.142.177", "::ffff:127.0.0.1"]})
    assert "::ffff:127.0.0.1" in await ob.host_refusal("h.example")


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_probe_connects_to_the_vetted_address_under_the_hostname(monkeypatch) -> None:
    import clickhouse_connect

    seen: dict[str, Any] = {}

    async def _client(**kw: Any) -> Any:
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _client)
    _resolving(monkeypatch, {"ch.example": ["3.16.142.177", "3.16.142.178"]})
    with pytest.raises(ob.ProbeError):
        await ob.probe_credential(
            host="ch.example", port=8443, secure=True, database="d", user="u", password="p"
        )
    assert seen == {
        "host": "3.16.142.177",
        "server_host_name": "ch.example",
        "port": 8443,
        "username": "u",
        "password": "p",
        "database": "d",
        "secure": True,
        "connect_timeout": 5,
        "send_receive_timeout": 15,  # the probe's default 10 s timeout + 5
        "compress": True,
        "client_name": "loom-tracker-onboarding",
        "settings": {"readonly": 2},
    }


@pytest.mark.asyncio
async def test_probe_refuses_a_name_that_turned_private_after_the_check(monkeypatch) -> None:
    import clickhouse_connect

    async def _client(**kw: Any) -> Any:
        raise AssertionError("must not connect")

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _client)
    _resolving(monkeypatch, {"ch.example": ["3.16.142.177"]})
    assert await ob.host_refusal("ch.example") == ""
    _resolving(monkeypatch, {"ch.example": ["169.254.169.254"]})  # the rebind
    with pytest.raises(ob.HostRefused):
        await ob.probe_credential(
            host="ch.example", port=8443, secure=True, database="d", user="u", password="p"
        )


@pytest.mark.asyncio
async def test_private_hosts_allowed_connect_by_name(monkeypatch) -> None:
    import clickhouse_connect

    seen: dict[str, Any] = {}

    async def _client(**kw: Any) -> Any:
        seen.update(kw)
        raise _Stop

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _client)
    monkeypatch.setattr(settings, "tracker_self_service_allow_private_hosts", True)
    _resolving(monkeypatch, {"ch.local": ["10.0.0.5"]})
    with pytest.raises(ob.ProbeError):
        await ob.probe_credential(
            host="ch.local", port=8123, secure=False, database="d", user="u", password="p"
        )
    assert seen["host"] == "ch.local" and seen["server_host_name"] is None


@pytest.mark.asyncio
async def test_probe_reviews_the_grants_a_role_carries(monkeypatch) -> None:
    import clickhouse_connect

    # what a server answers for a user whose only write access comes from a role
    answers = {
        "SHOW GRANTS": ["GRANT SELECT ON loom_qa.* TO u", "GRANT writer TO u"],
        "SHOW GRANTS FINAL": ["GRANT INSERT ON *.* TO u", "GRANT SELECT ON loom_qa.* TO u"],
    }

    class _Client:
        async def query(self, sql: str, parameters: Any = None) -> Any:
            rows = [(line,) for line in answers.get(sql, [])]
            return SimpleNamespace(column_names=["grants"], result_rows=rows)

        async def close(self) -> None:
            pass

    async def _connect(**_: Any) -> _Client:
        return _Client()

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _connect)

    async def _public(host: str) -> tuple[str, str]:
        return "", "3.16.142.177"

    monkeypatch.setattr(ob, "_vet", _public)
    probe = await ob.probe_credential(
        host="ch.example", port=8443, secure=True, database="loom_qa", user="u", password="p"
    )
    assert probe.grants.roles == ["writer"] and probe.grants.refused == ["INSERT"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "cause"),
    [(RuntimeError("Code: 62. Syntax error"), "Code: 62"), (TimeoutError(), "TimeoutError")],
)
async def test_probe_names_the_grant_statement_the_server_refused(
    monkeypatch, error: Exception, cause: str
) -> None:
    import clickhouse_connect

    class _Client:
        async def query(self, sql: str, parameters: Any = None) -> Any:
            if sql == "SHOW GRANTS FINAL":
                raise error
            return SimpleNamespace(column_names=["grants"], result_rows=[])

        async def close(self) -> None:
            pass

    async def _connect(**_: Any) -> _Client:
        return _Client()

    async def _public(host: str) -> tuple[str, str]:
        return "", "3.16.142.177"

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _connect)
    monkeypatch.setattr(ob, "_vet", _public)
    with pytest.raises(ob.ProbeError, match=f"SHOW GRANTS FINAL refused for 'u': {cause}"):
        await ob.probe_credential(
            host="ch.example", port=8443, secure=True, database="d", user="u", password="p"
        )
