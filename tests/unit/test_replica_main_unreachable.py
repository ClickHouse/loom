"""A read-replica whose main Loom does not answer says so: its calls fail as
BACKEND_UNAVAILABLE instead of an internal error, and /v1/health reports the
main Loom unreachable instead of looking healthy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from server.clients import main_loom
from server.core.errors import BackendUnavailableError
from server.observability.problems import HealthState, collect_problems, derive_status
from server.settings import settings

MAIN = "https://main.loom.test"


@pytest.fixture(autouse=True)
def replica(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "read_replica", True)
    monkeypatch.setattr(settings, "read_replica_main_url", MAIN)
    monkeypatch.setattr(main_loom, "MAIN_STATE", {"reachable": None, "checked_at": "", "error": ""})
    monkeypatch.setattr(main_loom, "service_token", lambda: "t0k")

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(main_loom.asyncio, "sleep", no_wait)


def _main(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Any]) -> list[str]:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    monkeypatch.setattr(main_loom, "get_client", lambda: client)
    return seen


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


async def test_an_unreachable_main_loom_is_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _main(monkeypatch, _refuse)
    with pytest.raises(BackendUnavailableError) as e:
        await main_loom.call("embedding.embed", {"texts": ["x"]})
    assert e.value.status_code == 503 and e.value.code == "BACKEND_UNAVAILABLE"
    assert "embedding.embed" in e.value.message and MAIN not in e.value.message
    assert len(seen) == 2  # one retry on a dropped connection
    assert main_loom.MAIN_STATE["reachable"] is False
    assert "cannot connect" in main_loom.MAIN_STATE["error"]


async def test_a_completion_is_not_resent_after_it_may_have_been_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    seen = _main(monkeypatch, slow)
    with pytest.raises(BackendUnavailableError):
        await main_loom.call("llm.complete", {"user": "u"}, idempotent=False)
    assert len(seen) == 1


@pytest.mark.parametrize(("status", "attempts"), [(503, 2), (400, 1), (403, 1)])
async def test_only_a_retryable_status_is_tried_again(
    monkeypatch: pytest.MonkeyPatch, status: int, attempts: int
) -> None:
    seen = _main(monkeypatch, lambda r: httpx.Response(status, json={"detail": {}}))
    with pytest.raises(BackendUnavailableError, match=f"HTTP {status}"):
        await main_loom.call("embedding.embed", {"texts": ["x"]})
    assert len(seen) == attempts


async def test_an_answer_marks_the_main_loom_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    main_loom.MAIN_STATE.update(reachable=False, error="earlier")
    _main(monkeypatch, lambda r: httpx.Response(200, json={"vectors": []}))
    assert await main_loom.call("embedding.embed", {"texts": ["x"]}) == {"vectors": []}
    assert main_loom.MAIN_STATE["reachable"] is True and main_loom.MAIN_STATE["error"] == ""


async def test_the_probe_records_both_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _main(monkeypatch, lambda r: httpx.Response(200, json={"status": "ok"}))
    await main_loom.probe()
    assert seen == ["/v1/health"] and main_loom.MAIN_STATE["reachable"] is True
    _main(monkeypatch, _refuse)
    await main_loom.probe()
    assert main_loom.MAIN_STATE["reachable"] is False


async def test_the_probe_runs_every_read_replica_main_probe_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "read_replica_main_probe_seconds", 42.0)
    _main(monkeypatch, lambda r: httpx.Response(200, json={}))
    waits: list[float] = []

    class _Stop(Exception):
        pass

    async def sleep(s: float) -> None:
        waits.append(s)
        raise _Stop

    monkeypatch.setattr(main_loom.asyncio, "sleep", sleep)
    with pytest.raises(_Stop):
        await main_loom.probe_loop()
    assert waits == [42.0] and main_loom.MAIN_STATE["reachable"] is True


def test_health_degrades_on_an_unreachable_main_loom() -> None:
    gone = HealthState(env={}, main_reachable=False, main_error="health: cannot connect")
    problems = collect_problems(settings, gone)
    p = next(p for p in problems if p.code == "main_loom_unreachable")
    assert p.severity == "error" and "LOOM_READ_REPLICA_MAIN_URL" in p.fix
    assert derive_status(True, problems) == "degraded"
    for state in (HealthState(env={}, main_reachable=True), HealthState(env={})):
        assert "main_loom_unreachable" not in {p.code for p in collect_problems(settings, state)}


def test_the_main_loom_itself_never_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "read_replica", False)
    state = HealthState(env={}, main_reachable=False)
    assert "main_loom_unreachable" not in {p.code for p in collect_problems(settings, state)}


def test_both_transports_name_the_failure() -> None:
    from server.transport.mcp_refusals import server_failure

    body = server_failure(BackendUnavailableError("m", operation="llm.complete"))
    assert body is not None and body["code"] == "BACKEND_UNAVAILABLE" and body["status"] == 503
