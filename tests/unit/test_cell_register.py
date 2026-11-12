"""The router's registration API (server/router/app.py `register`): `loom cell prepare`
registers a cell once its probe address answers the deployment id, the nonces prepare
wrote and the bucket and prefix it stores under; the first cell is probed at the router's
own address."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from server.router import app as router_app
from server.router import directory
from server.settings import settings

OPERATOR = "lco_" + "7" * 128
SHA = "a" * 64
NONCES = {"postgres": "n-pg", "clickhouse": "n-ch", "bucket": "n-b"}
SERVED = {"name": "docs", "prefix": "eu-1"}


HELD = {"tenancy_rows": 0, "state_rows": 0, "prefix_objects": 0, "processes_without_cell": 0}


def _body(**over: Any) -> dict[str, Any]:
    body = {
        "host": "loom.example",
        "deployment_id": "dep-1",
        "credential_sha256": SHA,
        "bucket": "docs",
        "bucket_prefix": "eu-1",
        "nonces": dict(NONCES),
        "held": dict(HELD),
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


class _Listener:
    """A probe listener at any address: what it answers, and where it was asked."""

    def __init__(self, status: int = 200, answer: Any = None, headers: Any = None) -> None:
        self.status, self.headers = status, headers or {}
        self.answer = (
            {"deployment_id": "dep-1", "nonces": dict(NONCES), "bucket": SERVED}
            if answer is None
            else answer
        )
        self.asked: list[str] = []
        # The first cell's upstream (the main port at :7777) answers the deployment id the
        # listener does unless this names another, or is down. Its status and body can be
        # another's.
        self.upstream_id: str | None = None
        self.upstream_down = False
        self.upstream_status, self.upstream_text = 200, ""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.port == 7777:
            if self.upstream_down:
                raise httpx.ConnectError("refused")
            if self.upstream_status != 200 or self.upstream_text:
                return httpx.Response(self.upstream_status, text=self.upstream_text)
            listened = self.answer.get("deployment_id") if isinstance(self.answer, dict) else ""
            return httpx.Response(200, json={"deployment_id": self.upstream_id or listened})
        self.asked.append(str(request.url))
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, str):
            return httpx.Response(self.status, text=self.answer, headers=self.headers)
        return httpx.Response(self.status, json=self.answer, headers=self.headers)


def _row(reg: directory.Registration) -> directory.Cell:
    return directory.Cell(
        cell=reg.cell,
        is_first=reg.is_first,
        host=reg.host,
        state="attaching",
        mode="recording" if reg.is_first else "enforced",
        closed=False,
        placements_max=None,
        placed=0,
        credential_sha256=reg.credential_sha256,
        deployment_id=reg.deployment_id,
        current_fp=None,
        next_fp=None,
        credential_next_sha256=None,
    )


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _Listener, list[Any]]:
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    monkeypatch.setattr(settings, "router_shared_host", "loom.example")
    monkeypatch.setattr(settings, "router_upstream_probe_url", "http://upstream.internal:7790/")
    monkeypatch.setattr(settings, "router_upstream_url", "http://upstream.internal:7777")
    listener = _Listener()
    calls: list[Any] = []

    async def _register(reg: directory.Registration) -> tuple[directory.Cell, bool]:
        calls.append(reg)
        if reg.deployment_id == "taken":
            raise directory.OperatorRefused("cell 'eu-2' records deployment id 'taken'")
        if reg.deployment_id == "down":
            raise directory.DirectoryUnavailable("the directory could not be read: OSError")
        return _row(reg), True

    async def _refusal_before_probe(reg: directory.Registration) -> str:
        if reg.deployment_id == "moved":
            return "cell 'eu-1' is registered with another probe address"
        if reg.deployment_id == "unread":
            raise directory.DirectoryUnavailable("the directory could not be read: OSError")
        return ""

    monkeypatch.setattr(directory, "register", _register)
    monkeypatch.setattr(directory, "refusal_before_probe", _refusal_before_probe)
    router = router_app.Router(probe_transport=httpx.MockTransport(listener))
    return TestClient(router_app.build_app(router)), listener, calls


def _post(client: TestClient, body: Any, cell: str = "eu-1", token: str = OPERATOR) -> Any:
    return client.post(
        f"/cells/v1/operator/cells/{cell}/register",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_the_first_cell_is_probed_at_the_routers_own_address(router) -> None:
    client, listener, calls = router
    r = _post(client, _body())
    assert r.status_code == 200
    assert r.json() == {
        "cell": "eu-1",
        "is_first": True,
        "state": "attaching",
        "mode": "recording",
        "registered": True,
        "current_fp": None,
        "next_fp": None,
    }
    assert listener.asked == ["http://upstream.internal:7790/v1/health/deployment"]
    (reg,) = calls
    assert (reg.tenancy_rows, reg.state_rows, reg.prefix_objects) == (0, 0, 0)
    assert reg.processes_without_cell == 0
    # Its row keeps the routes the router already had: the deployment it fronts.
    assert (reg.is_first, reg.probe_url, reg.upstream_url, reg.host) == (
        True,
        "http://upstream.internal:7790",
        "http://upstream.internal:7777",
        "loom.example",
    )


def test_the_first_cells_upstream_must_report_its_deployment_id(router) -> None:
    """Registration copies the upstream into the row: it is the deployment probed."""
    client, listener, calls = router
    listener.upstream_id = "dep-other"
    r = _post(client, _body())
    assert r.status_code == 409 and "reports another deployment id" in r.json()["error"]
    listener.upstream_down = True
    gone = _post(client, _body())  # nothing listening there
    assert (gone.status_code, gone.json()["code"]) == (503, "PROBE_UNREACHABLE")
    listener.upstream_id, listener.upstream_down = None, False
    listener.upstream_status, listener.upstream_text = 429, '{"error": "rate"}'
    busy = _post(client, _body())  # the main port past a rate: asked again
    assert (busy.status_code, busy.json()["code"]) == (503, "PROBE_UNREACHABLE")
    listener.upstream_status, listener.upstream_text = 502, "<html>bad gateway</html>"
    gateway = _post(client, _body())
    assert (gateway.status_code, gateway.json()["code"]) == (503, "PROBE_UNREACHABLE")
    listener.upstream_status = 500
    failed = _post(client, _body())  # a server error is no answer to ask again
    assert failed.status_code == 409 and "did not answer as Loom (500)" in failed.json()["error"]
    listener.upstream_status = 200
    page = _post(client, _body())  # something else answers there
    assert page.status_code == 409 and "did not answer as Loom" in page.json()["error"]
    # The probe listener's own answer at the upstream address: no main port.
    listener.upstream_text = json.dumps({"deployment_id": "dep-1", "nonces": NONCES})
    wrong = _post(client, _body())
    assert wrong.status_code == 409 and "answered as the probe listener" in wrong.json()["error"]
    assert calls == []


def test_an_upstream_without_the_postgres_authority_is_refused(router) -> None:
    """It answers no deployment id, and never will: refused at once, not retried."""
    client, listener, calls = router
    listener.upstream_text = '{"deployment_id": ""}'
    r = _post(client, _body())
    assert r.status_code == 409 and "Postgres authority" in r.json()["error"] and calls == []


def test_what_prepare_found_in_the_stores_reaches_the_directory(router) -> None:
    client, _listener, calls = router
    held = {"tenancy_rows": 3, "state_rows": 2, "prefix_objects": 1, "processes_without_cell": 4}
    assert _post(client, _body(held=held)).status_code == 200
    reg = calls[0]
    assert (reg.tenancy_rows, reg.state_rows, reg.prefix_objects) == (3, 2, 1)
    assert reg.processes_without_cell == 4


def test_a_host_that_normalises_to_the_shared_host_is_the_first_cell(router) -> None:
    client, _listener, calls = router
    assert _post(client, _body(host="LOOM.Example.")).json()["is_first"] is True
    assert calls[0].host == "loom.example"


def test_a_later_cell_is_probed_at_its_own_address(router) -> None:
    client, listener, calls = router
    body = _body(host="EU-2.Loom.Example.", probe_url="http://eu-2.internal:7790/")
    r = _post(client, body, cell="eu-2")
    assert r.status_code == 200 and r.json()["is_first"] is False
    assert r.json()["mode"] == "enforced"
    assert listener.asked == ["http://eu-2.internal:7790/v1/health/deployment"]
    assert (calls[0].host, calls[0].probe_url) == ("eu-2.loom.example", "http://eu-2.internal:7790")


def test_an_address_is_written_one_way_however_it_is_spelled(router) -> None:
    client, listener, calls = router
    body = _body(host="eu-2.loom.example", probe_url="HTTP://EU-2.Internal:80/")
    assert _post(client, body, cell="eu-2").status_code == 200
    assert calls[0].probe_url == "http://eu-2.internal"
    assert listener.asked == ["http://eu-2.internal/v1/health/deployment"]
    for spelled, written in (
        ("http://EU-2.internal.:7790", "http://eu-2.internal.:7790"),  # the dot kept
        ("http://[FD00:0:0:0:0:0:0:7]:7790", "http://[fd00::7]:7790"),
        ("https://eu-2.internal:443/", "https://eu-2.internal"),
    ):
        assert router_app._cell_address(spelled) == written


def test_a_rerun_the_directory_refuses_is_refused_before_the_probe(router) -> None:
    """Named at once as permanent, never probed and perhaps retried as unreachable."""
    client, listener, calls = router
    r = _post(client, _body(deployment_id="moved"))
    assert r.status_code == 409 and "another probe address" in r.json()["error"]
    unread = _post(client, _body(deployment_id="unread"))
    assert (unread.status_code, unread.json()["code"]) == (503, "BACKEND_UNAVAILABLE")
    assert listener.asked == [] and calls == []


def test_a_listener_echoing_the_longest_bucket_and_prefix_is_read(router) -> None:
    """The cap holds what a valid body makes the listener echo."""
    client, listener, calls = router
    longest = "\x01" * 1024  # six bytes each as JSON text, the most one character takes
    nonces = {k: "\x01" * 128 for k in NONCES}
    listener.answer = {
        "deployment_id": longest,
        "nonces": nonces,
        "bucket": {"name": longest, "prefix": longest},
    }
    body = _body(deployment_id=longest, bucket=longest, bucket_prefix=longest, nonces=nonces)
    assert _post(client, body).status_code == 200 and len(calls) == 1


@pytest.mark.parametrize(
    "host",
    [
        "eu-2.example:99999",
        "eu 2.loom.example",
        "eu|2.loom.example",
        "a,b.example",
        "[fd00:0::7]",  # an IPv6 address not in its compressed spelling: two rows for one
    ],
)
def test_a_host_it_cannot_read_is_named_as_such(router, host) -> None:
    """Checked label by label, as an address is: a cell's host is never moved."""
    client, listener, calls = router
    r = _post(client, _body(host=host))
    assert r.status_code == 400 and r.json()["error"] == "host names no valid host"
    assert listener.asked == [] and calls == []


