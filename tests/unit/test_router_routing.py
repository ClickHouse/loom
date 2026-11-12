"""The router's routing (server/router/routing.py, the forward in server/router/app.py;
docs/architecture/cells.md, "The router", rules 1 to 4)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from server.router import app as router_app
from server.router import directory, routing
from server.settings import settings
from tests.unit.clock_fake import ManualClock

SHARED = "loom.example"


def _jwt(iss: str) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part({'iss': iss, 'sub': 'u'})}.sig"


def _cells(first_state: str = "serving") -> list[dict[str, Any]]:
    return [
        {
            "cell": "eu-1",
            "host": SHARED,
            "state": first_state,
            "is_first": True,
            "issuers": ["https://loom.example"],
            "upstream_url": "http://eu-1.internal:7777",
        },
        {
            "cell": "eu-2",
            "host": "eu-2.example",
            "state": "serving",
            "is_first": False,
            "issuers": ["https://eu-2.example", "https://idp.example/eu-2"],
            "upstream_url": "http://eu-2.internal:7777/",
        },
    ]


def _table(first_state: str = "serving", **kw: Any) -> routing.Table:
    return routing.build(
        kw.pop("cells", _cells(first_state)),
        kw.pop("aliases", [("loom-old.example", "eu-1")]),
        shared_host=SHARED,
        passthrough="http://passthrough:7777",
    )


def _to(table: routing.Table, **kw: Any) -> str | int:
    d = routing.decide(table, **{"path": "/v1/memory.set", **kw})
    return d.upstream or d.status


def test_a_cells_own_host_goes_to_that_cell() -> None:
    assert _to(_table(), host="eu-2.example") == "http://eu-2.internal:7777"
    assert _to(_table(), host="EU-2.Example:443") == "http://eu-2.internal:7777"


def test_a_host_no_cell_names_answers_421() -> None:
    assert _to(_table(), host="other.example") == 421


def test_the_shared_host_goes_to_the_first_cell_with_no_token() -> None:
    assert _to(_table(), host=SHARED) == "http://eu-1.internal:7777"
    assert _to(_table(), host="loom-old.example") == "http://eu-1.internal:7777"  # an alias


def test_a_token_on_the_shared_host_goes_by_its_issuer() -> None:
    auth = f"Bearer {_jwt('https://idp.example/eu-2')}"
    assert _to(_table(), host=SHARED, authorization=auth) == "http://eu-2.internal:7777"


def test_an_issuer_no_cell_accepts_answers_401() -> None:
    auth = f"Bearer {_jwt('https://stranger.example')}"
    assert _to(_table(), host=SHARED, authorization=auth) == 401


@pytest.mark.parametrize("token", ["opaque-key", "a.b", "a.!!!.c", f"{_jwt('x')[:-4]}"])
def test_a_token_that_is_no_jwt_goes_to_the_first_cell(token: str) -> None:
    assert (
        _to(_table(), host=SHARED, authorization=f"Bearer {token}") == "http://eu-1.internal:7777"
    )


def test_a_bearer_that_is_no_jwt_names_no_issuer() -> None:
    assert routing.issuer("opaque-key") == "" and routing.issuer("a.!!!.c") == ""
    nested = base64.urlsafe_b64encode(b"[" * 5000).decode().rstrip("=")
    assert routing.issuer(f"x.{nested}.y") == ""  # past the parser's depth, not a 500
    assert routing.issuer(_jwt("https://eu-2.example")) == "https://eu-2.example"


def test_scim_on_the_shared_host_is_the_first_cells_but_groups_with_a_jwt_go_by_issuer() -> None:
    auth = f"Bearer {_jwt('https://idp.example/eu-2')}"
    table = _table()
    assert _to(table, host=SHARED, path="/scim/v2/Users", authorization=auth) == (
        "http://eu-1.internal:7777"
    )
    assert _to(table, host=SHARED, path="/scim/v2/Groups/g1", authorization=auth) == (
        "http://eu-2.internal:7777"
    )
    assert _to(table, host=SHARED, path="/scim/v2/Groups", authorization="Bearer scim-tok") == (
        "http://eu-1.internal:7777"
    )


def test_a_revocation_goes_by_the_revoked_tokens_issuer_or_to_the_first_cell() -> None:
    table = _table()
    jwt = _jwt("https://eu-2.example")
    assert _to(table, host=SHARED, path="/oauth/revoke", revoked=jwt) == "http://eu-2.internal:7777"
    assert _to(table, host=SHARED, path="/oauth/revoke", revoked="opaque-refresh") == (
        "http://eu-1.internal:7777"
    )


def test_before_the_first_cell_serves_the_shared_host_goes_to_rule_4s_cell() -> None:
    auth = f"Bearer {_jwt('https://idp.example/eu-2')}"
    assert _to(_table("attaching"), host=SHARED, authorization=auth) == "http://eu-1.internal:7777"
    empty = routing.build([], [], shared_host=SHARED, passthrough="http://passthrough:7777")
    assert _to(empty, host=SHARED, authorization=auth) == "http://passthrough:7777"


def test_an_issuer_two_cells_list_goes_to_neither() -> None:
    cells = _cells()
    cells[0]["issuers"] = ["https://idp.example/eu-2"]
    auth = f"Bearer {_jwt('https://idp.example/eu-2')}"
    assert _to(_table(cells=cells), host=SHARED, authorization=auth) == 401


def test_a_registered_first_cell_with_no_upstream_answers_503_not_the_pass_through() -> None:
    cells = _cells()
    cells[0]["upstream_url"] = ""
    d = routing.decide(_table(cells=cells), host=SHARED, path="/v1/health")
    assert d.status == 503 and "first cell 'eu-1'" in d.reason


def test_a_cell_with_no_upstream_answers_503() -> None:
    cells = _cells()
    cells[1]["upstream_url"] = ""
    assert _to(_table(cells=cells), host="eu-2.example") == 503
    empty = routing.build([], [], shared_host=SHARED, passthrough="")
    assert _to(empty, host=SHARED) == 503


# The forward, through the router app.


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):  # type: ignore[override]
        for chunk in self.chunks:
            yield chunk


class _Upstream(httpx.AsyncBaseTransport):
    """A cell behind the router; answers streamed, as a real connection's are."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.down = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        self.seen.append(request)
        self.bodies.append(await request.aread())
        return httpx.Response(
            201,
            headers={"x-cell": request.url.host, "connection": "close", "trailer": "x"},
            stream=_Chunks(b"from ", request.url.host.encode()),
        )


