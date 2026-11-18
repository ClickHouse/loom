"""Inert HTTP responses exercise the one-shot native observation authority."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from server.core import ch as ch_module
from server.core import ch_observer
from server.core.ch_observer import ObservationUnavailable, PolicyObserver, observer_origins

_UUID = "00000000-0000-0000-0000-000000000001"


def _body(rows, columns=None):
    names = columns or list(rows[0])
    return json.dumps(
        {
            "meta": [{"name": name, "type": "String"} for name in names],
            "data": rows,
            "rows": len(rows),
        }
    ).encode()


def _identity(**overrides):
    return {"server_uuid": _UUID, "observer_user": "service", "observer_roles": [], **overrides}


class _HTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
        self.readonly_settings = {}

    @asynccontextmanager
    async def post(self, origin, **options):
        self.calls.append((origin, options))
        if any(
            name in options["params"] and options["params"][name] != value
            for name, value in self.readonly_settings.items()
        ):
            response = (500, b"Cannot modify a readonly setting")
        else:
            response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        status, payload = response

        class Content:
            async def iter_chunked(self, size):
                for index in range(0, len(payload), size):
                    yield payload[index : index + size]

        class Response:
            content = Content()

        result = Response()
        result.status = status
        yield result

    async def close(self):
        self.closed = True


@pytest.fixture
def observer(monkeypatch):
    def create(responses):
        http = _HTTP(responses)
        monkeypatch.setattr(ch_observer.aiohttp, "ClientSession", lambda **options: http)

        async def guarded(sql, send):
            return await send()

        return PolicyObserver(
            "http://writer:8123/", "service", "password", "loom", guarded, lambda sql: {}
        ), http

    return create


async def test_bootstrap_and_policy_reads_preserve_readonly_query_logging(observer):
    instance, http = observer(
        [
            (200, _body([_identity()])),
            (200, _body([], ["statement"])),
            (200, _body([{"closed": 1}])),
        ]
    )
    http.readonly_settings = {"log_query_settings": "1"}
    try:
        await instance.bootstrap()
        assert await instance.policies() == []
    finally:
        await instance.close()
    assert http.closed and len(http.calls) == 3


async def test_successful_bootstrap_and_policy_reads_never_recreate_or_redirect(observer):
    instance, http = observer(
        [
            (200, _body([_identity()])),
            (200, _body([], ["statement"])),
            (200, _body([{"closed": 1}])),
        ]
    )
    try:
        await instance.bootstrap()
        assert await instance.policies() == []
    finally:
        await instance.close()
    assert http.closed
    assert [call[1]["params"]["session_check"] for call in http.calls] == ["0", "1", "1"]
    assert len({call[1]["params"]["session_id"] for call in http.calls}) == 1
    assert http.calls[-1][1]["params"]["close_session"] == "1"
    assert all(call[1]["allow_redirects"] is False for call in http.calls)
    assert all("log_query_settings" not in call[1]["params"] for call in http.calls)
    assert all("log_query_parameters" not in call[1]["params"] for call in http.calls)


@pytest.mark.parametrize(
    "failure",
    [
        OSError("secret transport detail"),
        (302, b"redirect"),
        (200, b'{"meta": [], "data": ['),
        (200, b'{"meta": [], "data": [], "rows":0,"rows":1}'),
        (200, _body([_identity(observer_user="analyst")])),
    ],
)
async def test_ambiguous_bootstrap_is_abandoned_without_a_second_unchecked_post(observer, failure):
    instance, http = observer([failure, (404, b"session missing")])
    with pytest.raises(ObservationUnavailable) as error:
        await instance.bootstrap()
    assert "secret" not in str(error.value)
    with pytest.raises(ObservationUnavailable):
        await instance.bootstrap()
    assert len(http.calls) == 1
    with pytest.raises(ObservationUnavailable):
        await instance.close()
    assert http.closed and http.calls[-1][1]["params"]["session_check"] == "1"


@pytest.mark.parametrize(
    "row",
    [
        _identity(server_uuid="00000000-0000-0000-0000-000000000002"),
        _identity(observer_user="other"),
        _identity(observer_roles=["loom_analyst"]),
        _identity(observer_roles=""),
    ],
)
async def test_each_decision_envelope_attests_backend_principal_and_roles(observer, row):
    instance, http = observer([(200, _body([_identity()])), (200, _body([row])), (404, b"")])
    try:
        await instance.bootstrap()
        with pytest.raises(ObservationUnavailable):
            await instance.query_all("SELECT toString(serverUUID()) AS server_uuid")
        with pytest.raises(ObservationUnavailable):
            await instance.policies()
        assert len(http.calls) == 2
    finally:
        with pytest.raises(ObservationUnavailable):
            await instance.close()
    assert http.closed


async def test_oversized_and_late_exception_policy_output_cannot_be_partial_evidence(
    observer, monkeypatch
):
    monkeypatch.setattr(ch_observer, "_BODY_BYTES", 1024)
    instance, http = observer(
        [
            (200, _body([_identity()])),
            (200, _body([{"statement": "x" * 2048}])),
            (404, b""),
        ]
    )
    try:
        await instance.bootstrap()
        with pytest.raises(ObservationUnavailable):
            await instance.policies()
    finally:
        with pytest.raises(ObservationUnavailable):
            await instance.close()
    assert http.closed


async def test_bootstrap_cancellation_does_not_replay_and_local_cleanup_is_unconditional(observer):
    instance, http = observer([asyncio.CancelledError(), (404, b"")])
    with pytest.raises(asyncio.CancelledError):
        await instance.bootstrap()
    with pytest.raises(ObservationUnavailable):
        await instance.close()
    assert http.closed and len(http.calls) == 2
    assert [call[1]["params"]["session_check"] for call in http.calls] == ["0", "1"]


@pytest.mark.parametrize(
    "additional",
    [
        ("http://writer:8123",),
        ("http://user:password@other:8123",),
        ("https://other:8443/path",),
        ("https://other:8443/?password=value",),
        ("https://other:8443/#fragment",),
        ("https://other:0",),
        ("https://other:99999",),
        ("https://other\n:8443",),
    ],
)
def test_only_explicit_distinct_origins_can_receive_existing_credentials(additional):
    config = {"host": "writer", "port": 8123, "secure": False}
    with pytest.raises(ObservationUnavailable):
        observer_origins(config, additional)


def test_secure_service_cannot_downgrade_observer_transport():
    with pytest.raises(ObservationUnavailable):
        observer_origins({"host": "writer", "port": 8443, "secure": True}, ("http://other:8123",))


@pytest.fixture
def owner(monkeypatch):
    configuration = {
        "host": "writer",
        "port": 8123,
        "secure": False,
        "username": "service",
        "password": "password",
        "database": "loom",
    }
    monkeypatch.setattr(ch_module, "connection_kwargs", lambda: dict(configuration))
    monkeypatch.setattr(ch_module.settings, "ch_analysis_observer_urls", ["http://other:8123"])
    client = ch_module.CHClient()
    client._client = object()
    client._connection_config = dict(configuration)
    instances = []

    class Channel:
        bootstrap_failure = None
        close_failure = None

        def __init__(self, origin, user, password, database, guarded, options):
            self.guarded = guarded
            self.closed = False
            instances.append(self)

        async def bootstrap(self):
            if self.bootstrap_failure:
                raise self.bootstrap_failure

        async def close(self):
            self.closed = True
            if self.close_failure:
                raise self.close_failure

    monkeypatch.setattr(ch_module, "PolicyObserver", Channel)
    return client, configuration, instances, Channel


async def test_core_preserves_bootstrap_cancellation_across_missing_session_cleanup(owner):
    client, config, channels, channel = owner
    channel.bootstrap_failure = asyncio.CancelledError()
    channel.close_failure = ObservationUnavailable()
    with pytest.raises(asyncio.CancelledError):
        async with client.policy_observers():
            pytest.fail("Cancelled bootstrap cannot authorize an observer")
    assert len(channels) == 1 and channels[0].closed


async def test_core_cleanup_failure_cannot_authorize_a_successful_proof(owner):
    client, config, channels, channel = owner
    channel.close_failure = ObservationUnavailable()
    with pytest.raises(ObservationUnavailable):
        async with client.policy_observers():
            pass
    assert len(channels) == 2 and all(item.closed for item in channels)


async def test_core_closes_all_owned_routes_concurrently(owner, monkeypatch):
    client, config, channels, channel = owner
    arrived = set()
    release = asyncio.Event()

    async def close(self):
        arrived.add(id(self))
        if len(arrived) == len(channels):
            release.set()
        await release.wait()
        self.closed = True

    monkeypatch.setattr(channel, "close", close)
    async with asyncio.timeout(5):
        async with client.policy_observers():
            pass
    assert len(channels) == 2 and all(item.closed for item in channels)


@pytest.mark.parametrize("drift", ["driver", "generation", "credential", "route"])
async def test_core_refuses_changed_owner_configuration_before_any_send(owner, monkeypatch, drift):
    client, config, channels, channel = owner
    sent = []

    async def send():
        sent.append(True)

    async with client.policy_observers() as observers:
        if drift == "driver":
            client._client = object()
        elif drift == "generation":
            client._generation += 1
        elif drift == "credential":
            config["password"] = "replacement"
        else:
            monkeypatch.setattr(ch_module.settings, "ch_analysis_observer_urls", [])
        with pytest.raises(ObservationUnavailable):
            await observers[0].guarded("SELECT 1", send)
    assert not sent and all(item.closed for item in channels)


async def test_an_analyst_override_cannot_bootstrap_an_administrative_observer(owner):
    client, config, channels, channel = owner
    with ch_module.as_analyst("loom_analyst", "password", {}):
        with pytest.raises(ObservationUnavailable):
            async with client.policy_observers():
                pytest.fail("Analyst context cannot acquire observation authority")
    assert not channels