def test_an_empty_upstream_is_never_asked(router, monkeypatch) -> None:
    """With no LOOM_ROUTER_UPSTREAM_URL the listener alone is probed (a rerun needs none;
    the directory refuses a new first cell without one: tests/integration)."""
    client, listener, calls = router
    monkeypatch.setattr(settings, "router_upstream_url", "")
    listener.upstream_down = True  # were it asked, the registration would answer 503
    assert _post(client, _body()).status_code == 200
    assert listener.asked == ["http://upstream.internal:7790/v1/health/deployment"]
    assert calls[0].upstream_url == ""


def test_a_first_cells_malformed_probe_url_is_named_as_such(router) -> None:
    client, listener, calls = router
    r = _post(client, _body(probe_url="x"))
    assert r.status_code == 400 and "probe_url, when given" in r.json()["error"]
    assert listener.asked == [] and calls == []


def test_the_first_cell_names_no_probe_address(router) -> None:
    client, listener, calls = router
    r = _post(client, _body(probe_url="http://elsewhere:7790"))
    assert r.status_code == 400 and listener.asked == [] and calls == []


@pytest.mark.parametrize(
    "setting,value",
    [
        ("router_upstream_probe_url", ""),
        ("router_upstream_probe_url", "upstream.internal:7790"),  # no scheme
    ],
)
def test_no_first_cell_is_probed_without_the_routers_probe_address(
    router, monkeypatch, setting, value
) -> None:
    client, listener, calls = router
    monkeypatch.setattr(settings, setting, value)
    r = _post(client, _body())
    assert r.status_code == 409 and "LOOM_ROUTER_UPSTREAM_PROBE_URL" in r.json()["error"]
    assert listener.asked == [] and calls == []


