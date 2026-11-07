"""Memory webhooks end to end: a webhook registered over HTTP, memory writes on
the server, the outbox rows they leave, and the delivery worker's pass (run
here against the same ClickHouse, narrowed to the test's namespace) POSTing
signed events to a receiver on 127.0.0.1.

The server must accept http to 127.0.0.1 (LOOM_MEMORY_WEBHOOK_ALLOW_HTTP=true,
LOOM_MEMORY_WEBHOOK_PRIVATE_HOSTS=127.0.0.1, as CI's integration job sets for the
server and the tests). A server that refuses the receiver's URL skips the delivery
tests, unless the tests run with that allowlist too: then the refusal fails them."""

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

from server.core.webhook_secrets import webhook_secret_org_prefix
from server.memory.workers import webhook_deliver
from server.settings import settings
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("memhook")
SECRET = "whsec_" + base64.b64encode(b"memory-webhook-test-secret-32by!").decode()
REF = webhook_secret_org_prefix(ORG) + "CHAT"
# The wire contract: every key a body carries, in this order.
BODY_KEYS = [
    "type",
    "event_id",
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_id",
    "version",
    "actor",
    "request_id",
    "erasure_id",
    "occurred_at",
]
# Read before the receiver fixture patches these settings: what this process was started with.
STARTED_WITH_ALLOWLIST = bool(
    settings.memory_webhook_allow_http and settings.memory_webhook_private_hosts
)


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
def receiver(monkeypatch: pytest.MonkeyPatch) -> Iterator[Receiver]:
    # This process runs the worker: it may reach the receiver too.
    monkeypatch.setattr(settings, "memory_webhook_allow_http", True)
    monkeypatch.setattr(settings, "memory_webhook_private_hosts", "127.0.0.1")
    r = Receiver()
    yield r
    r.server.shutdown()
    r.server.server_close()


@pytest.fixture(autouse=True)
async def _ch_connected() -> Any:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


class Ops:
    def __init__(self, client: httpx.AsyncClient, token: str, ns: str) -> None:
        self.client, self.token, self.ns = client, token, ns

    async def __call__(self, op: str, *, expect: int = 200, **body: Any) -> dict[str, Any]:
        body.setdefault("org", ORG)
        if op not in {"memory.update_by_id", "memory.redact", "erasure.request"}:
            body.setdefault("namespace", self.ns)
        r = await self.client.post(
            f"/v1/{op}", json=body, headers={"authorization": f"Bearer {self.token}"}
        )
        assert r.status_code == expect, (op, r.status_code, r.text)
        return r.json()

    async def register(self, url: str, **body: Any) -> dict[str, Any]:
        r = await self.client.post(
            "/v1/webhook.register",
            json={"org": ORG, "namespace": self.ns, "url": url, "secret_ref": REF, **body},
            headers={"authorization": f"Bearer {self.token}"},
        )
        # A run configured as CI is (the allowlist in this process's environment, shared
        # with the server) fails on a refusal: it can only be a regression.
        if r.status_code == 422 and "url refused" in r.text and not STARTED_WITH_ALLOWLIST:
            pytest.skip(
                "the server refuses http to 127.0.0.1: start it and the tests with "
                "LOOM_MEMORY_WEBHOOK_ALLOW_HTTP=true LOOM_MEMORY_WEBHOOK_PRIVATE_HOSTS=127.0.0.1"
            )
        assert r.status_code == 200, r.text
        hook = r.json()["webhook"]
        listed = await self("webhook.list")
        assert hook["webhook_id"] in [h["webhook_id"] for h in listed["webhooks"]]
        await self._until_announced(url)
        return hook

    async def _until_announced(self, url: str) -> None:
        """Until the server's processes announce writes for webhooks registered now:
        a probe webhook, registered after the test's own, in a namespace of its own,
        and probe writes until three in a row leave an event (each process reads the
        registry within its refresh interval, and every registration in one read)."""
        probe = f"{self.ns}-probe"
        probe_hook = await self("webhook.register", namespace=probe, url=url, secret_ref=REF)
        in_a_row = 0
        for i in range(120):
            key = f"probe/{i}"
            await self(
                "memory.set",
                namespace=probe,
                agent="probe",
                memory_key=key,
                value=f"probe {uuid.uuid4().hex}",
            )
            seen = await self("webhook.deliveries", namespace=probe, limit=500)
            in_a_row = in_a_row + 1 if key in {d["memory_key"] for d in seen["deliveries"]} else 0
            if in_a_row == 3:
                await self(
                    "webhook.remove",
                    namespace=probe,
                    webhook_id=probe_hook["webhook"]["webhook_id"],
                )
                return
            await asyncio.sleep(0.25)
        raise AssertionError("the server never announced a probe write")


