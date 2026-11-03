"""The tracker clients: read-only, bounded, one
breaker per credential that opens after consecutive failures and probes
after cooldown, and one credential for every namespace."""

from __future__ import annotations

import pytest

import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from server.tracker.domain import sources
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    qi.reset_for_tests()
    monkeypatch.setattr(settings, "tracker_circuit_failure_threshold", 2)
    monkeypatch.setattr(settings, "tracker_circuit_cooldown_s", 1.0)
    yield
    qi.reset_for_tests()


def test_breaker_opens_after_threshold_and_half_opens_after_cooldown(monkeypatch) -> None:
    assert qi.circuit_allow()
    qi.circuit_record(False)
    assert qi.circuit_status()["state"] == "CLOSED"
    qi.circuit_record(False)
    assert qi.circuit_status()["state"] == "OPEN" and not qi.circuit_allow()
    import time

    monkeypatch.setattr(time, "monotonic", lambda: qi._slot(qi.CREDENTIAL).breaker.opened_at + 5.0)
    assert qi.circuit_allow() and qi.circuit_status()["state"] == "HALF_OPEN"
    qi.circuit_record(True)
    assert qi.circuit_status()["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_query_rows_records_outcome_and_raises_when_open(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json())

    class _Res:
        column_names = ["n"]
        result_rows = [(3,)]

    class _Client:
        def __init__(self) -> None:
            self.fail = False

        async def query(self, sql, parameters=None, settings=None):
            self.last_settings = settings
            if self.fail:
                raise RuntimeError("boom")
            return _Res()

    client = _Client()

    async def _get(credential=qi.CREDENTIAL):
        return client

    monkeypatch.setattr(qi, "_get_client", _get)
    assert await qi.query_rows("SELECT 3 AS n") == [{"n": 3}]
    assert client.last_settings == {}  # an interactive read keeps the session ceiling
    # a read with its own budget carries a matching server-side ceiling
    await qi.query_rows("SELECT 3 AS n", max_execution_s=6.0)
    assert client.last_settings == {"max_execution_time": 6.0}
    client.fail = True
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await qi.query_rows("SELECT 1")
    with pytest.raises(qi.QaIntelUnavailable):
        await qi.query_rows("SELECT 1")  # breaker open: no call reaches the client


@pytest.mark.asyncio
async def test_unconfigured_raises_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_sources_json", "")
    assert not qi.configured()
    with pytest.raises(qi.QaIntelUnavailable):
        await qi.query_rows("SELECT 1")
    assert (await qi.probe())["configured"] is False


@pytest.mark.asyncio
async def test_client_is_read_only_and_bounded(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json())
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_timeout_s", 1.5)
    captured = {}

    async def _fake_get_async_client(**kw):
        captured.update(kw)
        return object()

    import clickhouse_connect

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _fake_get_async_client)
    await qi._get_client(qi.CREDENTIAL)
    # readonly=2: the remoteSecure-backed views refuse readonly=1
    assert captured["settings"] == {"readonly": 2, "max_execution_time": 1.5}
    # the socket outlives the widest read budget (the 6 s bisect scan), so a
    # slow statement ends as the server's TIMEOUT_EXCEEDED
    # (the daily roll-up statements carry a 60 s budget, so 65 s)
    assert captured["send_receive_timeout"] == 65 and captured["secure"] is True
    assert captured["database"] == "qa_intelligence"
    assert captured["username"] == "ro" and captured["password"] == "pw"
    assert captured["client_name"] == "loom-tracker-live-qa"


@pytest.mark.asyncio
async def test_one_credential_serves_every_namespace(monkeypatch) -> None:
    """There is no private credential; an unset user makes the
    plane unconfigured, and the one breaker is the plane's breaker."""
    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json())
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    assert sources.credential_configured(qi.CREDENTIAL) and qi.CREDENTIALS == (qi.CREDENTIAL,)
    monkeypatch.setattr(settings, "tracker_credentials_json", "")
    assert not sources.credential_configured(qi.CREDENTIAL)
    with pytest.raises(qi.QaIntelUnavailable):
        await qi.query_rows("SELECT 1")
    qi.circuit_record(False)
    qi.circuit_record(False)
    assert qi.circuit_status()["state"] == "OPEN"