@pytest.mark.parametrize(
    "value",
    [
        "",
        "up.internal:7777",  # no scheme
        "http://up.internal:7777/loom",  # a path the forward drops
    ],
)
def test_an_unusable_upstream_reaches_the_directory_as_none(router, monkeypatch, value) -> None:
    """The directory refuses a new first cell without it, and a rerun needs none."""
    client, _listener, calls = router
    monkeypatch.setattr(settings, "router_upstream_url", value)
    assert _post(client, _body()).status_code == 200
    assert calls[0].upstream_url == ""


@pytest.mark.parametrize(
    "probe_url",
    [
        "",
        "ftp://eu-2.internal",
        "http://user:pw@eu-2.internal",
        "http://eu-2.internal/?x",
        "eu-2",
        "http://eu-2.internal:77\n90",  # not printable
        "http://eu-2.internal/probe",  # a path: the router appends its own
        "http://999.0.0.1:7790",  # no IPv4 address
        " http://eu-2.internal:7790",  # httpx keeps the space
        "http://\u2603.example",
        "http://eu-2.internal?",  # an empty query still ends the address
        "http://eu-2.internal#",
        "http://eu|2:7790",  # no host name
        "http://eu-2.internal,x:7790",
        "http://eu-2..internal:7790",  # an empty label
        "http://eu-2.-internal:7790",  # a label starting with a hyphen
        "http://eu-2.internal:77900",  # no port
        "http://eu-2.internal:-1",
        "http://eu-2.internal:1_0",
        "http://eu-2.internal:\u0667\u0667\u0669\u0660",  # another script's digits
        "http://eu-2.internal:0",
        "http://10.1:7790",  # numbers that resolve as an IPv4 address it does not spell
        "http://0xa.0.0.1:7790",
        "http://010.0.0.1:7790",
        "http://10.0.0.1.:7790",  # a final dot on numbers: no address
        "http://[fe80::1%\u00e9]:7790",  # a zone id no host can carry
    ],
)
def test_a_later_cells_probe_address_must_be_http_with_a_host(router, probe_url) -> None:
    client, listener, calls = router
    r = _post(client, _body(host="eu-2.loom.example", probe_url=probe_url), cell="eu-2")
    assert r.status_code == 400 and listener.asked == [] and calls == []


