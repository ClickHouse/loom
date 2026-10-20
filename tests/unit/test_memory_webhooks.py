"""Memory webhooks: the body carries ids and never content, the write path's
outbox rows, the signed delivery and its retries, and the URL rules.

`loom.memory_webhooks` and `loom.memory_webhook_outbox` are written whole
through `WEBHOOK_COLS` / `OUTBOX_COLS`; the first test holds them to the
tables' columns."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from server.core import webhooks as signed
from server.core.errors import ValidationError
from server.memory.api.models_webhook import WebhookRegisterInput
from server.memory.app import webhooks as app
from server.memory.domain.webhook_events import BODY_FIELDS, EVENT_TYPES, event_body
from server.memory.store import webhooks as store
from server.memory.workers import webhook_deliver as worker
from server.settings import settings
from tests.unit.ch_fake import RecordingCH
from tests.unit.schema_ddl import column_names

NOW = datetime(2026, 9, 20, 12, 0)
REF = "LOOM_WEBHOOK_SECRET_ACME__CHAT"
SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
PUBLIC = "93.184.215.14"


def test_the_writers_carry_every_column_of_both_tables() -> None:
    assert list(store.WEBHOOK_COLS) == column_names("memory_webhooks")
    assert list(store.OUTBOX_COLS) == column_names("memory_webhook_outbox")


def _outbox_row(**over: Any) -> dict[str, Any]:
    row = app._outbox_row(
        "memory.written",
        "wh1",
        {
            "org": "acme",
            "namespace": "chat",
            "agent": "bot",
            "user_id": "u1",
            "session_id": "-",
            "memory_key": "user/pref/tea",
            "memory_id": "11111111-1111-1111-1111-111111111111",
            "version": 1_758_369_600_000_000_000,
        },
        actor="user:alice",
        now=NOW,
    )
    return {**row, **over}


# ------------------------------------------------------------ the body


def test_the_body_names_the_change_and_never_its_content() -> None:
    # A memory version row handed to announce carries its value, tags and evidence;
    # the outbox row and the body keep none of them.
    row = {
        **_outbox_row(),
        "value": "prefers green tea",
        "tags": ["diet"],
        "source_trace": "said so in chat",
    }
    body = json.loads(event_body(row))
    assert list(body) == list(BODY_FIELDS)
    assert body["type"] == "memory.written"
    assert body["memory_key"] == "user/pref/tea" and body["user_id"] == "u1"
    assert body["version"] == 1_758_369_600_000_000_000
    assert body["occurred_at"] == "2026-09-20T12:00:00.000Z"
    text = json.dumps(body)
    for content in ("green tea", "diet", "said so"):
        assert content not in text
    assert set(store.OUTBOX_COLS).isdisjoint({"value", "value_json", "tags", "source_trace"})


def test_an_event_type_outside_the_fixed_set_is_refused() -> None:
    assert set(EVENT_TYPES) == {
        "memory.written",
        "memory.revised",
        "memory.tombstoned",
        "memory.restored",
        "memory.redacted",
        "memory.erased",
    }
    with pytest.raises(PydanticValidationError):
        WebhookRegisterInput(
            org="acme", namespace="chat", url="https://x", secret_ref=REF, events=["memory.read"]
        )
    assert WebhookRegisterInput(
        org="acme", namespace="chat", url="https://x", secret_ref=REF
    ).events == list(EVENT_TYPES)


# ------------------------------------------------------------ the write path


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], Any]:
    """This process's copy of the registry, fresh for the test."""
    hooks: dict[tuple[str, str], Any] = {}
    monkeypatch.setattr(app, "_hooks", hooks)
    monkeypatch.setattr(app, "_next_read", float("inf"))
    return hooks