@pytest.fixture
def routed(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, _Upstream, dict[str, Any]]]:
    state: dict[str, Any] = {"cells": _cells(), "down": False, "reads": 0}

    async def _routing() -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        state["reads"] += 1
        if state["down"]:
            raise directory.DirectoryUnavailable("the directory could not be read: OSError")
        return state["cells"], []

    monkeypatch.setattr(directory, "routing", _routing)
    monkeypatch.setattr(settings, "router_shared_host", SHARED)
    monkeypatch.setattr(settings, "router_upstream_url", "http://passthrough:7777")
    upstream = _Upstream()
    clock = ManualClock()
    router = router_app.Router(clock=clock)
    router._http = router_app.proxy_client(upstream)
    state["clock"] = clock
    state["router"] = router
    # One event loop for every request, and the lifespan run: what `serve` does.
    with TestClient(router_app.build_app(router)) as client:
        yield client, upstream, state


def test_a_request_is_forwarded_whole_with_where_it_came_from(routed) -> None:
    client, upstream, _ = routed
    r = client.post(
        "/v1/memory.set?x=1",
        content=b'{"key": "k"}',
        headers={"host": "eu-2.example", "content-type": "application/json", "x-request-id": "r1"},
    )
    assert (r.status_code, r.content, r.headers["x-cell"]) == (
        201,
        b"from eu-2.internal",
        "eu-2.internal",
    )
    assert "trailer" not in r.headers  # a hop-by-hop header of the cell's connection
    (seen,) = upstream.seen
    assert str(seen.url) == "http://eu-2.internal:7777/v1/memory.set?x=1"
    assert upstream.bodies == [b'{"key": "k"}']
    assert seen.headers["x-forwarded-host"] == "eu-2.example"
    assert seen.headers["x-forwarded-proto"] == "http"
    assert seen.headers["x-request-id"] == "r1" and seen.headers["host"] == "eu-2.example"


