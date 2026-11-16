"""The first cell's state as the router's directory answers it (server/core/cell_router.py):
the four answers the tenancy check reads, sticky once `serving`, the background
reader of a server process and the on-demand read of one without it, and the heartbeat's
`router` and `router_why` keys (docs/architecture/cells.md, "The heartbeat's keys")."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from server.auth import tenancy
from server.auth.jwt import AuthInfo
from server.core import cell, cell_router, errors
from server.operations.base import Role
from server.settings import settings
from tests.unit.clock_fake import ManualClock

_TOKEN = "lcr_" + "0" * 128


@pytest.fixture(autouse=True)
def _first_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first cell's process with its router configured, nothing read yet."""
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "cell_router_url", "https://router.test/")
    monkeypatch.setattr(settings, "cell_router_token", _TOKEN)
    # The module's state is fresh: tests/unit/conftest.py resets it around every test.
    monkeypatch.setattr(cell, "_view", cell._View(read_at=time.monotonic(), first=(True, "eu-1")))


def _router(monkeypatch: pytest.MonkeyPatch, *answers: Any) -> list[str]:
    """The router answering each call with the next of `answers` (the last repeats): a
    (status, body) pair, or an exception raised; the calls it got."""
    calls: list[str] = []
    queue = list(answers)

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path} {request.headers.get('authorization')}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_handler), **kw)
    )
    return calls


def _clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(cell_router.time, "monotonic", clock.monotonic)
    return clock


ATTACHING = (200, {"cell": "eu-1", "state": "attaching"})
SERVING = (200, {"cell": "eu-1", "state": "serving"})


@pytest.mark.parametrize(
    "answer,expected,router",
    [
        (ATTACHING, "attaching", True),
        (SERVING, "serving", True),
        ((200, {"cell": "eu-1", "state": "closing"}), "other", True),
        ((404, {"error": "not the first cell"}), "unknown", False),
        ((401, {"error": "no cell"}), "unknown", None),
        ((200, ["attaching"]), "unknown", None),
        ((200, {"cell": "eu-2", "state": "attaching"}), "unknown", False),
        ((503, {"error": "directory down"}), "unknown", None),
        (httpx.ConnectError("down"), "unknown", None),
    ],
)
def test_the_answer_and_the_router_key_from_one_read(
    monkeypatch: pytest.MonkeyPatch, answer: Any, expected: str, router: bool | None
) -> None:
    calls = _router(monkeypatch, answer)
    assert asyncio.run(cell_router.directory_answer()) == expected
    assert calls == [f"GET /cells/v1/cells/eu-1 Bearer {_TOKEN}"]
    assert cell_router.router_key()["router"] is router
    if router is not True:
        assert cell_router.router_key()["router_why"]


def test_an_on_demand_read_is_read_once_per_window_and_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process with no background reader (a CLI, the workers child) asks at most once a
    LOOM_TENANCY_MODE_CACHE_S, a burst of new orgs sharing one call."""
    clock = _clock(monkeypatch)
    calls = _router(monkeypatch, ATTACHING)

    async def _burst() -> list[str]:
        return list(await asyncio.gather(*(cell_router.directory_answer() for _ in range(5))))

    assert asyncio.run(_burst()) == ["attaching"] * 5
    assert asyncio.run(cell_router.directory_answer()) == "attaching" and len(calls) == 1
    clock.advance(settings.tenancy_mode_cache_s)
    assert asyncio.run(cell_router.directory_answer()) == "attaching" and len(calls) == 2


def test_an_unknown_answer_stands_for_the_window_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A router outage costs one call per window, not one per new-org request."""
    calls = _router(monkeypatch, httpx.ConnectError("down"))
    assert [asyncio.run(cell_router.directory_answer()) for _ in range(3)] == ["unknown"] * 3
    assert len(calls) == 1