def test_host_accepts_a_url_or_a_bare_hostname() -> None:
    """The deployment wrote the host as a URL; both forms name the service."""
    assert sources.host_of("https://x.us-east-2.aws.example.com/") == "x.us-east-2.aws.example.com"
    assert (
        sources.host_of("http://x.example.com:8443/?a=1") == "x.example.com"
    )  # the port is its own setting
    assert sources.host_of("x.example.com") == "x.example.com" and sources.host_of("  ") == ""


def test_host_drops_userinfo_from_a_url() -> None:
    """A credential typed into the host never becomes part of the stored host."""
    assert sources.host_of("https://user:pw@x.example.com:8443/") == "x.example.com"
    assert sources.host_of("tok@x.example.com") == "x.example.com"
    assert sources.host_of("x.example.com#@y.example.com") == "x.example.com"


@pytest.mark.asyncio
async def test_an_upstream_quota_refusal_is_unavailable_but_leaves_the_breaker_closed(
    monkeypatch,
) -> None:
    """The build-history views proxy a playground whose quota
    rations queries per statement shape; a refusal is not an outage."""
    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json())
    monkeypatch.setattr(settings, "tracker_circuit_failure_threshold", 1)
    qi.reset_for_tests()

    class _Client:
        async def query(self, sql, parameters=None, settings=None):
            raise RuntimeError(
                "Code: 201. DB::Exception: Quota for user `play` for 3600s has been exceeded:"
                " queries_per_normalized_hash = 101/100 (QUOTA_EXCEEDED)"
            )

    async def _fake_get_client(credential):
        return _Client()

    monkeypatch.setattr(qi, "_get_client", _fake_get_client)
    with pytest.raises(qi.QaIntelUnavailable, match="quota"):
        await qi.query_rows("SELECT 1")
    assert qi.circuit_allow(qi.CREDENTIAL)  # one real failure would have opened it
    assert qi.is_quota_error(RuntimeError("Code: 201. x")) and not qi.is_quota_error(
        RuntimeError("Code: 60.")
    )


def test_socket_timeout_outlives_every_read_budget(monkeypatch) -> None:
    """A read timeout equal to the bisect scan's own 6 s budget would end
    the scan as a socket error, so the socket waits past the largest budget a
    reader may carry."""
    monkeypatch.setattr(settings, "tracker_live_budget_ms", 300)
    monkeypatch.setattr(settings, "tracker_live_text_budget_ms", 900)
    monkeypatch.setattr(settings, "tracker_live_bisect_budget_ms", 6000)
    # the checks history and daily roll-up budgets are in the set too
    monkeypatch.setattr(settings, "tracker_live_checks_budget_ms", 300)
    monkeypatch.setattr(settings, "tracker_test_daily_budget_ms", 300)
    assert qi.largest_budget_s() == 6.0
    assert qi.socket_timeout_s(1.5) == 11
    monkeypatch.setattr(settings, "tracker_live_bisect_budget_ms", 20000)
    assert qi.socket_timeout_s(1.5) == 25
    # a session ceiling wider than every budget still wins
    assert qi.socket_timeout_s(30.0) == 35
    monkeypatch.setattr(settings, "tracker_test_daily_budget_ms", 60000)
    assert qi.socket_timeout_s(1.5) == 65  # the daily roll-up's statements


