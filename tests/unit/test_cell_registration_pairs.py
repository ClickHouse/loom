"""Later registration closes its accepted pair before publishing any directory row."""

from __future__ import annotations

import contextlib
import copy
import hashlib
from dataclasses import replace
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from server.router import app as router_app
from server.router import directory
from server.router.limits import MAX_BODY_BYTES
from server.settings import settings
from tests.unit.test_cell_register import OPERATOR, SHA, _body, _Listener, _post, _row
from tests.unit.test_router_attach import CURRENT


def _later_body() -> dict[str, Any]:
    current = copy.deepcopy(CURRENT)
    current["fields"].update(public_host="eu-2.loom.example", oauth_issuer_host="eu-2.loom.example")
    return _body(
        host="eu-2.loom.example",
        probe_url="http://eu-2.internal:7790",
        current_fp=current,
        next_fp=None,
    )


@pytest.fixture
def pair_router(monkeypatch):
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    monkeypatch.setattr(settings, "router_shared_host", "loom.example")
    monkeypatch.setattr(settings, "router_upstream_probe_url", "http://upstream.internal:7790")
    monkeypatch.setattr(settings, "router_upstream_url", "http://upstream.internal:7777")
    listener = _Listener()
    written = []

    async def refusal(_reg):
        return ""

    async def register(reg):
        written.append(reg)
        return _row(reg), True

    monkeypatch.setattr(directory, "refusal_before_probe", refusal)
    monkeypatch.setattr(directory, "register", register)
    router = router_app.Router(probe_transport=httpx.MockTransport(listener))
    with TestClient(router_app.build_app(router)) as client:
        yield client, listener, written


@pytest.mark.parametrize(
    "fault",
    ["missing-both", "missing-current", "missing-next", "current-type", "foreign", "first", "size"],
)
def test_http_registration_rejects_an_unclosed_pair_before_proof(pair_router, fault):
    client, listener, written = pair_router
    body = _later_body()
    if fault == "missing-both":
        del body["current_fp"], body["next_fp"]
    elif fault == "missing-current":
        del body["current_fp"]
    elif fault == "missing-next":
        del body["next_fp"]
    elif fault == "current-type":
        body["current_fp"] = []
    elif fault == "foreign":
        body["current_fp"]["fields"]["deployment_id"] = "dep-other"
    elif fault == "first":
        body = _body(current_fp=copy.deepcopy(CURRENT), next_fp=None)
    else:
        body["current_fp"]["fields"]["opaque"] = "x" * MAX_BODY_BYTES
    answer = _post(client, body, cell="eu-2")
    assert answer.status_code in {400, 413}
    assert listener.asked == []
    assert written == []


def _registration() -> directory.Registration:
    body = _later_body()
    return directory.Registration(
        cell="eu-2",
        host=body["host"],
        deployment_id=body["deployment_id"],
        credential_sha256=SHA,
        bucket=body["bucket"],
        bucket_prefix=body["bucket_prefix"],
        probe_url=body["probe_url"],
        upstream_url=body["upstream_url"],
        is_first=False,
        tenancy_rows=0,
        state_rows=0,
        prefix_objects=0,
        processes_without_cell=0,
        current_fp=body["current_fp"],
        next_fp=body["next_fp"],
    )


class _DirectoryConnection:
    def __init__(self, held=None, *, first=True):
        self.held = held
        self.first = first
        self.writes = []

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *args):
        if sql.startswith(("INSERT ", "UPDATE ", "DELETE ")):
            self.writes.append((sql, copy.deepcopy(args)))
        return ""

    async def fetchrow(self, sql, *args):
        if "WHERE is_first" in sql:
            return {"cell": "first", "state": "serving", "mode": "enforced"} if self.first else None
        if "WHERE cell = $1" in sql:
            return copy.deepcopy(self.held)
        raise AssertionError("unexpected directory read")

    async def fetchval(self, _sql, *_args):
        return None

    async def fetch(self, _sql, *_args):
        return []


@pytest.fixture
def directory_connection(monkeypatch):
    def connect(held=None, *, first=True):
        conn = _DirectoryConnection(held, first=first)

        @contextlib.asynccontextmanager
        async def connection(**_kwargs):
            yield conn

        monkeypatch.setattr(directory, "_connection", connection)
        return conn

    return connect


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing-current", "next-type", "foreign", "first", "size"])
async def test_direct_registration_revalidates_pair_before_any_insert(directory_connection, fault):
    reg = _registration()
    if fault == "missing-current":
        reg = replace(reg, current_fp=None)
    elif fault == "next-type":
        reg = replace(reg, next_fp=False)
    elif fault == "foreign":
        reg.current_fp["fields"]["deployment_id"] = "dep-other"
    elif fault == "first":
        reg = replace(
            reg,
            is_first=True,
            host="loom.example",
            current_fp=copy.deepcopy(CURRENT),
            probe_url="http://upstream.internal:7790",
            upstream_url="http://upstream.internal:7777",
        )
    else:
        reg.current_fp["fields"]["opaque"] = "x" * MAX_BODY_BYTES
    conn = directory_connection(first=fault != "first")
    with pytest.raises(directory.OperatorRefused):
        await directory.register(reg)
    assert conn.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["blank-old", "changed-current", "changed-next", "exact"])
async def test_registration_rerun_preserves_the_exact_published_pair(directory_connection, fault):
    reg = _registration()
    held = {
        **vars(_row(reg)),
        "bucket": reg.bucket,
        "bucket_prefix": reg.bucket_prefix,
        "probe_url": reg.probe_url,
        "upstream_url": reg.upstream_url,
        "current_fp": copy.deepcopy(reg.current_fp),
        "next_fp": copy.deepcopy(reg.next_fp),
    }
    if fault == "blank-old":
        held["current_fp"] = None
    elif fault == "changed-current":
        reg.current_fp["fields"]["opaque"] = "changed"
    elif fault == "changed-next":
        reg = replace(reg, next_fp=copy.deepcopy(reg.current_fp))
        reg.next_fp["fields"]["opaque"] = "pending"
    before = copy.deepcopy(held)
    conn = directory_connection(held)
    if fault == "exact":
        assert directory._rerun_refusal(held, reg) == ""
        row, written = await directory.register(reg)
        assert written is False
        assert row.current_fp == reg.current_fp
        assert row.next_fp == reg.next_fp
    else:
        assert directory._rerun_refusal(held, reg)
        with pytest.raises(directory.OperatorRefused):
            await directory.register(reg)
    assert conn.writes == []
    assert held == before