@pytest.mark.asyncio
async def test_announce_writes_one_row_per_subscribed_webhook(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    registry[("acme", "chat")] = (
        ("wh1", frozenset({"memory.written"})),
        ("wh2", frozenset({"memory.tombstoned"})),
    )
    ch = RecordingCH().install(monkeypatch)
    change = {
        "org": "acme",
        "namespace": "chat",
        "agent": "bot",
        "user_id": "u1",
        "session_id": "s1",
        "memory_key": "k",
        "memory_id": "m",
        "version": 7,
        "value": "secret text",
    }
    await app.announce("memory.written", [change])
    rows = ch.inserted("memory_webhook_outbox")
    assert [r["webhook_id"] for r in rows] == ["wh1"]
    row = rows[0]
    assert {k: row[k] for k in ("org", "namespace", "agent", "user_id", "session_id")} == {
        "org": "acme",
        "namespace": "chat",
        "agent": "bot",
        "user_id": "u1",
        "session_id": "s1",
    }
    assert (row["memory_key"], row["memory_id"], row["memory_version"]) == ("k", "m", 7)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert "secret text" not in json.dumps(rows, default=str)


@pytest.mark.asyncio
async def test_a_namespace_without_webhooks_pays_no_statement(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    ch = RecordingCH().install(monkeypatch)
    await app.announce("memory.written", [{"org": "acme", "namespace": "chat"}])
    assert ch.calls == []


@pytest.mark.asyncio
async def test_a_failed_outbox_insert_never_fails_the_write(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    registry[("acme", "chat")] = (("wh1", frozenset({"memory.written"})),)

    async def broken(rows: Any) -> None:
        raise RuntimeError("ClickHouse is away")

    monkeypatch.setattr(store, "insert_outbox", broken)
    await app.announce("memory.written", [{"org": "acme", "namespace": "chat"}])


@pytest.mark.asyncio
async def test_a_users_erasure_names_the_erasure_only(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    registry[("acme", "chat")] = (("wh1", frozenset({"memory.erased"})),)
    registry[("acme", "notes")] = (("wh2", frozenset({"memory.written"})),)
    registry[("globex", "chat")] = (("wh3", frozenset({"memory.erased"})),)
    ch = RecordingCH().install(monkeypatch)
    await app.announce_erasure("acme", None, "er-1")
    rows = ch.inserted("memory_webhook_outbox")
    assert [(r["namespace"], r["webhook_id"]) for r in rows] == [("chat", "wh1")]
    assert rows[0]["erasure_id"] == "er-1"
    assert rows[0]["user_id"] == "-" and rows[0]["memory_key"] == ""


# ------------------------------------------------------------ delivery


class FakeStore:
    def __init__(self, due: list[dict[str, Any]], hooks: list[dict[str, Any]]) -> None:
        self.due = due
        self.hooks = hooks
        self.writes: list[dict[str, Any]] = []

    async def due_outbox(self, now: datetime, *, limit: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.due][:limit]

    async def live_webhooks(self) -> list[dict[str, Any]]:
        return [dict(h) for h in self.hooks]

    async def insert_outbox(self, rows: list[dict[str, Any]]) -> None:
        self.writes.extend(dict(r) for r in rows)

    def last(self) -> dict[str, Any]:
        return self.writes[-1]


async def _public(host: str, **_: Any) -> tuple[str, str]:
    return "", PUBLIC


def _hook(url: str = "https://chat.example/hooks/loom") -> dict[str, Any]:
    return {"org": "acme", "namespace": "chat", "webhook_id": "wh1", "url": url, "secret_ref": REF}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fs = FakeStore([_outbox_row(webhook_id="wh1", event_id="ev-1")], [_hook()])
    monkeypatch.setattr(worker, "store", fs)
    monkeypatch.setattr(signed, "vet_host", _public)
    return fs


async def _pass(handler: Any, *, now: datetime = NOW, env: dict[str, str] | None = None) -> Any:
    async with httpx.MockTransport(handler) as transport:
        return await worker.deliver_once(
            transport=transport, now=now, env={REF: SECRET} if env is None else env
        )


@pytest.mark.asyncio
async def test_a_delivered_event_is_signed_over_its_timestamp_and_body(fake: FakeStore) -> None:
    seen: list[httpx.Request] = []
    result = await _pass(lambda r: seen.append(r) or httpx.Response(204))
    assert result.delivered == 1
    req = seen[0]
    assert str(req.url) == f"https://{PUBLIC}/hooks/loom"
    assert req.headers["host"] == "chat.example"
    assert req.headers["webhook-id"] == "ev-1"
    ts = req.headers["webhook-timestamp"]
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    mac = hmac.new(key, f"ev-1.{ts}.".encode() + req.content, hashlib.sha256).digest()
    assert req.headers["webhook-signature"] == "v1," + base64.b64encode(mac).decode()
    assert json.loads(req.content)["event_id"] == "ev-1"
    # leased first, then the outcome with the lease released
    assert fake.writes[0]["lease_until"] > NOW
    assert fake.last()["status"] == "delivered" and fake.last()["last_status"] == 204
    assert fake.last()["lease_until"] == datetime(1970, 1, 1)
    assert SECRET not in json.dumps(fake.writes, default=str)


@pytest.mark.asyncio
async def test_a_5xx_is_retried_with_backoff_and_then_marked_dead(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _pass(lambda r: httpx.Response(500))
    assert result.failed == 1
    out = fake.last()
    assert out["status"] == "pending" and out["attempts"] == 1 and out["last_status"] == 500
    assert out["next_try_at"] == NOW + timedelta(seconds=5)
    monkeypatch.setattr(settings, "memory_webhook_max_attempts", 2)
    fake.due = [out]
    result = await _pass(lambda r: httpx.Response(502), now=NOW + timedelta(seconds=6))
    assert result.dead == 1 and fake.last()["status"] == "dead"


@pytest.mark.asyncio
async def test_an_event_waits_when_its_secret_is_not_set(fake: FakeStore) -> None:
    seen: list[httpx.Request] = []
    result = await _pass(lambda r: seen.append(r) or httpx.Response(204), env={})
    assert seen == [] and result.failed == 1
    assert fake.last()["last_error"] == f"no secret in env {REF}"


@pytest.mark.asyncio
async def test_an_event_of_a_removed_webhook_is_dead_without_a_send(fake: FakeStore) -> None:
    fake.hooks = []
    seen: list[httpx.Request] = []
    await _pass(lambda r: seen.append(r) or httpx.Response(204))
    assert seen == []
    assert fake.last()["status"] == "dead"
    assert fake.last()["last_error"] == "the webhook was removed"


@pytest.mark.asyncio
async def test_another_orgs_secret_is_never_read(fake: FakeStore) -> None:
    fake.hooks = [{**_hook(), "secret_ref": "LOOM_WEBHOOK_SECRET_GLOBEX__CHAT"}]
    seen: list[httpx.Request] = []
    await _pass(
        lambda r: seen.append(r) or httpx.Response(204),
        env={"LOOM_WEBHOOK_SECRET_GLOBEX__CHAT": SECRET},
    )
    assert seen == []


# ------------------------------------------------------------ URL rules


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "resolves_to"),
    [
        ("https://hook.example/x", "10.0.0.1"),
        ("https://hook.example/x", "127.0.0.1"),
        ("https://hook.example/x", "169.254.169.254"),
        ("https://hook.example/x", "::ffff:192.168.1.1"),
    ],
    ids=["private", "loopback", "link-local", "mapped-private"],
)
async def test_a_url_resolving_inward_is_refused(
    monkeypatch: pytest.MonkeyPatch, url: str, resolves_to: str
) -> None:
    import server.core.hosts as hosts

    monkeypatch.setattr(hosts, "_resolve", lambda host: [resolves_to])
    monkeypatch.setattr(settings, "memory_webhook_private_hosts", "")
    assert await app.url_refusal(url)


@pytest.mark.asyncio
async def test_http_is_refused_unless_the_deployment_allows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(signed, "vet_host", _public)
    monkeypatch.setattr(settings, "memory_webhook_allow_http", False)
    assert await app.url_refusal("http://chat.example/hook")
    assert await app.url_refusal("ftp://chat.example/hook")
    assert await app.url_refusal("https://chat.example/hook") == ""
    monkeypatch.setattr(settings, "memory_webhook_allow_http", True)
    assert await app.url_refusal("http://chat.example/hook") == ""


@pytest.mark.asyncio
async def test_an_allowlisted_private_host_is_reached_by_its_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.core.hosts as hosts

    monkeypatch.setattr(hosts, "_resolve", lambda host: ["127.0.0.1"])
    monkeypatch.setattr(settings, "memory_webhook_private_hosts", "Receiver.Local, 127.0.0.1")
    assert await app.url_refusal("https://receiver.local:8443/h") == ""
    assert await signed.pinned_target(
        "http://127.0.0.1:9000/h", private_hosts=settings.memory_webhook_private_host_set()
    ) == ("", "http://127.0.0.1:9000/h", "", "")
    assert await app.url_refusal("https://other.local/h")


@pytest.mark.asyncio
async def test_register_refuses_a_secret_ref_of_another_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="LOOM_WEBHOOK_SECRET_ACME__"):
        await app.handle_webhook_register(
            WebhookRegisterInput(
                org="acme",
                namespace="chat",
                url="https://chat.example/h",
                secret_ref="LOOM_WEBHOOK_SECRET_GLOBEX__CHAT",
            )
        )


def test_the_worker_is_registered_on_its_settings() -> None:
    from server.workers.registry import enabled_workers, worker_meta

    spec = {s.name: s for s in enabled_workers()}["memory_webhook_deliver"]
    assert spec.interval_seconds == settings.memory_webhook_deliver_interval_seconds == 5
    assert spec.idle_interval_seconds == settings.memory_webhook_deliver_idle_interval_seconds
    assert spec.max_pass_seconds == 120.0
    assert worker_meta()["memory_webhook_deliver"]["enabled"] is (
        settings.memory_webhook_deliver_enabled
    )
