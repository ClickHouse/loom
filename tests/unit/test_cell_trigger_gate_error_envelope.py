"""Failed benchmark requests classify real wire errors without echoing payloads."""

import json

import httpx
import pytest
from fastapi import FastAPI

from bench import cell_trigger_gate as gate
from server.core.errors import register_exception_handler
from server.core.pg_lease import PublicationPending
from tests.unit.test_bench_cell_trigger_gate import args


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("error classification must not open a database")

    monkeypatch.setattr(gate.asyncpg, "connect", forbidden)
    monkeypatch.setattr(gate.clickhouse_connect, "get_async_client", forbidden)


@pytest.mark.asyncio
async def test_publication_pending_from_the_server_handler_retains_its_code(monkeypatch):
    app = FastAPI()
    register_exception_handler(app)

    @app.post("/v1/memory.set")
    async def pending():
        error = PublicationPending()
        error.message = "secret-token"
        raise error

    transport = httpx.ASGITransport(app=app)
    envelopes = []

    async def send(self, request, **kwargs):
        response = await transport.handle_async_request(request)
        await response.aread()
        envelopes.append(response.json())
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    events = []
    with pytest.raises(gate.Invalid, match="memory.set HTTP status"):
        await gate._memory_set(args(), emit=lambda kind, **row: events.append((kind, row)))
    assert len(envelopes) == 1 and "error" not in envelopes[0]
    assert envelopes[0]["detail"]["code"] == "PUBLICATION_PENDING"
    assert envelopes[0]["detail"]["committed"] is True
    assert len(events) == 1 and events[0][0] == "request_error"
    assert events[0][1]["status"] == 503
    assert events[0][1]["code"] == "PUBLICATION_PENDING"
    assert events[0][1]["measured"] is False
    assert "secret-token" not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate,expected",
    [
        pytest.param("BAD_CONFIG", "BAD_CONFIG", id="valid-code"),
        pytest.param("A" * 64, "A" * 64, id="maximum-length"),
        pytest.param("secret-token", "", id="unsafe-text"),
        pytest.param("A" * 65, "", id="overlong"),
        pytest.param(42, "", id="number"),
        pytest.param(None, "", id="null"),
        pytest.param([], "", id="list"),
    ],
)
async def test_detail_code_uses_the_existing_allowlist(monkeypatch, candidate, expected):
    async def send(self, request, **kwargs):
        return httpx.Response(
            503,
            request=request,
            json={"detail": {"code": candidate, "message": "secret-token"}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    events = []
    with pytest.raises(gate.Invalid, match="memory.set HTTP status"):
        await gate._memory_set(args(), emit=lambda kind, **row: events.append((kind, row)))
    assert len(events) == 1 and events[0][0] == "request_error"
    assert events[0][1]["status"] == 503
    assert events[0][1]["code"] == expected
    assert "secret-token" not in json.dumps(events)