def test_a_cancelled_waiter_leaves_the_shared_read_to_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _answer(_request: httpx.Request) -> httpx.Response:
        calls.append("read")
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"cell": "eu-1", "state": "attaching"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_answer), **kw)
    )

    async def _burst() -> list[Any]:
        waiters = [asyncio.create_task(cell_router.directory_answer()) for _ in range(3)]
        await asyncio.sleep(0.01)
        waiters[0].cancel()
        return await asyncio.gather(*waiters, return_exceptions=True)

    results = asyncio.run(_burst())
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1:] == ["attaching", "attaching"] and calls == ["read"]


@pytest.mark.parametrize("missing", ["LOOM_CELL_ROUTER_URL", "LOOM_CELL_ROUTER_TOKEN"])
def test_a_router_setting_unset_asks_nothing_and_reports_false(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    field = {
        "LOOM_CELL_ROUTER_URL": "cell_router_url",
        "LOOM_CELL_ROUTER_TOKEN": "cell_router_token",
    }
    monkeypatch.setattr(settings, field[missing], "")
    calls = _router(monkeypatch, ATTACHING)
    assert asyncio.run(cell_router.directory_answer()) == "unknown" and calls == []
    assert cell_router.router_key() == {"router": False, "router_why": f"{missing} unset"}


@pytest.mark.parametrize("url", ["https://router.test:notaport", "router.test"])
def test_a_url_that_cannot_be_requested_reports_false(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """No transport is reached: httpx refuses the URL itself, before any connection."""
    monkeypatch.setattr(settings, "cell_router_url", url)
    assert asyncio.run(cell_router.directory_answer()) == "unknown"
    key = cell_router.router_key()
    assert key["router"] is False and "cannot be requested" in str(key["router_why"])


def test_a_read_slower_than_the_timeout_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell_router_timeout_s", 0.2)

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"cell": "eu-1", "state": "attaching"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_slow), **kw)
    )
    started = time.monotonic()
    assert asyncio.run(cell_router.directory_answer()) == "unknown"
    assert time.monotonic() - started < 2
    assert cell_router.router_key() == {"router": None, "router_why": "no answer within 0.2 s"}