def test_the_probe_is_asked_uncompressed_and_capped_on_the_wire(router) -> None:
    """A compressed answer is never inflated: its bytes as received are the answer."""
    import gzip

    client, _listener, calls = router
    asked: list[str] = []
    answer = {"deployment_id": "dep-1", "nonces": NONCES, "bucket": SERVED}

    def _gzip(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers.get("accept-encoding", ""))
        body = gzip.compress(json.dumps(answer).encode())
        return httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"})

    zipped = router_app.Router(probe_transport=httpx.MockTransport(_gzip))
    r = _post(TestClient(router_app.build_app(zipped)), _body())
    assert r.status_code == 409 and "did not answer as a probe listener" in r.json()["error"]
    assert asked == ["identity"] and calls == []


@pytest.mark.parametrize(
    "answer,why",
    [
        ({"deployment_id": "dep-other", "nonces": NONCES, "bucket": SERVED}, "deployment id"),
        (  # another bucket name: no bucket nonce read, and the bucket named
            {
                "deployment_id": "dep-1",
                "nonces": {**NONCES, "bucket": None},
                "bucket": {"name": "docs-b", "prefix": "eu-1"},
            },
            "another bucket or prefix",
        ),
        (
            {"deployment_id": "dep-1", "nonces": {**NONCES, "postgres": "n-old"}, "bucket": SERVED},
            "stores prepare",
        ),
        (
            {"deployment_id": "dep-1", "nonces": NONCES, "bucket": {"name": "docs", "prefix": ""}},
            "another bucket or prefix",
        ),
        ("<html>not a listener</html>", "did not answer as a probe listener"),
        ({"deployment_id": "dep-1"}, "answered as a main port"),  # the main port's answer
        ({"status": 404}, "did not answer as a probe listener (404)"),
        ({"status": 500}, "did not answer as a probe listener (500)"),
        ("x" * (router_app.PROBE_ANSWER_MAX + 1), "did not answer as a probe listener"),
        ("[" * 4000, "did not answer as a probe listener"),  # nested past the parser's depth
    ],
)
def test_a_probe_that_does_not_echo_this_deployment_refuses(router, answer, why) -> None:
    """A copy of another deployment's stores answers that deployment's id or none of
    this prepare's nonces."""
    client, listener, calls = router
    if isinstance(answer, dict) and "status" in answer:  # no listener at that port
        listener.status, answer = answer["status"], {"detail": "Not Found"}
    listener.answer = answer
    r = _post(client, _body())
    assert r.status_code == 409 and why in r.json()["error"] and calls == []


