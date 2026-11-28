"""Preparation rejects unverified origins and refreshes routes after publication."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from server.cli import cell_control, cell_prepare
from server.core import cell
from server.router import app as router_app
from server.router import directory
from server.settings import settings
from tests.unit.test_cell_prepare import _args, _beat, _rows
from tests.unit.test_cell_register import OPERATOR, _body, _Listener, _post, _row
from tests.unit.test_cell_register import router as router
from tests.unit.test_router_attach import _cell_row
from tests.unit.test_router_routing import _cells

ORIGIN = "http://eu-2.internal:7777"
PROBE = "http://eu-2.internal:7790"


@pytest.mark.parametrize(
    "origin",
    [
        ...,
        None,
        "",
        "http://user@host",
        "http://host/path",
        "http://host?q=1",
        "http://host#x",
        "http://host:0",
        12,
        PROBE,
    ],
)
def test_invalid_later_origin_stops_before_store_probe_or_publication(router, origin):
    client, listener, calls = router
    result = _post(
        client, _body(host="eu-2.loom.example", probe_url=PROBE, upstream_url=origin), cell="eu-2"
    )
    assert result.status_code == 400
    assert listener.asked == [] and calls == []


@pytest.mark.parametrize("origin", [None, "", ORIGIN])
def test_first_registration_never_accepts_a_caller_main_origin(router, origin):
    client, listener, calls = router
    result = _post(client, _body(upstream_url=origin))
    assert result.status_code == 400
    assert listener.asked == [] and calls == []


def test_later_response_confirms_canonical_verified_origin_without_sending_credentials(
    router, monkeypatch
):
    client, listener, calls = router
    original = _Listener.__call__

    def checked(self, request):
        assert "authorization" not in request.headers
        return original(self, request)

    monkeypatch.setattr(_Listener, "__call__", checked)
    result = _post(
        client,
        _body(host="eu-2.loom.example", probe_url=PROBE, upstream_url="HTTP://EU-2.INTERNAL:7777/"),
        cell="eu-2",
    )
    assert result.status_code == 200
    assert result.json()["upstream_url"] == calls[0].upstream_url == ORIGIN
    assert result.json()["deployment_id"] == "dep-1"
    assert listener.asked == [PROBE + "/v1/health/deployment"]


def test_a_main_address_answering_as_a_probe_is_never_published(router):
    client, _listener, calls = router
    result = _post(
        client,
        _body(host="eu-2.loom.example", probe_url=PROBE, upstream_url="http://eu-2.internal:7791"),
        cell="eu-2",
    )
    assert result.status_code == 409
    assert "answered as the probe listener" in result.json()["error"]
    assert calls == []


@pytest.mark.parametrize(
    "origin", ["", "http://user@host", "http://host/path", "http://host?q=1", "http://host#x"]
)
def test_prepare_rejects_an_explicit_invalid_origin_before_native_admission(monkeypatch, origin):
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://cell.internal/loom")
    assert "--upstream-url" in cell_prepare._args_problem(
        _args(probe_url=PROBE, upstream_url=origin)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"upstream_url": None},
        {"upstream_url": PROBE},
        {"deployment_id": "another"},
        {"is_first": True},
    ],
)
@pytest.mark.asyncio
async def test_old_or_wrong_later_reply_stops_without_an_action_retry(monkeypatch, change):
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    asked = []
    answer = {
        "cell": "eu-2",
        "is_first": False,
        "state": "attaching",
        "mode": "enforced",
        "deployment_id": "dep-1",
        "upstream_url": ORIGIN,
        **change,
    }

    def response(request):
        asked.append(request)
        return httpx.Response(200, json=answer)

    with pytest.raises(cell_prepare.Refused, match="did not confirm"):
        await cell_prepare._register(
            argparse.Namespace(cell="eu-2", wait=0),
            httpx.URL("http://router.internal"),
            {"deployment_id": "dep-1", "upstream_url": ORIGIN},
            httpx.MockTransport(response),
        )
    assert len(asked) == 1


@pytest.mark.parametrize("unknown", [False, True], ids=["acknowledged", "unknown-commit"])
@pytest.mark.asyncio
async def test_registration_invalidates_after_an_inflight_old_routing_read(monkeypatch, unknown):
    monkeypatch.setattr(
        settings,
        "router_operator_sha256",
        hashlib.sha256(OPERATOR.encode()).hexdigest(),
    )
    monkeypatch.setattr(settings, "router_shared_host", "loom.example")
    entered, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = []

    async def routing():
        reads.append(True)
        if len(reads) == 1:
            entered.set()
            await release.wait()
        return _cells(), []

    async def register(reg):
        committed.set()
        await asyncio.sleep(0)
        if unknown:
            raise directory.DirectoryUnavailable("commit reply lost")
        return _row(reg), True

    monkeypatch.setattr(directory, "routing", routing)
    monkeypatch.setattr(directory, "refusal_before_probe", AsyncMock(return_value=""))
    monkeypatch.setattr(directory, "register", register)
    instance = router_app.Router(probe_transport=httpx.MockTransport(_Listener()))
    instance._credentials_at = 1.0
    refresh = asyncio.create_task(instance._refresh())
    await entered.wait()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router_app.build_app(instance)),
        base_url="http://router.internal",
    ) as client:
        pending = asyncio.create_task(
            client.post(
                "/cells/v1/operator/cells/eu-2/register",
                json=_body(host="eu-2.loom.example", probe_url=PROBE, upstream_url=ORIGIN),
                headers={"Authorization": "Bearer " + OPERATOR},
            )
        )
        await committed.wait()
        assert not pending.done()
        release.set()
        await refresh
        result = await pending
        assert result.status_code == (503 if unknown else 200)
        assert instance._credentials_at is None
        assert instance._table is not None
        assert instance._table_at is None
        await instance._routing_table()
        if instance._refreshing:
            await instance._refreshing
        assert len(reads) == 2


@pytest.mark.parametrize(
    "changed", [{"probe_url": "http://changed.internal:7790"}, {"upstream_url": None}]
)
@pytest.mark.asyncio
async def test_changed_registered_address_stops_before_local_claim_or_acknowledgment(
    monkeypatch, changed
):
    monkeypatch.setattr(settings, "cell", "eu-2")
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    monkeypatch.setattr(settings, "public_base_url", "https://eu-2.loom.example")
    monkeypatch.setattr(settings, "oauth_issuer", "")

    async def transaction(work):
        return await work()

    owner = SimpleNamespace(conn=None, transaction=transaction)
    rows = _rows()
    beat = _beat("web", cell.fingerprint("dep-1"), on="eu-2")
    for name, result in [
        ("_read_rows", rows),
        ("_read_beats", [beat]),
        ("_prefix_objects", 0),
        ("_write_nonces", None),
        ("_delete_nonces", None),
    ]:
        monkeypatch.setattr(cell_prepare, name, AsyncMock(return_value=result))
    monkeypatch.setattr(
        cell_prepare,
        "_register",
        AsyncMock(
            return_value={
                "cell": "eu-2",
                "is_first": False,
                "state": "attaching",
                "mode": "enforced",
                "deployment_id": "dep-1",
                "upstream_url": ORIGIN,
            }
        ),
    )
    snapshot = _cell_row(
        cell="eu-2",
        is_first=False,
        mode="enforced",
        host="eu-2.loom.example",
        upstream_url=ORIGIN,
        probe_url=PROBE,
        bucket="",
        bucket_prefix="",
    )
    snapshot.update(changed)
    monkeypatch.setattr(cell_control, "request", AsyncMock(return_value=snapshot))
    claim, write = AsyncMock(), AsyncMock()
    monkeypatch.setattr(cell_control, "claim", claim)
    monkeypatch.setattr(cell_prepare, "_write_rows", write)
    args = _args(cell="eu-2", host="eu-2.loom.example", probe_url=PROBE, upstream_url=ORIGIN)
    with pytest.raises(cell_prepare.Refused, match="registered later upstream or identity changed"):
        await cell_prepare._prepare_locked(
            None, args, httpx.URL("http://router.internal"), None, owner
        )
    claim.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.parametrize("unknown", [False, True], ids=["acknowledged", "unknown-commit"])
@pytest.mark.asyncio
async def test_cancelled_registration_cannot_make_an_older_routing_snapshot_fresh(
    monkeypatch, unknown
):
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    monkeypatch.setattr(settings, "router_shared_host", "loom.example")
    entered, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old = _cells()
    old[1].update(state="attaching", upstream_url="")
    published = _cells()
    published[1].update(state="attaching", upstream_url=ORIGIN)
    reads, writes = [], []

    async def routing():
        snapshot = old if not reads else published
        reads.append(True)
        if len(reads) == 1:
            entered.set()
            await release.wait()
        return snapshot, []

    async def register(reg):
        writes.append(reg.upstream_url)
        committed.set()
        if unknown:
            raise directory.DirectoryUnavailable("commit reply lost")
        return _row(reg), True

    monkeypatch.setattr(directory, "routing", routing)
    monkeypatch.setattr(directory, "refusal_before_probe", AsyncMock(return_value=""))
    monkeypatch.setattr(directory, "register", register)
    instance = router_app.Router(probe_transport=httpx.MockTransport(_Listener()))
    refresh = asyncio.create_task(instance._refresh())
    pending = None
    try:
        await entered.wait()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app.build_app(instance)),
            base_url="http://router.internal",
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    "/cells/v1/operator/cells/eu-2/register",
                    json=_body(host="eu-2.loom.example", probe_url=PROBE, upstream_url=ORIGIN),
                    headers={"Authorization": "Bearer " + OPERATOR},
                )
            )
            await committed.wait()
            assert writes == [ORIGIN]
            pending.cancel()
            try:
                answer = await pending
            except asyncio.CancelledError:
                pass
            else:
                assert answer.status_code == (503 if unknown else 200)
            release.set()
            await refresh
            await instance._routing_table()
            if instance._refreshing:
                await instance._refreshing
            assert (
                router_app.routing.decide(
                    instance._table, host="eu-2.example", path="/oauth/token"
                ).upstream
                == ORIGIN
            )
            assert len(reads) == 2
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
        await asyncio.gather(
            *(task for task in [pending, refresh] if task is not None), return_exceptions=True
        )


@pytest.mark.parametrize("unreachable", [False, True], ids=["wrong-deployment", "unreachable"])
def test_later_main_origin_refusal_names_the_supplied_fields(router, unreachable):
    client, listener, calls = router
    listener.upstream_down = unreachable
    listener.upstream_id = "another-deployment"
    answer = _post(
        client, _body(host="eu-2.loom.example", probe_url=PROBE, upstream_url=ORIGIN), cell="eu-2"
    )
    assert answer.status_code == (503 if unreachable else 409)
    error = answer.json()["error"]
    assert "upstream_url" in error
    assert "LOOM_ROUTER_UPSTREAM_URL" not in error
    if not unreachable:
        assert "probe_url" in error
    assert calls == []