@pytest.mark.asyncio
async def test_server_side_budget_timeout_does_not_count_on_the_breaker(monkeypatch) -> None:
    """A statement the upstream ended at the `max_execution_time`
    we sent (code 159) is our budget; a transport or upstream error is theirs."""
    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json())

    class _Client:
        error: Exception | None = None

        async def query(self, sql, parameters=None, settings=None):
            raise self.error

    client = _Client()

    async def _get(credential=qi.CREDENTIAL):
        return client

    monkeypatch.setattr(qi, "_get_client", _get)
    client.error = RuntimeError(
        "Received ClickHouse exception, code: 159, server response: Code: 159. "
        "DB::Exception: Timeout exceeded: elapsed 6001 ms, maximum: 6000 ms. (TIMEOUT_EXCEEDED)"
    )
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await qi.query_rows("SELECT 1", max_execution_s=6.0)
    assert qi.circuit_status()["consecutive_failures"] == 0 and qi.circuit_allow()
    client.error = RuntimeError("Network Error: connection refused")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await qi.query_rows("SELECT 1")
    assert qi.circuit_status()["state"] == "OPEN"
    assert qi.is_budget_timeout(RuntimeError("x (TIMEOUT_EXCEEDED)")) and not qi.is_budget_timeout(
        RuntimeError("code: 210 NETWORK_ERROR")
    )


@pytest.mark.asyncio
async def test_a_missing_table_does_not_open_the_shared_breaker(monkeypatch) -> None:
    """A read of a table this tracker lacks
    (qa_intelligence.release_health_snapshot, code 60) must not count: three in
    a row would open the breaker every tracker read shares and code.backport_gap
    would answer tracker_unavailable for every caller. A statement-shape error is
    raised to its caller and never counted; a real outage still opens it."""
    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json())

    class _Client:
        error: Exception | None = None

        async def query(self, sql, parameters=None, settings=None):
            raise self.error

    client = _Client()

    async def _get(credential=qi.CREDENTIAL):
        return client

    monkeypatch.setattr(qi, "_get_client", _get)
    shape_errors = [
        "Received ClickHouse exception, code: 60, server response: Code: 60. DB::Exception: "
        "Unknown table expression identifier 'qa_intelligence.release_health_snapshot' in scope "
        "SELECT computed_at FROM qa_intelligence.release_health_snapshot. (UNKNOWN_TABLE)",
        "Code: 47. DB::Exception: Unknown expression identifier `severity_level`. (UNKNOWN_IDENTIFIER)",
        "Code: 46. DB::Exception: Unknown function foo. (UNKNOWN_FUNCTION)",
        "Code: 62. DB::Exception: Syntax error: failed at position 1. (SYNTAX_ERROR)",
    ]
    for text in shape_errors * 2:
        client.error = RuntimeError(text)
        with pytest.raises(RuntimeError):
            await qi.query_rows("SELECT 1")
    assert qi.circuit_status()["consecutive_failures"] == 0 and qi.circuit_allow()
    client.error = RuntimeError("Network Error: connection refused")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await qi.query_rows("SELECT 1")
    assert qi.circuit_status()["state"] == "OPEN"
    assert qi.is_shape_error(RuntimeError(shape_errors[0]))
    assert not qi.is_shape_error(RuntimeError("Code: 210. DB::Exception: NETWORK_ERROR"))
    assert not qi.is_shape_error(RuntimeError("Code: 241. DB::Exception: MEMORY_LIMIT_EXCEEDED"))


def _open_then_cool(monkeypatch) -> None:
    """Open the default breaker, then move the clock past its cooldown."""
    import time

    qi.circuit_record(False)
    qi.circuit_record(False)
    assert qi.circuit_status()["state"] == "OPEN"
    opened = qi._slot(qi.CREDENTIAL).breaker.opened_at
    monkeypatch.setattr(time, "monotonic", lambda: opened + 5.0)


def test_half_open_lets_exactly_one_probe_through(monkeypatch) -> None:
    """HALF_OPEN admits one caller until that probe's outcome is recorded; a
    failed probe re-opens, a successful one closes for everyone."""
    _open_then_cool(monkeypatch)
    assert qi.circuit_allow() and qi.circuit_status()["state"] == "HALF_OPEN"
    assert not qi.circuit_allow() and not qi.circuit_allow()
    qi.circuit_record(False)
    assert qi.circuit_status()["state"] == "OPEN" and not qi.circuit_allow()
    _open_then_cool(monkeypatch)
    assert qi.circuit_allow() and not qi.circuit_allow()
    qi.circuit_record(True)
    assert qi.circuit_status()["state"] == "CLOSED"
    assert all(qi.circuit_allow() for _ in range(3))