def test_a_busy_or_unreachable_probe_answers_503_with_a_code(router) -> None:
    client, listener, calls = router
    listener.status, listener.headers = 429, {"Retry-After": "7"}
    busy = _post(client, _body())
    assert (busy.status_code, busy.json()["code"], busy.headers["retry-after"]) == (
        503,
        "PROBE_BUSY",
        "7",
    )
    listener.status, listener.answer = 200, httpx.ConnectError("refused")
    gone = _post(client, _body())
    assert (gone.status_code, gone.json()["code"]) == (503, "PROBE_UNREACHABLE")
    listener.status, listener.answer = (
        503,
        {"error": "postgres could not be read", "store": "postgres"},
    )
    store = _post(client, _body())
    assert (store.status_code, store.json()["code"]) == (503, "PROBE_UNREACHABLE")
    assert "could not read postgres" in store.json()["error"]
    # A 503 that names no store of the listener's is a gateway's, with no healthy listener.
    listener.answer = {"error": "no healthy upstream", "store": "x"}
    gateway = _post(client, _body())
    assert (gateway.status_code, gateway.json()["code"]) == (503, "PROBE_UNREACHABLE")
    assert "could not be reached" in gateway.json()["error"]
    for status in (502, 504):  # a gateway in front of a listener it could not reach
        listener.status, listener.answer = status, "<html>bad gateway</html>"
        r = _post(client, _body())
        assert (r.status_code, r.json()["code"]) == (503, "PROBE_UNREACHABLE")
    assert calls == []


@pytest.mark.parametrize(
    "body",
    [
        _body(credential_sha256="lcr_" + "0" * 128),  # the credential, not its hash
        _body(host=""),
        _body(deployment_id=""),
        _body(nonces={**NONCES, "bucket": None}),  # a bucket with no bucket nonce
        _body(bucket="", nonces=NONCES),  # a bucket nonce with no bucket
        _body(nonces={**NONCES, "bucket": ""}),
        _body(bucket="", bucket_prefix="cells/x", nonces={**NONCES, "bucket": None}),
        _body(nonces={"postgres": "n"}),
        _body(extra=1),
        _body(bucket_prefix=7),
        _body(nonces={**NONCES, "postgres": "n" * 129}),  # past a nonce's length
        _body(bucket_prefix="p" * 1025),  # past a field's length
        _body(bucket_prefix=""),  # a bucket names its prefix, as a cell's settings do
        _body(bucket_prefix="eu-1/"),
        _body(bucket_prefix="/eu-1"),
        _body(deployment_id="dep\x001"),
        _body(held=...),  # what prepare found in the stores is always named
        _body(held={"tenancy_rows": 0}),
        _body(held={**HELD, "tenancy_rows": -1}),
        _body(held={**HELD, "state_rows": True}),
        _body(held={**HELD, "prefix_objects": 2**31}),
        _body(held={**HELD, "other": 0}),
        [],
    ],
)
def test_a_malformed_registration_answers_400_and_probes_nothing(router, body) -> None:
    client, listener, calls = router
    assert _post(client, body).status_code == 400 and listener.asked == [] and calls == []