@pytest.fixture
async def ops(control_plane_url: str, mint_test_token: Any) -> Any:
    ns = f"chat-{uuid.uuid4().hex[:6]}"
    # org_admin: a user's erasure spans the org's namespaces.
    token = mint_test_token(org=ORG, namespaces=["*"], roles=["org_admin", "admin", "writer"])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as client:
        yield Ops(client, token, ns)


async def _deliver_until(receiver: Receiver, ns: str, n: int) -> None:
    """Run the worker's pass over the namespace until the receiver holds `n`
    requests (a row written a moment ago can take a moment to read back)."""
    for _ in range(40):
        await webhook_deliver.deliver_once(env={REF: SECRET}, org=ORG, namespace=ns)
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


def _outbox(ch_client: Any, ns: str) -> list[tuple[str, str, int]]:
    rows = ch_client.query(
        "SELECT event_type, memory_key, memory_version FROM loom.memory_webhook_outbox "
        "WHERE org = {org:String} AND namespace = {ns:String} ORDER BY occurred_at, event_type",
        parameters={"org": ORG, "ns": ns},
        settings={"select_sequential_consistency": 1},
    ).result_rows
    return [(str(t), str(k), int(v)) for t, k, v in rows]


async def test_set_and_delete_reach_the_receiver_signed_and_without_content(
    ops: Ops, receiver: Receiver
) -> None:
    hook = await ops.register(receiver.url, events=["memory.written", "memory.tombstoned"])
    assert hook["url"] == receiver.url and hook["secret_ref"] == REF
    listed = await ops("webhook.list")
    assert [h["webhook_id"] for h in listed["webhooks"]] == [hook["webhook_id"]]

    ident = {"agent": "assistant", "user_id": "u-42", "memory_key": "user/pref/tea"}
    written = await ops("memory.set", value="prefers green tea, no sugar", **ident)
    await _deliver_until(receiver, ops.ns, 1)
    await ops("memory.delete", **ident)
    await _deliver_until(receiver, ops.ns, 2)
    assert (await ops("webhook.remove", webhook_id=hook["webhook_id"]))["removed"] is True
    assert (await ops("webhook.list"))["webhooks"] == []

    first, second = receiver.events()
    assert first["type"] == "memory.written" and second["type"] == "memory.tombstoned"
    for event in (first, second):
        assert list(event) == BODY_KEYS
        assert (event["org"], event["namespace"], event["agent"], event["user_id"]) == (
            ORG,
            ops.ns,
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
    ops: Ops, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver.statuses = [500]
    await ops.register(receiver.url)
    await ops("memory.set", agent="assistant", memory_key="user/name", value="Sam")
    await _deliver_until(receiver, ops.ns, 1)
    pending = await ops("webhook.deliveries", status="pending")
    assert [(d["attempts"], d["last_status"]) for d in pending["deliveries"]] == [(1, 500)]

    # The retry is due 5 s later: the worker's clock runs 6 s ahead.
    ahead = timedelta(seconds=6)
    monkeypatch.setattr(
        webhook_deliver, "naive_utc_now", lambda: datetime.now(UTC).replace(tzinfo=None) + ahead
    )
    await _deliver_until(receiver, ops.ns, 2)
    for _ in range(20):
        done = await ops("webhook.deliveries", status="delivered")
        if done["deliveries"]:
            break
        await asyncio.sleep(0.25)
    (first_h, _b1), (second_h, _b2) = receiver.requests
    assert first_h["webhook-id"] == second_h["webhook-id"]  # the same event, retried
    assert [(d["attempts"], d["last_status"]) for d in done["deliveries"]] == [(2, 200)]


async def test_the_admin_ops_reach_only_their_own_namespace(
    ops: Ops, receiver: Receiver, mint_test_token: Any
) -> None:
    hook = await ops.register(receiver.url)
    other_ns = f"{ops.ns}-other"
    other = Ops(
        ops.client,
        mint_test_token(org=ORG, namespaces=[other_ns], roles=["admin", "writer"]),
        other_ns,
    )
    # Another namespace's admin can neither remove nor see the webhook.
    removed = await other("webhook.remove", webhook_id=hook["webhook_id"])
    assert removed["removed"] is False
    assert (await other("webhook.list"))["webhooks"] == []
    listed = await ops("webhook.list")
    assert [h["webhook_id"] for h in listed["webhooks"]] == [hook["webhook_id"]]

    # A writer is refused all four ops.
    writer = Ops(
        ops.client, mint_test_token(org=ORG, namespaces=[ops.ns], roles=["writer"]), ops.ns
    )
    await writer("webhook.register", expect=403, url=receiver.url, secret_ref=REF)
    await writer("webhook.list", expect=403)
    await writer("webhook.deliveries", expect=403)
    await writer("webhook.remove", expect=403, webhook_id=hook["webhook_id"])
    assert (await ops("webhook.remove", webhook_id=hook["webhook_id"]))["removed"] is True


async def test_each_write_path_leaves_its_event_and_version(
    ops: Ops, receiver: Receiver, ch_client: Any
) -> None:
    await ops.register(receiver.url)
    a = {"agent": "assistant"}
    first = await ops("memory.set", memory_key="k/one", value="first value", **a)
    # The same value again is a restatement: no event.
    restated = await ops("memory.set", memory_key="k/one", value="first value", **a)
    revised = await ops("memory.set", memory_key="k/one", value="second value", **a)
    bulk = await ops(
        "memory.bulk_set",
        entries=[
            {"memory_key": "k/two", "value": "bulk new"},
            {"memory_key": "k/one", "value": "bulk revision"},
        ],
        **a,
    )
    # An unchanged entry of a batch writes nothing: no event.
    await ops("memory.bulk_set", entries=[{"memory_key": "k/two", "value": "bulk new"}], **a)
    by_id = await ops("memory.update_by_id", memory_id=first["memory_id"], value="by id")
    deleted = await ops("memory.delete", memory_key="k/one", **a)
    restored = await ops("memory.restore", memory_key="k/one", **a)
    head = await ops("memory.get", memory_key="k/two", **a)
    dv = await ops("memory.delete_version", memory_key="k/two", version=head["version"], **a)
    redacted = await ops(
        "memory.redact", memory_id=first["memory_id"], version=first["version"], reason="pii"
    )
    assert redacted["redacted"] is True
    # Writes that write nothing announce nothing.
    await ops("memory.set", memory_key="k/dry", value="dry run", dry_run=True, **a)
    await ops("blacklist.add", pattern="forbidden-word")
    await ops("memory.set", memory_key="k/no", value="a forbidden-word", expect=422, **a)

    versions = {r["memory_key"]: r["version"] for r in bulk["written"]}
    expected = [
        ("memory.written", "k/one", first["version"]),
        ("memory.revised", "k/one", revised["version"]),
        ("memory.written", "k/two", versions["k/two"]),
        ("memory.revised", "k/one", versions["k/one"]),
        ("memory.revised", "k/one", by_id["new_version"]),
        ("memory.tombstoned", "k/one", deleted["version"]),
        ("memory.restored", "k/one", restored["version"]),
        ("memory.tombstoned", "k/two", dv["version"]),
        ("memory.redacted", "k/one", first["version"]),
        # The restatement copies the redacted text, so the redaction scrubs it too.
        ("memory.redacted", "k/one", restated["version"]),
    ]
    for _ in range(20):
        if len(_outbox(ch_client, ops.ns)) >= len(expected):
            break
        await asyncio.sleep(0.25)
    assert sorted(_outbox(ch_client, ops.ns)) == sorted(expected)


async def test_clear_and_a_conflict_resolve_announce_what_they_tombstone(
    ops: Ops, receiver: Receiver, ch_client: Any
) -> None:
    a = {"agent": "assistant"}
    # 0.60 admits the fake n-gram embedder's distance for the two style texts below
    # (test_memory_conflicts uses the same pair).
    await ops("namespace.register", owner_email="itest@loom", conflict_threshold=0.60, **a)
    await ops.register(receiver.url, events=["memory.tombstoned"])

    # memory.clear: every live row of the user, at the version it held.
    await ops("memory.set", memory_key="k/kept", value="another user's value", **a)
    cleared = await ops("memory.set", memory_key="k/cleared", user_id="u-42", value="v", **a)
    out = await ops("memory.clear", user_id="u-42", confirm_subject="u-42")
    assert out["rows_affected"]["loom.memory"] == 1

    # conflicts.resolve(revise): the peer it tombstones.
    first = "Always use 2-space indentation"
    loser = await ops("memory.set", memory_key="style", value=first, **a)
    for _ in range(60):
        hits = await ops("memory.search", query=first, **a)
        if "style" in json.dumps(hits):
            break
        await asyncio.sleep(0.25)
    winner = await ops(
        "memory.set", memory_key="style_v2", value=first + " throughout the project", **a
    )
    for _ in range(60):
        if len((await ops("conflicts.list", **a))["rows"]) == 2:
            break
        await asyncio.sleep(0.25)
    resolved = await ops(
        "conflicts.resolve", memory_id=winner["memory_id"], decision="revise", resolved_by="t"
    )
    assert resolved["resolved"] is True and loser["memory_id"] in resolved["affected_memory_ids"]

    for _ in range(20):
        if len(_outbox(ch_client, ops.ns)) >= 2:
            break
        await asyncio.sleep(0.25)
    (clear_event, resolve_event) = sorted(_outbox(ch_client, ops.ns), key=lambda e: e[1])
    assert clear_event == ("memory.tombstoned", "k/cleared", cleared["version"])
    assert resolve_event[:2] == ("memory.tombstoned", "style")
    assert resolve_event[2] > loser["version"]


async def test_a_users_erasure_leaves_nothing_to_send(
    ops: Ops, receiver: Receiver, ch_client: Any
) -> None:
    await ops.register(receiver.url, events=["memory.written"])
    await ops("memory.set", agent="assistant", user_id="u-gone", memory_key="k", value="v")
    await ops("memory.set", agent="assistant", user_id="u-kept", memory_key="k", value="w")
    erased = await ops("erasure.request", scope_type="user_id", scope_value="u-gone", requester="t")
    assert erased["status"] == "completed"
    remaining = ch_client.query(
        "SELECT user_id FROM loom.memory_webhook_outbox "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": ops.ns},
        settings={"select_sequential_consistency": 1},
    ).result_rows
    assert [u for (u,) in remaining] == ["u-kept"]
    await _deliver_until(receiver, ops.ns, 1)
    assert [e["user_id"] for e in receiver.events()] == ["u-kept"]


async def test_a_namespace_erasure_is_announced_by_its_id(
    ops: Ops, receiver: Receiver, ch_client: Any
) -> None:
    await ops.register(receiver.url, events=["memory.erased"])
    await ops("memory.set", agent="assistant", memory_key="k", value="v")
    erased = await ops("erasure.request", scope_type="namespace", scope_value=ops.ns, requester="t")
    assert erased["status"] == "completed"
    await _deliver_until(receiver, ops.ns, 1)
    (event,) = receiver.events()
    assert event["type"] == "memory.erased" and event["erasure_id"] == erased["erasure_id"]
    assert (event["memory_key"], event["user_id"]) == ("", "-")
