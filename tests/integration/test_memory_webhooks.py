"""Memory webhooks end to end: a webhook registered over HTTP, memory.set and
memory.delete on the server, and the delivery worker's pass (run here, against
the same ClickHouse) POSTing signed events to a receiver on 127.0.0.1. The
server and this process allow it through LOOM_MEMORY_WEBHOOK_ALLOW_HTTP and
LOOM_MEMORY_WEBHOOK_PRIVATE_HOSTS=127.0.0.1, as CI's integration job sets."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest

from server.core.webhooks import webhook_secret_org_prefix
from server.memory.domain.webhook_events import BODY_FIELDS
from server.memory.workers import webhook_deliver
from server.settings import settings
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("memhook")
SECRET = "whsec_" + base64.b64encode(b"memory-webhook-test-secret-32by!").decode()
REF = webhook_secret_org_prefix(ORG) + "CHAT"


class Receiver:
    """An HTTP server on 127.0.0.1 that records each request and answers the
    next status queued (200 when none is)."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.statuses: list[int] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(outer.statuses.pop(0) if outer.statuses else 200)
                self.end_headers()

            def log_message(self, *_: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hooks/loom"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(body) for _h, body in self.requests]


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    r = Receiver()
    yield r
    r.server.shutdown()


@pytest.fixture(autouse=True)
async def _ch_connected() -> Any:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


@pytest.fixture(autouse=True)
def _allows_the_local_receiver() -> None:
    if not settings.memory_webhook_allow_http or "127.0.0.1" not in (
        settings.memory_webhook_private_host_set()
    ):
        pytest.fail(
            "set LOOM_MEMORY_WEBHOOK_ALLOW_HTTP=true and LOOM_MEMORY_WEBHOOK_PRIVATE_HOSTS="
            "127.0.0.1 for the server and the tests (CI's integration job does)"
        )


async def _op(client: httpx.AsyncClient, op: str, token: str, **body: Any) -> dict[str, Any]:
    r = await client.post(f"/v1/{op}", json=body, headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200, (op, r.status_code, r.text)
    return r.json()


async def _deliver_until(receiver: Receiver, n: int, *, now: datetime | None = None) -> None:
    """Run the worker's pass until the receiver holds `n` requests (a written row
    can take a moment to reach every replica's FINAL read)."""
    for _ in range(40):
        await webhook_deliver.deliver_once(now=now, env={REF: SECRET})
        if len(receiver.requests) >= n:
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"{len(receiver.requests)} of {n} deliveries arrived")


def _verify(headers: dict[str, str], body: bytes) -> None:
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert headers["webhook-signature"] == f"v1,{mac}"
    assert abs(int(headers["webhook-timestamp"]) - datetime.now(UTC).timestamp()) < 120


async def test_set_and_delete_reach_the_receiver_signed_and_without_content(
    control_plane_url: str, mint_test_token: Any, receiver: Receiver
) -> None:
    ns = f"chat-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as client:
        reg = await _op(
            client,
            "webhook.register",
            token,
            org=ORG,
            namespace=ns,
            url=receiver.url,
            secret_ref=REF,
            events=["memory.written", "memory.tombstoned"],
        )
        hook = reg["webhook"]
        assert hook["url"] == receiver.url and hook["secret_ref"] == REF
        listed = await _op(client, "webhook.list", token, org=ORG, namespace=ns)
        assert [h["webhook_id"] for h in listed["webhooks"]] == [hook["webhook_id"]]
        # Every server process reads the registry within its refresh interval.
        await asyncio.sleep(settings.memory_webhook_refresh_seconds + 1)

        written = await _op(
            client,
            "memory.set",
            token,
            org=ORG,
            namespace=ns,
            agent="assistant",
            user_id="u-42",
            memory_key="user/pref/tea",
            value="prefers green tea, no sugar",
            memory_type="semantic",
        )
        await _deliver_until(receiver, 1)
        await _op(
            client,
            "memory.delete",
            token,
            org=ORG,
            namespace=ns,
            agent="assistant",
            user_id="u-42",
            memory_key="user/pref/tea",
        )
        await _deliver_until(receiver, 2)
        removed = await _op(
            client, "webhook.remove", token, org=ORG, namespace=ns, webhook_id=hook["webhook_id"]
        )
        assert removed["removed"] is True
        assert (await _op(client, "webhook.list", token, org=ORG, namespace=ns))["webhooks"] == []

    first, second = receiver.events()
    assert first["type"] == "memory.written" and second["type"] == "memory.tombstoned"
    for event in (first, second):
        assert list(event) == list(BODY_FIELDS)
        assert (event["org"], event["namespace"], event["agent"], event["user_id"]) == (
            ORG,
            ns,
            "assistant",
            "u-42",
        )
        assert event["memory_key"] == "user/pref/tea"
        assert event["memory_id"] == written["memory_id"]
    assert first["version"] == written["version"]
    assert second["version"] > first["version"]
    for headers, body in receiver.requests:
        _verify(headers, body)
        assert b"green tea" not in body and b"sugar" not in body


async def test_a_500_is_retried_and_the_retry_is_delivered(
    control_plane_url: str, mint_test_token: Any, receiver: Receiver
) -> None:
    ns = f"chat-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    receiver.statuses = [500]
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as client:
        await _op(
            client,
            "webhook.register",
            token,
            org=ORG,
            namespace=ns,
            url=receiver.url,
            secret_ref=REF,
        )
        await asyncio.sleep(settings.memory_webhook_refresh_seconds + 1)
        await _op(
            client,
            "memory.set",
            token,
            org=ORG,
            namespace=ns,
            agent="assistant",
            memory_key="user/name",
            value="Sam",
            memory_type="semantic",
        )
        await _deliver_until(receiver, 1)
        pending = await _op(
            client, "webhook.deliveries", token, org=ORG, namespace=ns, status="pending"
        )
        assert [(d["attempts"], d["last_status"]) for d in pending["deliveries"]] == [(1, 500)]

        # The retry is due 5 s later; the pass is run as of then.
        later = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=6)
        await _deliver_until(receiver, 2, now=later)
        for _ in range(20):
            done = await _op(
                client, "webhook.deliveries", token, org=ORG, namespace=ns, status="delivered"
            )
            if done["deliveries"]:
                break
            await asyncio.sleep(0.25)
    (first_h, _b1), (second_h, _b2) = receiver.requests
    assert first_h["webhook-id"] == second_h["webhook-id"]  # the same event, retried
    assert [(d["attempts"], d["last_status"]) for d in done["deliveries"]] == [(2, 200)]