@pytest.mark.asyncio
async def test_half_open_fan_out_sends_one_read_upstream(monkeypatch) -> None:
    """A brief fans out twelve reads; after the cooldown only one may reach a
    service that may still be down, and the probe slot is freed whatever
    ends it — an uncounted error, a cancellation, or its recorded outcome."""
    import asyncio

    monkeypatch.setattr(settings, "tracker_sources_json", tracker_sources_json())

    class _Res:
        column_names = ["n"]
        result_rows = [(1,)]

    class _Client:
        def __init__(self) -> None:
            self.calls = 0
            self.gate = asyncio.Event()
            self.error: Exception | None = None

        async def query(self, sql, parameters=None, settings=None):
            self.calls += 1
            await self.gate.wait()
            if self.error is not None:
                raise self.error
            return _Res()

    client = _Client()

    async def _get(credential=qi.CREDENTIAL):
        return client

    monkeypatch.setattr(qi, "_get_client", _get)
    _open_then_cool(monkeypatch)

    # an uncounted (shape) error ends the probe without a verdict: the slot frees
    client.error = RuntimeError("Code: 60. DB::Exception: x. (UNKNOWN_TABLE)")
    client.gate.set()
    with pytest.raises(RuntimeError):
        await qi.query_rows("SELECT 1")
    assert qi.circuit_status()["state"] == "HALF_OPEN"

    # a cancelled probe frees the slot too
    client.gate = asyncio.Event()
    client.error = None
    probe = asyncio.create_task(qi.query_rows("SELECT 1"))
    await asyncio.sleep(0)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    # twelve-way fan-out: one read goes upstream, eleven are refused
    client.calls = 0
    tasks = [asyncio.create_task(qi.query_rows("SELECT 1")) for _ in range(12)]
    await asyncio.sleep(0)
    client.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert client.calls == 1
    assert sum(r == [{"n": 1}] for r in results) == 1
    assert sum(isinstance(r, qi.QaIntelUnavailable) for r in results) == 11
    assert qi.circuit_status()["state"] == "CLOSED"


def test_budget_timeout_matches_the_server_code_in_the_body() -> None:
    """ClickHouse writes `Code: 159.`; an error parsed from the response body
    (a mid-stream failure) carries only that, not the driver's `code: 159,`."""
    assert qi.is_budget_timeout(
        RuntimeError("Code: 159. DB::Exception: Timeout exceeded: elapsed 6001 ms")
    )
    assert qi.is_budget_timeout(RuntimeError("Received ClickHouse exception, code: 159, x"))
    assert not qi.is_budget_timeout(RuntimeError("Code: 1590. DB::Exception: other"))


def test_probe_output_masks_remote_credentials() -> None:
    clause = "remoteSecure('play.example.com', 'default', 'checks', 'play', 's3cr\\'t')"
    masked = qi.redact_table_function(clause)
    assert "s3cr" not in masked
    assert masked.startswith("remoteSecure('play.example.com', 'default', 'checks', ")
    assert masked.count("'***'") == 2
    # the `db.table` form puts the password fourth
    assert "pw" not in qi.redact_table_function("remote('h:9000', 'db.t', 'u', 'pw')")
    # a table or a view has no credential to hide
    assert qi.redact_table_function("qa.checks") == "qa.checks"
    assert qi.redact_table_function("") == ""


def test_printable_endpoint_masks_checks_from() -> None:
    from server.tracker.domain.sources import TrackerEndpoint

    ep = TrackerEndpoint(
        name="x",
        host="h",
        database="d",
        checks_from="remoteSecure('h', 'default', 'checks', 'u', 'secret')",
    )
    out = qi._printable_endpoint(ep)
    assert "secret" not in out["checks_from"] and out["host"] == "h"
    assert ep.checks_from.endswith("'secret')")  # the endpoint itself is untouched
