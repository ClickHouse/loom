"""A self-service tracker source's client connects to an address that passed the
onboarding host gate when the client was built, with the name kept for Host, SNI
and the certificate: a name that turns private after registration is refused,
not followed. An operator-configured source is connected by name."""

from __future__ import annotations

import socket
from typing import Any

import pytest

import server.tracker.domain.sources as qs
import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from tests.unit._hosts import resolves
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

HOST = "qa.example"


class _Client:
    def __init__(self) -> None:
        self.queries = 0

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        self.queries += 1

        class _Res:
            column_names = ("x",)
            result_rows = [(1,)]

        return _Res()

    async def close(self) -> None:
        return None


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    qi.reset_for_tests()
    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json(), raising=False)
    monkeypatch.setattr(
        settings, "tracker_credentials_json", tracker_credentials_json(), raising=False
    )
    monkeypatch.setattr(settings, "tracker_self_service_allow_private_hosts", False)
    state: dict[str, Any] = {"origin": "registry", "answer": ["3.16.142.177"], "lookups": 0}
    state["built"] = []
    monkeypatch.setattr(qs, "source_origin", lambda name: state["origin"])

    def _gai(host: str, *a: Any, **kw: Any) -> list[Any]:
        assert host == HOST
        state["lookups"] += 1
        return [(socket.AF_INET, 1, 6, "", (ip, 0)) for ip in state["answer"]]

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    resolves(monkeypatch, lambda host: [info[4][0] for info in _gai(host)])

    async def _client(**kw: Any) -> Any:
        state["built"].append(kw)
        return _Client()

    import clickhouse_connect

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _client)
    yield state
    qi.reset_for_tests()


@pytest.mark.asyncio
async def test_a_registered_source_connects_to_the_vetted_address(wired) -> None:
    await qi.query_rows("SELECT 1")
    await qi.query_rows("SELECT 1")
    (kw,) = wired["built"]
    assert kw["host"] == "3.16.142.177" and kw["server_host_name"] == HOST
    assert wired["lookups"] == 1  # once per client built, never per query


@pytest.mark.asyncio
async def test_the_live_client_is_readonly_2_compressed_and_bounded(wired, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_live_timeout_s", 1.5)
    await qi.query_rows("SELECT 1")
    (kw,) = wired["built"]
    ep = qs.endpoint(qi.CREDENTIAL)
    assert ep is not None
    user, password = qi._user_password(qi.CREDENTIAL)
    assert kw == {
        "host": "3.16.142.177",
        "server_host_name": HOST,
        "port": int(ep.port),
        "username": user,
        "password": password,
        "database": ep.database,
        "secure": bool(ep.secure),
        "connect_timeout": 5,
        "send_receive_timeout": qi.socket_timeout_s(1.5),
        "compress": True,
        "client_name": f"loom-tracker-live-{qi.CREDENTIAL}",
        "settings": {"readonly": 2, "max_execution_time": 1.5},
    }


@pytest.mark.asyncio
async def test_a_rebind_to_a_private_address_after_registration_is_refused(wired) -> None:
    from server.tracker.app import onboarding

    assert await onboarding.host_refusal(HOST) == ""  # public when it was registered
    wired["answer"] = ["169.254.169.254"]  # the rebind
    with pytest.raises(qi.QaIntelUnavailable, match="host refused"):
        await qi.query_rows("SELECT 1")
    assert wired["built"] == []
    assert qi.circuit_status()["state"] == "CLOSED"  # a refusal, not an outage


@pytest.mark.asyncio
async def test_a_rebuilt_client_re_resolves(wired) -> None:
    await qi.query_rows("SELECT 1")
    wired["answer"] = ["::ffff:10.0.0.1"]
    await qi.query_rows("SELECT 1")  # the pooled client keeps its vetted address
    assert len(wired["built"]) == 1
    await qi.forget(qi.CREDENTIAL)  # rotation rebuilds the client
    with pytest.raises(qi.QaIntelUnavailable, match="host refused"):
        await qi.query_rows("SELECT 1")
    assert wired["lookups"] == 2


@pytest.mark.asyncio
async def test_an_operator_configured_source_connects_by_name(wired) -> None:
    wired["origin"] = "config"
    wired["answer"] = ["10.0.0.5"]
    await qi.query_rows("SELECT 1")
    (kw,) = wired["built"]
    assert kw["host"] == HOST and kw["server_host_name"] is None
    assert wired["lookups"] == 0


@pytest.mark.asyncio
async def test_concurrent_first_reads_share_one_client(wired, monkeypatch) -> None:
    """A fan-out that lands on a cold slot builds one client, not one per read
    (every extra one was dropped unclosed)."""
    import asyncio

    import clickhouse_connect

    async def _slow_client(**kw: Any) -> Any:
        wired["built"].append(kw)
        await asyncio.sleep(0.01)
        return _Client()

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _slow_client)
    await asyncio.gather(*(qi.query_rows("SELECT 1") for _ in range(8)))
    assert len(wired["built"]) == 1