def test_a_cell_never_registers_with_the_operator_credentials_hash(router) -> None:
    """A cell holding it could make every operator change."""
    client, listener, calls = router
    operator_sha = hashlib.sha256(OPERATOR.encode()).hexdigest()
    r = _post(client, _body(credential_sha256=operator_sha))
    assert r.status_code == 400 and "operator credential's hash" in r.json()["error"]
    rotated = client.post(
        "/cells/v1/operator/cells/eu-1/credential",
        json={"step": "next", "sha256": operator_sha},
        headers={"Authorization": f"Bearer {OPERATOR}"},
    )
    assert rotated.status_code == 400 and "operator credential's hash" in rotated.json()["error"]
    assert listener.asked == [] and calls == []


def test_a_cells_credential_never_operates(router, monkeypatch) -> None:
    """Even with its hash configured as the operator's, an `lcr_` credential is a cell's."""
    client, listener, calls = router
    cell_token = "lcr_" + "7" * 128
    sha = hashlib.sha256(cell_token.encode()).hexdigest()
    monkeypatch.setattr(settings, "router_operator_sha256", sha)
    assert _post(client, _body(), token=cell_token).status_code == 401
    assert listener.asked == [] and calls == []


def test_a_body_is_judged_before_the_routers_configuration(router, monkeypatch) -> None:
    client, listener, calls = router
    monkeypatch.setattr(settings, "router_shared_host", "")
    r = _post(client, _body(bucket="", bucket_prefix="cells/x", nonces={**NONCES, "bucket": None}))
    assert r.status_code == 400 and "bucket_prefix" in r.json()["error"]
    bad_probe = _post(client, _body(probe_url="ftp://eu-2.internal"), cell="eu-2")
    assert bad_probe.status_code == 400 and "probe_url" in bad_probe.json()["error"]


class _CountingConnection:
    """A directory connection that counts the statements sent through it: one each
    query, and the transaction's begin and commit."""

    def __init__(self, first: str | None) -> None:
        self.first, self.statements, self.inserted = first, 0, False

    async def execute(self, sql: str, *_a: Any) -> str:
        self.statements += 1
        self.inserted = self.inserted or sql.startswith("INSERT INTO loom_router.cells ")
        return ""

    async def fetchval(self, sql: str, *_a: Any) -> Any:
        self.statements += 1
        return self.first if "WHERE is_first" in sql else None

    async def fetchrow(self, sql: str, *_a: Any) -> Any:
        self.statements += 1
        if not self.inserted:
            return None  # a new registration: no row until it inserts one
        return {
            f: None
            if f in ("placements_max", "current_fp", "next_fp", "credential_next_sha256")
            else 0
            if f == "placed"
            else False
            if f in ("is_first", "closed")
            else "x"
            for f in directory.Cell.__dataclass_fields__
        }

    async def fetch(self, *_a: Any) -> list[Any]:
        self.statements += 1
        return []

    def transaction(self) -> Any:
        connection = self

        class _Tx:
            async def __aenter__(self) -> None:
                connection.statements += 1

            async def __aexit__(self, *exc: Any) -> None:
                connection.statements += 1

        return _Tx()


def test_the_registration_bounds_count_the_statements_the_code_sends(monkeypatch) -> None:
    """REGISTER_MAX_S and REFUSAL_READ_MAX_S are counted from the code, so a statement
    added to either path fails here until the bound counts it."""
    import contextlib

    def _connected(conn: _CountingConnection) -> Any:
        @contextlib.asynccontextmanager
        async def _connection(**_kw: Any) -> Any:
            yield conn

        @contextlib.asynccontextmanager
        async def _reading() -> Any:
            yield

        monkeypatch.setattr(directory, "_connection", _connection)
        monkeypatch.setattr(directory, "_reading", _reading)

    later = directory.Registration(
        cell="eu-2",
        host="eu-2.loom.example",
        deployment_id="dep-2",
        credential_sha256="b" * 64,
        bucket="docs",
        bucket_prefix="eu-2",
        probe_url="http://eu-2.internal:7790",
        is_first=False,
        tenancy_rows=0,
        state_rows=0,
        prefix_objects=0,
        processes_without_cell=0,
    )
    read = _CountingConnection(first="eu-1")
    _connected(read)
    assert asyncio.run(directory.refusal_before_probe(later)) == ""
    assert read.statements == directory.REFUSAL_READ_STATEMENTS
    write = _CountingConnection(first="eu-1")
    _connected(write)
    asyncio.run(directory.register(later))
    assert write.statements == directory.REGISTER_STATEMENTS