def test_serving_is_kept_for_the_process_life(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _clock(monkeypatch)
    calls = _router(monkeypatch, SERVING, httpx.ConnectError("down"))
    assert asyncio.run(cell_router.directory_answer()) == "serving"
    clock.advance(10 * cell_router.late_after_s())
    assert asyncio.run(cell_router.directory_answer()) == "serving"
    assert len(calls) == 1


def test_a_late_background_answer_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The background reader's answer counts until one period plus the read's timeout,
    past which a reader that stopped answering reads as unknown."""
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cell_router, "_background", True)
    monkeypatch.setattr(cell_router, "_answer", ("attaching", clock.t))
    clock.advance(cell_router.late_after_s())
    assert asyncio.run(cell_router.directory_answer()) == "attaching"
    clock.advance(0.01)
    assert asyncio.run(cell_router.directory_answer()) == "unknown"


def test_an_on_demand_answer_is_never_late_within_its_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the background reader an answer is read again only once it is
    LOOM_TENANCY_MODE_CACHE_S old, so it counts for that window, not for the shorter
    background period: at a mode cache longer than the beat, a fresh answer stays
    `attaching` until the next read."""
    monkeypatch.setattr(settings, "tenancy_mode_cache_s", 30.0)
    monkeypatch.setattr(settings, "instance_heartbeat_seconds", 10.0)
    monkeypatch.setattr(settings, "cell_router_timeout_s", 5.0)
    clock = _clock(monkeypatch)
    calls = _router(monkeypatch, ATTACHING)
    assert asyncio.run(cell_router.directory_answer()) == "attaching"
    for _ in range(29):
        clock.advance(1)
        assert asyncio.run(cell_router.directory_answer()) == "attaching"
    assert len(calls) == 1
    clock.advance(1)
    assert asyncio.run(cell_router.directory_answer()) == "attaching" and len(calls) == 2


def test_a_new_org_records_at_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the default settings the reader asks every period and a read takes at most the
    timeout, so its answer is never late between two reads: a new org on a recording
    first cell records."""
    for name in ("instance_heartbeat_seconds", "tenancy_mode_cache_s", "cell_router_timeout_s"):
        monkeypatch.setattr(settings, name, type(settings).model_fields[name].default)
    clock = _clock(monkeypatch)
    monkeypatch.setattr(cell_router, "_background", True)
    # The worst case: the last answer one period plus one whole timeout old when the
    # next one lands.
    monkeypatch.setattr(cell_router, "_answer", ("attaching", clock.t))
    clock.advance(cell_router.period_s() + float(settings.cell_router_timeout_s))
    held: set[str] = set()

    async def _fetchval(sql: str, org: str) -> Any:
        if sql == tenancy._RECORD:
            held.add(org)
            return 1
        return 1 if org in held else None

    from server.core import pg

    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(cell, "mode", lambda: "recording")
    monkeypatch.setattr(tenancy, "_held", set())
    asyncio.run(tenancy.ensure_held("new", None))
    assert held == {"new"}


def test_a_cli_with_the_router_token_records_in_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that runs no heartbeat reads on demand, so a CLI holding the router
    credential on a recording first cell records a new org."""
    from server.core import pg

    calls = _router(monkeypatch, ATTACHING)
    statements: list[str] = []

    async def _fetchval(sql: str, org: str) -> Any:
        statements.append(sql)
        return 1 if sql == tenancy._RECORD else None

    monkeypatch.setattr(pg, "fetchval", _fetchval)
    monkeypatch.setattr(cell, "mode", lambda: "recording")
    monkeypatch.setattr(tenancy, "_held", set())
    asyncio.run(tenancy.ensure_held("acme", None))
    assert statements == [tenancy._HOLDS, tenancy._RECORD] and len(calls) == 1


def test_a_401_turns_the_key_false_only_once_it_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """A router replica's cached credential hashes answer 401 for a token rotated a moment
    ago: the last answer stands until 401s outlast LOOM_ROUTER_DIRECTORY_CACHE_S."""
    clock = _clock(monkeypatch)
    _router(monkeypatch, ATTACHING, (401, {"error": "no cell"}))
    asyncio.run(cell_router.read_once())
    assert cell_router.router_key() == {"router": True, "router_why": ""}
    clock.advance(1)
    asyncio.run(cell_router.read_once())
    assert cell_router.router_key() == {"router": True, "router_why": "401, possibly a rotation"}
    assert asyncio.run(cell_router.directory_answer()) == "unknown"
    clock.advance(settings.router_directory_cache_s)  # 401s for exactly one window: kept
    asyncio.run(cell_router.read_once())
    assert cell_router.router_key()["router"] is True
    clock.advance(0.01)
    asyncio.run(cell_router.read_once())
    key = cell_router.router_key()
    assert key["router"] is False and str(key["router_why"]).startswith("401")


def test_503s_between_401s_do_not_restart_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient answer proves nothing about the credential: 401s interleaved with 503s
    still turn the key false once the first 401 is one LOOM_ROUTER_DIRECTORY_CACHE_S old."""
    clock = _clock(monkeypatch)
    unauthorized, down = (401, {"error": "no cell"}), (503, {"error": "down"})
    _router(monkeypatch, ATTACHING, unauthorized, down, unauthorized, down, unauthorized)
    asyncio.run(cell_router.read_once())
    for _ in range(6):  # the last answer repeats: 401
        clock.advance(settings.router_directory_cache_s / 4)
        asyncio.run(cell_router.read_once())
    key = cell_router.router_key()
    assert key["router"] is False and str(key["router_why"]).startswith("401")


def test_a_transient_failure_keeps_the_last_router_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _router(monkeypatch, httpx.ConnectError("down"), ATTACHING, (503, {"error": "down"}))
    asyncio.run(cell_router.read_once())
    # While the key is null, its cause is the last transient one.
    assert cell_router.router_key() == {
        "router": None,
        "router_why": "the router did not answer (ConnectError)",
    }
    asyncio.run(cell_router.read_once())
    asyncio.run(cell_router.read_once())
    assert cell_router.router_key() == {"router": True, "router_why": ""}
    assert asyncio.run(cell_router.directory_answer()) == "unknown"


def test_the_key_is_present_only_on_a_first_cell_with_loom_cell_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cell, "_view", cell._View())
    assert cell_router.router_key()["router"] is None  # the first row is not read yet
    monkeypatch.setattr(cell, "_view", cell._View(read_at=time.monotonic(), first=(False, "eu-1")))
    assert cell_router.router_key() == {}
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(cell, "_view", cell._View(read_at=time.monotonic(), first=(True, "eu-1")))
    assert cell_router.router_key() == {}


def test_router_why_tells_an_unread_first_row_from_an_absent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cell, "_view", cell._View())
    assert cell_router.router_key() == {
        "router": None,
        "router_why": "the cell's first row is not read yet",
    }
    monkeypatch.setattr(cell, "_view", cell._View(read_at=time.monotonic()))
    assert cell_router.router_key() == {
        "router": None,
        "router_why": "this cell's first row is absent: run loom cell prepare",
    }


def test_a_run_of_the_same_failure_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A router down, or answering 401 through a rotation, for many periods logs one
    warning per change of cause, not one per read."""
    _router(
        monkeypatch,
        (503, {"error": "down"}),
        (503, {"error": "down"}),
        (503, {"error": "down"}),
        (401, {"error": "no cell"}),
        (401, {"error": "no cell"}),
        ATTACHING,
        (503, {"error": "down"}),
    )
    with caplog.at_level("WARNING", logger=cell_router.log.name):
        for _ in range(7):
            asyncio.run(cell_router.read_once())
    logged = [r.getMessage() for r in caplog.records if r.name == cell_router.log.name]
    assert logged == [
        "cell state: the router read: the router answered 503",
        "cell state: the router read: 401: the router does not accept LOOM_CELL_ROUTER_TOKEN",
        "cell state: the router read: the router answered 503",
    ]


def test_the_background_reader_asks_until_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its reads share one client, closed when it ends."""
    calls = _router(monkeypatch, ATTACHING, ATTACHING, SERVING)
    clients: list[httpx.AsyncClient] = []
    build = httpx.AsyncClient

    def _client(**kw: Any) -> httpx.AsyncClient:
        clients.append(build(**kw))
        return clients[-1]

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    sleeps: list[float] = []

    async def _sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(cell_router, "_sleep", _sleep)

    async def _run() -> None:
        await cell_router.start_reader()

    asyncio.run(_run())
    assert len(calls) == 3 and sleeps == [cell_router.period_s()] * 3
    assert cell_router._serving and cell_router.router_key()["router"] is True
    assert len(clients) == 1 and clients[0].is_closed


def test_the_background_reader_waits_for_the_first_row_and_a_later_cell_never_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _router(monkeypatch, ATTACHING)
    monkeypatch.setattr(cell, "_view", cell._View())
    sleeps: list[float] = []

    async def _sleep(s: float) -> None:
        sleeps.append(s)
        # The first row is read between two periods: a later cell's.
        cell._view = cell._View(read_at=time.monotonic(), first=(False, "eu-1"))

    monkeypatch.setattr(cell_router, "_sleep", _sleep)

    async def _run() -> None:
        await cell_router.start_reader()

    asyncio.run(_run())
    assert calls == [] and len(sleeps) == 1


def test_the_period_is_the_shorter_of_the_beat_and_the_mode_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "instance_heartbeat_seconds", 3.0)
    monkeypatch.setattr(settings, "tenancy_mode_cache_s", 7.0)
    assert cell_router.period_s() == 3.0
    monkeypatch.setattr(settings, "cell_router_timeout_s", 2.0)
    assert cell_router.late_after_s() == 9.0  # on demand: the mode cache's window
    monkeypatch.setattr(cell_router, "_background", True)
    assert cell_router.late_after_s() == 5.0


def _auth(org: str, *roles: Role) -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=org,
        namespaces=("ns",),
        roles=roles or (Role.WRITER,),
        is_service_account=False,
    )


@pytest.fixture
def enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `enforced` cell holding no org, its placement never called."""
    from server.core import pg

    monkeypatch.setattr(pg, "fetchval", AsyncMock(return_value=None))
    monkeypatch.setattr(cell, "mode", lambda: "enforced")
    monkeypatch.setattr(tenancy, "_held", set())
    monkeypatch.setattr(tenancy, "_elsewhere", {})
    monkeypatch.setattr(tenancy, "_place", AsyncMock(side_effect=AssertionError("placed")))


@pytest.mark.parametrize(
    "answer,refusal",
    [
        ("attaching", "attach in progress"),
        ("unknown", "is not held by this cell, and"),
    ],
)
@pytest.mark.parametrize("principal", ["platform", "none"])
def test_an_unscoped_caller_on_an_attaching_first_cell_answers_503(
    enforced: None, monkeypatch: pytest.MonkeyPatch, answer: str, refusal: str, principal: str
) -> None:
    """A platform or `*`-org principal, or a call with no principal, places no org: on a
    first cell still attaching, or whose directory cannot be read before `serving`, the
    org's data may be in this cell, so it answers 503 (retryable), never WRONG_CELL."""
    monkeypatch.setattr(cell_router, "directory_answer", AsyncMock(return_value=answer))
    auth = _auth("*", Role.PLATFORM_ADMIN) if principal == "platform" else None
    with pytest.raises(errors.BackendUnavailableError, match=refusal):
        asyncio.run(tenancy.ensure_held("new", auth))


def test_an_unscoped_refusal_without_router_settings_names_the_org_alone(
    enforced: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the router token unset, as every CLI past `recording` has it, the refusal names
    the org alone: never attach, never a token the CLI is refused with."""
    monkeypatch.setattr(
        cell_router, "directory_answer", AsyncMock(return_value=cell_router.UNKNOWN)
    )
    monkeypatch.setattr(cell_router, "unset", lambda: ["LOOM_CELL_ROUTER_TOKEN"])
    with pytest.raises(errors.BackendUnavailableError) as raised:
        asyncio.run(tenancy.ensure_held("new", None))
    assert str(raised.value) == "org 'new' is not held by this cell"


def test_a_server_names_its_unset_router_settings_in_an_unscoped_refusal(
    enforced: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server process (the background reader) with router settings unset answers every
    unscoped call 503 for its life: the refusal names the unset setting."""
    monkeypatch.setattr(cell_router, "_background", True)
    monkeypatch.setattr(
        cell_router, "directory_answer", AsyncMock(return_value=cell_router.UNKNOWN)
    )
    monkeypatch.setattr(cell_router, "unset", lambda: ["LOOM_CELL_ROUTER_TOKEN"])
    with pytest.raises(errors.BackendUnavailableError) as raised:
        asyncio.run(tenancy.ensure_held("new", None))
    assert str(raised.value) == (
        "org 'new' is not held by this cell, and LOOM_CELL_ROUTER_TOKEN unset"
    )


@pytest.mark.parametrize("answer", ["serving", "other"])
def test_an_unscoped_caller_answers_wrong_cell_once_serving_or_on_a_later_cell(
    enforced: None, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    read = AsyncMock(return_value=answer)
    monkeypatch.setattr(cell_router, "directory_answer", read)
    with pytest.raises(errors.WrongCellError):
        asyncio.run(tenancy.ensure_held("new", None))
    monkeypatch.setattr(cell, "_view", cell._View(read_at=time.monotonic(), first=(False, "eu-1")))
    with pytest.raises(errors.WrongCellError):
        asyncio.run(tenancy.ensure_held("new", None))
    read.assert_awaited_once()  # a later cell never asks