def test_a_revocation_is_forwarded_with_the_body_it_was_routed_by(routed) -> None:
    client, upstream, _ = routed
    form = f"token={_jwt('https://eu-2.example')}&token_type_hint=access_token"
    r = client.post(
        "/oauth/revoke",
        content=form.encode(),
        headers={"host": SHARED, "content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 201 and upstream.bodies == [form.encode()]
    assert upstream.seen[0].url.host == "eu-2.internal"


def test_the_routers_own_paths_are_never_forwarded(routed) -> None:
    client, upstream, _ = routed
    assert client.get("/cells/nope", headers={"host": SHARED}).status_code == 404
    assert upstream.seen == []


def test_a_cell_that_cannot_be_reached_answers_502(routed) -> None:
    client, upstream, _ = routed
    upstream.down = True
    assert client.get("/v1/health", headers={"host": "eu-2.example"}).status_code == 502


def test_routing_reads_the_directory_once_a_window_and_keeps_the_last_table(routed) -> None:
    client, upstream, state = routed
    assert client.get("/v1/health", headers={"host": "eu-2.example"}).status_code == 201
    assert client.get("/v1/health", headers={"host": "eu-2.example"}).status_code == 201
    assert state["reads"] == 1
    state["down"] = True
    state["clock"].advance(settings.router_directory_cache_s)
    assert client.get("/v1/health", headers={"host": "eu-2.example"}).status_code == 201
    assert client.get("/v1/health", headers={"host": "eu-2.example"}).status_code == 201
    assert state["reads"] == 2  # the failed read stands for the window


def test_no_table_yet_answers_503(routed) -> None:
    client, upstream, state = routed
    state["down"] = True
    assert client.get("/v1/health", headers={"host": SHARED}).status_code == 503
    assert upstream.seen == []


def test_a_host_no_cell_names_is_refused_unforwarded(routed) -> None:
    client, upstream, _ = routed
    r = client.get("/v1/health", headers={"host": "nobody.example"})
    assert r.status_code == 421 and upstream.seen == []


def test_every_header_line_of_the_answer_reaches_the_client(routed, monkeypatch) -> None:
    """Two Set-Cookie lines (a sign-in's link cookie and its state cookie's delete)."""
    client, upstream, _ = routed

    async def _two_cookies(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "a=1; Path=/"),
                ("set-cookie", "b=; Max-Age=0"),
                ("connection", "x-hop"),
                ("x-hop", "1"),
            ],
            stream=_Chunks(b"ok"),
        )

    monkeypatch.setattr(upstream, "handle_async_request", _two_cookies)
    r = client.get("/oauth/oidc/callback", headers={"host": SHARED})
    assert r.headers.get_list("set-cookie") == ["a=1; Path=/", "b=; Max-Age=0"]
    assert "x-hop" not in r.headers  # named by the cell's Connection header


def test_the_path_and_query_go_as_the_client_sent_them(routed) -> None:
    client, upstream, _ = routed
    client.get("/v1/x%3Fy%2Fz?q=a%20b", headers={"host": "eu-2.example"})
    assert upstream.seen[0].url.raw_path == b"/v1/x%3Fy%2Fz?q=a%20b"


def test_what_the_client_claims_about_where_it_came_from_is_replaced(routed) -> None:
    client, upstream, _ = routed
    client.get(
        "/v1/health",
        headers={
            "host": "eu-2.example",
            "x-forwarded-for": "6.6.6.6",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "evil.example",
            "forwarded": "for=6.6.6.6",
            "connection": "x-secret",
            "x-secret": "1",
        },
    )
    seen = upstream.seen[0].headers
    assert seen.get_list("x-forwarded-for") == ["testclient"]
    assert seen.get_list("x-forwarded-host") == ["eu-2.example"]
    assert seen.get_list("x-forwarded-proto") == ["http"]
    assert "forwarded" not in seen and "x-secret" not in seen


def test_a_revocation_goes_by_the_last_token_the_cell_reads(routed) -> None:
    client, upstream, _ = routed
    form = f"token={_jwt('https://loom.example')}&token={_jwt('https://eu-2.example')}"
    client.post(
        "/oauth/revoke",
        content=form.encode(),
        headers={"host": SHARED, "content-type": "application/x-www-form-urlencoded"},
    )
    assert upstream.seen[0].url.host == "eu-2.internal"


def test_a_revocation_past_the_bound_is_refused_unread(routed) -> None:
    client, upstream, _ = routed
    big = b"token=" + b"x" * (router_app.MAX_BODY_BYTES + 1)
    r = client.post("/oauth/revoke", content=big, headers={"host": SHARED})
    assert r.status_code == 413 and upstream.seen == []