def test_a_lone_surrogate_is_malformed_never_an_outage(router) -> None:
    """JSON can carry one and Postgres cannot store it: 400, not a retryable 503."""
    client, listener, calls = router
    raw = json.dumps(_body(deployment_id="\ud800"))  # escaped, as a client may send it
    r = client.post(
        "/cells/v1/operator/cells/eu-1/register",
        content=raw,
        headers={"Authorization": f"Bearer {OPERATOR}", "Content-Type": "application/json"},
    )
    assert r.status_code == 400 and listener.asked == [] and calls == []


def test_the_directorys_refusal_and_outage_are_answered(router) -> None:
    client, listener, _calls = router
    listener.answer = {"deployment_id": "taken", "nonces": NONCES, "bucket": SERVED}
    refused = _post(client, _body(deployment_id="taken"))
    assert refused.status_code == 409 and "records deployment id" in refused.json()["error"]
    listener.answer = {"deployment_id": "down", "nonces": NONCES, "bucket": SERVED}
    down = _post(client, _body(deployment_id="down"))
    assert (down.status_code, down.json()["code"]) == (503, "BACKEND_UNAVAILABLE")


def test_registration_needs_the_operator_credential(router) -> None:
    client, listener, calls = router
    assert _post(client, _body(), token="lco_" + "0" * 128).status_code == 401
    assert _post(client, _body(), cell="EU_1").status_code == 404
    assert listener.asked == [] and calls == []


def test_a_registration_naming_no_bucket_cannot_pass_for_a_cell_that_has_one(router) -> None:
    """No `_cell_probe` object is the normal state of a bucket in use: the listener names
    the bucket it stores under, so a body that names none is refused."""
    client, listener, calls = router
    # It has a bucket, and no probe object in it: its bucket nonce reads null.
    listener.answer = {
        "deployment_id": "dep-1",
        "nonces": {**NONCES, "bucket": None},
        "bucket": SERVED,
    }
    body = _body(bucket="", bucket_prefix="", nonces={**NONCES, "bucket": None})
    r = _post(client, body)
    assert r.status_code == 409 and "another bucket or prefix" in r.json()["error"]
    assert calls == []


def test_a_cell_with_no_bucket_registers_against_a_listener_with_none(router) -> None:
    client, listener, calls = router
    listener.answer = {
        "deployment_id": "dep-1",
        "nonces": {**NONCES, "bucket": None},
        "bucket": {"name": "", "prefix": ""},
    }
    body = _body(bucket="", bucket_prefix="", nonces={**NONCES, "bucket": None})
    assert _post(client, body).status_code == 200
    assert (calls[0].bucket, calls[0].bucket_prefix) == ("", "")


def test_a_busy_probe_with_no_retry_after_is_asked_again_in_a_second(router) -> None:
    client, listener, _calls = router
    listener.status = 429
    r = _post(client, _body())
    assert (r.status_code, r.json()["code"], r.headers["retry-after"]) == (503, "PROBE_BUSY", "1")


@pytest.mark.parametrize(
    "given,passed",
    [("12", "12"), ("0", "1"), ("€", "1"), ("9" * 20, "1"), ("soon", "1"), ("²", "1"), ("١", "1")],
)
def test_a_busy_probes_retry_after_is_passed_on_only_as_a_short_count(
    router, given, passed
) -> None:
    client, listener, _calls = router
    listener.status, listener.headers = (
        429,
        [(b"Retry-After", given.encode("utf-8"))],
    )
    r = _post(client, _body())
    assert (r.status_code, r.headers["retry-after"]) == (503, passed)


def test_a_probe_that_answers_past_its_deadline_is_unreachable(router, monkeypatch) -> None:
    client, _listener, calls = router
    monkeypatch.setattr(router_app, "PROBE_DEADLINE_S", 0.05)

    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    slow = router_app.Router(probe_transport=httpx.MockTransport(_slow))
    r = _post(TestClient(router_app.build_app(slow)), _body())
    assert (r.status_code, r.json()["code"]) == (503, "PROBE_UNREACHABLE") and calls == []


@pytest.mark.parametrize("outcome", ["written", "rerun", "down"])
def test_after_a_registration_that_may_have_written_the_new_credential_places_at_once(
    monkeypatch, outcome
) -> None:
    """This replica reads the cells' hashes again after a registration answered 200 or a
    503 whose commit may have landed, so the new cell's credential is known at once."""
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    monkeypatch.setattr(settings, "router_shared_host", "loom.example")
    monkeypatch.setattr(settings, "router_upstream_probe_url", "http://upstream.internal:7790")
    monkeypatch.setattr(settings, "router_upstream_url", "http://upstream.internal:7777")
    hashes: dict[str, tuple[str, ...]] = {}

    async def _credentials() -> dict[str, tuple[str, ...]]:
        return dict(hashes)

    async def _register(reg: directory.Registration) -> Any:
        hashes[reg.cell] = (reg.credential_sha256,)  # the commit lands
        if outcome == "down":
            raise directory.DirectoryUnavailable("the directory could not be read: OSError")
        return _row(reg), outcome == "written"

    async def _no_refusal(_reg: directory.Registration) -> str:
        return ""

    monkeypatch.setattr(directory, "credentials", _credentials)
    monkeypatch.setattr(directory, "register", _register)
    monkeypatch.setattr(directory, "refusal_before_probe", _no_refusal)
    router = router_app.Router(probe_transport=httpx.MockTransport(_Listener()))
    asyncio.run(router._credential("eu-1"))  # cached: no eu-1 yet
    _post(TestClient(router_app.build_app(router)), _body())
    assert asyncio.run(router._credential("eu-1")) == (SHA,)


@pytest.mark.parametrize("shared_host", ["", "https://loom.example:99999"])
def test_no_cell_registers_without_the_shared_host(router, monkeypatch, shared_host) -> None:
    """Unset, or naming no host it can read: never taken for no first cell."""
    client, listener, calls = router
    monkeypatch.setattr(settings, "router_shared_host", shared_host)
    r = _post(client, _body())
    assert r.status_code == 409 and "LOOM_ROUTER_SHARED_HOST" in r.json()["error"]
    assert listener.asked == [] and calls == []


@pytest.mark.parametrize(
    "value",
    [
        "upstream.internal:7790",
        "http://up/probe",
        "http://up:77\n90",
        "http://999.0.0.1:1",
        " http://up:1",
    ],
)
def test_the_router_refuses_to_serve_with_an_unusable_probe_address(monkeypatch, value) -> None:
    from server.cli import router as router_cli

    monkeypatch.setattr(settings, "router_pg_dsn", "postgres://h/router")
    monkeypatch.setattr(settings, "pg_dsn", "postgres://h/loom")
    monkeypatch.setattr(settings, "router_upstream_url", "")
    monkeypatch.setattr(settings, "router_upstream_probe_url", value)
    assert "LOOM_ROUTER_UPSTREAM_PROBE_URL" in router_cli._refusal()
    monkeypatch.setattr(settings, "router_upstream_probe_url", "http://up:7790")
    assert router_cli._refusal() == ""


def test_the_router_refuses_to_serve_with_an_unreadable_shared_host(monkeypatch) -> None:
    from server.cli import router as router_cli

    monkeypatch.setattr(settings, "router_pg_dsn", "postgres://h/router")
    monkeypatch.setattr(settings, "pg_dsn", "postgres://h/loom")
    for unreadable in ("https://loom.example:99999", "loom example.com", "10.1"):
        monkeypatch.setattr(settings, "router_shared_host", unreadable)
        assert "LOOM_ROUTER_SHARED_HOST" in router_cli._refusal()
    monkeypatch.setattr(settings, "router_shared_host", "loom.example")
    assert router_cli._refusal() == ""