def test_a_revocation_at_a_cells_own_host_is_forwarded_unread(routed) -> None:
    """Rule 1 needs no body: it streams on, however long."""
    client, upstream, _ = routed
    big = b"token=" + b"x" * (router_app.MAX_BODY_BYTES + 1)
    r = client.post("/oauth/revoke", content=big, headers={"host": "eu-2.example"})
    assert r.status_code == 201 and upstream.bodies == [big]


@pytest.mark.parametrize("path", ["/livez", "/readyz"])
def test_the_routers_own_probes_are_never_forwarded_whatever_the_method(routed, path) -> None:
    client, upstream, _ = routed
    assert client.post(path, headers={"host": SHARED}).status_code == 405
    assert upstream.seen == []


def test_a_directory_that_hangs_delays_no_request(routed, monkeypatch) -> None:
    """Once there is a table, a stale one is answered at once and read again behind it."""
    client, upstream, state = routed
    assert client.get("/v1/health", headers={"host": "eu-2.example"}).status_code == 201
    hung = asyncio.Event()

    async def _hang() -> Any:
        await hung.wait()

    monkeypatch.setattr(directory, "routing", _hang)
    state["clock"].advance(settings.router_directory_cache_s)
    answers: list[int] = []

    def _three() -> None:
        for _ in range(3):
            answers.append(client.get("/v1/health", headers={"host": "eu-2.example"}).status_code)

    worker = threading.Thread(target=_three, daemon=True)
    worker.start()
    worker.join(timeout=5)  # a request that waited on the hung read would still be running
    assert not worker.is_alive() and answers == [201, 201, 201]
    # The read still hangs behind them; the router's shutdown cancels it.
    refreshing = state["router"]._refreshing
    assert refreshing is not None and not refreshing.done()


def test_the_forward_has_no_connection_bound_and_no_read_timeout() -> None:
    assert router_app.PROXY_LIMITS.max_connections is None
    assert router_app.PROXY_TIMEOUT.read is None and router_app.PROXY_TIMEOUT.connect == 5.0


@pytest.mark.parametrize(
    "upstream",
    ["eu-2.internal:7777", "http://[bad", "http://x/base", "ftp://x", "http://u:p@x", "http://x?q"],
)
def test_an_upstream_that_is_no_bare_http_address_answers_503(upstream: str) -> None:
    cells = _cells()
    cells[1]["upstream_url"] = upstream
    assert _to(_table(cells=cells), host="eu-2.example") == 503
    assert not routing.usable_upstream(upstream)
    assert routing.usable_upstream("http://eu-2.internal:7777/")


def test_the_router_refuses_to_serve_with_an_unusable_pass_through(monkeypatch) -> None:
    from server.cli import router as router_cli

    monkeypatch.setattr(settings, "router_pg_dsn", "postgres://h/router")
    monkeypatch.setattr(settings, "pg_dsn", "postgres://h/loom")
    monkeypatch.setattr(settings, "router_upstream_url", "http://x/base")
    assert "LOOM_ROUTER_UPSTREAM_URL" in router_cli._refusal()
    monkeypatch.setattr(settings, "router_upstream_url", "http://deployment:7777")
    assert router_cli._refusal() == ""


def test_a_request_with_no_body_is_forwarded_with_none_framed(routed) -> None:
    client, upstream, _ = routed
    client.get("/v1/health", headers={"host": "eu-2.example"})
    seen = upstream.seen[0].headers
    assert "transfer-encoding" not in seen and seen.get("content-length") in (None, "0")


def test_a_revocation_whose_last_token_is_blank_goes_to_the_first_cell(routed) -> None:
    """As the cell reads the form: a blank last `token` is the one revoked."""
    client, upstream, _ = routed
    form = f"token={_jwt('https://eu-2.example')}&token="
    client.post(
        "/oauth/revoke",
        content=form.encode(),
        headers={"host": SHARED, "content-type": "application/x-www-form-urlencoded"},
    )
    assert upstream.seen[0].url.host == "eu-1.internal"


def test_an_issuer_no_cell_lists_answers_401_with_a_challenge(routed) -> None:
    client, upstream, _ = routed
    auth = f"Bearer {_jwt('https://stranger.example')}"
    r = client.post("/mcp", headers={"host": SHARED, "authorization": auth})
    assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert upstream.seen == []
