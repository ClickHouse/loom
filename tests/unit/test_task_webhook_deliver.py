"""Signed webhooks for the task inbox plane (wake-up layer):
Standard Webhooks signing, the backoff schedule, and one pass over a fake
repository with ``httpx.MockTransport`` standing in for the receiver. The
worker is `task_webhook_deliver`; the secret never reaches ClickHouse or a
log line."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from server.tasks.workers import webhook_deliver as wd

SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _event(seq: int, event: str = "created") -> dict[str, Any]:
    return {
        "org": "acme",
        "inbox": "private-bot",
        "task_id": f"00000000-0000-0000-0000-{seq:012d}",
        "seq": seq,
        "event": event,
        "from_status": "",
        "to_status": "triage",
        "ts": NOW - timedelta(seconds=10),
    }


class FakeRepo:
    def __init__(self) -> None:
        self.inboxes: list[dict[str, Any]] = []
        self.cursors: dict[tuple[str, str], dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.reads: list[int] = []

    next_version = staticmethod(lambda: 1_700_000_000_000_000_000)

    async def list_webhook_inboxes(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.inboxes]

    async def fetch_webhook_cursors(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.cursors.values()]

    async def insert_webhook_cursor(self, row: dict[str, Any]) -> None:
        self.writes.append(dict(row))
        self.cursors[(row["org"], row["inbox"])] = dict(row)

    async def events_since(self, org, *, since_seq, watermark, inbox=None, limit=200, **_):
        self.reads.append(since_seq)
        return [
            dict(e)
            for e in self.events
            if e["org"] == org and e["inbox"] == inbox and e["seq"] > since_seq
        ][:limit]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fr = FakeRepo()
    monkeypatch.setattr(wd, "repo", fr)
    wd._warned_missing_secret.clear()
    fr.inboxes.append(
        {
            "org": "acme",
            "inbox": "private-bot",
            "owner_namespace": "private",
            "webhook_url": "https://bot.example.invalid/hook",
            "webhook_secret_ref": "PRIVATE_BOT_WEBHOOK_SECRET",
        }
    )
    fr.events.extend([_event(101), _event(102, "accepted")])
    return fr


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------ signing


def test_signature_is_v1_base64_hmac_over_id_dot_timestamp_dot_body() -> None:
    body = b'{"a":1}'
    key = wd.secret_bytes(SECRET)
    assert key == b"0123456789abcdef0123456789abcdef"  # whsec_ marks a base64 key
    assert wd.secret_bytes("plain-secret") == b"plain-secret"
    sig = wd.sign(key, msg_id="102", timestamp=1_758_369_600, body=body)
    expected = base64.b64encode(
        hmac.new(key, b"102.1758369600." + body, hashlib.sha256).digest()
    ).decode()
    assert sig == f"v1,{expected}"
    headers = wd.signed_headers(key, msg_id="102", timestamp=1_758_369_600, body=body)
    assert headers["webhook-id"] == "102"
    assert headers["webhook-timestamp"] == "1758369600"
    assert headers["webhook-signature"] == sig
    assert headers["content-type"] == "application/json"


def test_backoff_is_next_pass_then_5s_5min_30min_capped() -> None:
    assert [wd.backoff_seconds(n) for n in range(0, 7)] == [0, 0, 5, 300, 1800, 1800, 1800]


def test_notice_carries_ids_and_kinds_never_task_text() -> None:
    rows = [{**_event(101), "title": "UB in Parser", "brief": "{}"}]
    body = json.loads(wd.notice_body("acme", "private-bot", 100, rows))
    assert body["type"] == "task.inbox.events"
    assert body["inbox"] == "private-bot" and body["since_seq"] == 100 and body["last_seq"] == 101
    assert body["count"] == 1 and body["pull"] == "task.pull"
    assert body["events"][0]["event"] == "created"
    assert "UB in Parser" not in json.dumps(body)


# ------------------------------------------------------------ the pass


@pytest.mark.asyncio
async def test_a_2xx_advances_the_cursor_and_resets_attempts(fake: FakeRepo, caplog) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    caplog.set_level(logging.DEBUG)
    async with _client(handler) as client:
        result = await wd.deliver_once(
            client=client, now=NOW, env={"PRIVATE_BOT_WEBHOOK_SECRET": SECRET}
        )
    assert result.delivered == 1 and result.failed == 0
    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == "https://bot.example.invalid/hook"
    body = json.loads(req.content)
    assert body["since_seq"] == 0 and body["last_seq"] == 102 and body["count"] == 2
    assert req.headers["webhook-id"] == "102"
    ts = int(req.headers["webhook-timestamp"])
    assert req.headers["webhook-signature"] == wd.sign(
        wd.secret_bytes(SECRET), msg_id="102", timestamp=ts, body=req.content
    )
    cursor = fake.cursors[("acme", "private-bot")]
    assert cursor["last_seq"] == 102 and cursor["attempts"] == 0 and cursor["last_status"] == 204
    assert cursor["next_try_at"] == NOW.replace(tzinfo=None)
    # the secret is in no log line and in no cursor row
    assert SECRET not in caplog.text and "0123456789abcdef" not in caplog.text
    assert SECRET not in json.dumps(fake.writes, default=str)


@pytest.mark.asyncio
async def test_a_5xx_keeps_the_cursor_and_backs_off(fake: FakeRepo) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    env = {"PRIVATE_BOT_WEBHOOK_SECRET": SECRET}
    async with _client(handler) as client:
        r1 = await wd.deliver_once(client=client, now=NOW, env=env)
        cur = fake.cursors[("acme", "private-bot")]
        assert r1.failed == 1 and cur["attempts"] == 1 and cur["last_seq"] == 0
        assert cur["next_try_at"] == NOW.replace(tzinfo=None)  # immediate: next pass
        r2 = await wd.deliver_once(client=client, now=NOW, env=env)
        cur = fake.cursors[("acme", "private-bot")]
        assert r2.failed == 1 and cur["attempts"] == 2
        assert cur["next_try_at"] == (NOW + timedelta(seconds=5)).replace(tzinfo=None)
        # not due yet: skipped without a request
        r3 = await wd.deliver_once(client=client, now=NOW + timedelta(seconds=2), env=env)
        assert r3.skipped == 1 and r3.failed == 0
        r4 = await wd.deliver_once(client=client, now=NOW + timedelta(seconds=6), env=env)
        cur = fake.cursors[("acme", "private-bot")]
        assert r4.failed == 1 and cur["attempts"] == 3
        assert cur["next_try_at"] == (NOW + timedelta(seconds=6 + 300)).replace(tzinfo=None)
    # every attempt re-read from the acknowledged cursor
    assert fake.reads == [0, 0, 0]


@pytest.mark.asyncio
async def test_no_answer_in_the_ack_window_counts_as_status_0(fake: FakeRepo) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow receiver", request=request)

    async with _client(handler) as client:
        r = await wd.deliver_once(
            client=client, now=NOW, env={"PRIVATE_BOT_WEBHOOK_SECRET": SECRET}
        )
    assert r.failed == 1
    assert fake.cursors[("acme", "private-bot")]["last_status"] == 0


@pytest.mark.asyncio
async def test_a_missing_secret_holds_the_notice_and_warns_once(fake: FakeRepo, caplog) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    caplog.set_level(logging.WARNING)
    async with _client(handler) as client:
        r1 = await wd.deliver_once(client=client, now=NOW, env={})
        r2 = await wd.deliver_once(client=client, now=NOW, env={})
    assert r1.skipped == 1 and r2.skipped == 1 and calls == 0 and not fake.writes
    assert sum("no secret in env" in rec.message for rec in caplog.records) == 1


@pytest.mark.asyncio
async def test_nothing_new_makes_no_request(fake: FakeRepo) -> None:
    fake.cursors[("acme", "private-bot")] = {
        "org": "acme",
        "inbox": "private-bot",
        "last_seq": 102,
        "attempts": 0,
        "next_try_at": NOW.replace(tzinfo=None),
        "last_status": 200,
        "version": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    async with _client(handler) as client:
        r = await wd.deliver_once(client=client, now=NOW, env={"PRIVATE_BOT_WEBHOOK_SECRET": "x"})
    assert r.skipped == 1 and r.total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_url", "bad_secret"),
    [
        ("https://bad.example.invalid:port/hook", SECRET),
        ("https://bad.example.invalid/hook", "whsec_abcde"),
    ],
    ids=["invalid-url", "undecodable-secret"],
)
async def test_one_broken_inbox_does_not_stop_the_next(
    fake: FakeRepo, bad_url: str, bad_secret: str
) -> None:
    # inbox 1 raises outside httpx.HTTPError (httpx.InvalidURL / binascii.Error);
    # inbox 2 must still be delivered and inbox 1's failure lands in its cursor.
    fake.inboxes[:] = [
        {**fake.inboxes[0], "webhook_url": bad_url, "webhook_secret_ref": "BAD_SECRET"},
        {
            "org": "acme",
            "inbox": "good-bot",
            "owner_namespace": "good",
            "webhook_url": "https://good.example.invalid/hook",
            "webhook_secret_ref": "PRIVATE_BOT_WEBHOOK_SECRET",
        },
    ]
    fake.events.append({**_event(103), "inbox": "good-bot"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(204)

    env = {"PRIVATE_BOT_WEBHOOK_SECRET": SECRET, "BAD_SECRET": bad_secret}
    async with _client(handler) as client:
        r = await wd.deliver_once(client=client, now=NOW, env=env)
    assert seen == ["https://good.example.invalid/hook"]
    assert r.delivered == 1 and r.failed == 1
    assert fake.cursors[("acme", "good-bot")]["last_seq"] == 103
    bad = fake.cursors[("acme", "private-bot")]
    assert bad["attempts"] == 1 and bad["last_seq"] == 0 and bad["last_status"] == 0
    assert bad["next_try_at"] == NOW.replace(tzinfo=None)


def test_worker_is_registered_beside_the_sweeper() -> None:
    from server.workers.registry import enabled_workers, worker_meta

    spec = {s.name: s for s in enabled_workers()}["task_webhook_deliver"]
    assert spec.interval_seconds == 5 and spec.idle_interval_seconds == 30
    assert spec.max_pass_seconds == 120.0
    assert worker_meta()["task_webhook_deliver"]["enabled"] is True


async def test_a_failure_row_that_cannot_be_written_does_not_stop_the_next(
    fake: FakeRepo,
) -> None:
    """The backoff row of a broken inbox is itself an insert; if it raised,
    every inbox after the broken one would wait for the next pass."""
    fake.inboxes[:] = [
        {**fake.inboxes[0], "webhook_url": "http://[::1", "webhook_secret_ref": "BAD"},
        {**fake.inboxes[0], "inbox": "good-bot", "owner_namespace": "good"},
    ]
    fake.events.append({**_event(103), "inbox": "good-bot"})
    real_insert = fake.insert_webhook_cursor

    async def insert(row: dict[str, Any]) -> None:
        if row["inbox"] == "private-bot":
            raise RuntimeError("insert timed out")
        await real_insert(row)

    fake.insert_webhook_cursor = insert  # type: ignore[method-assign]
    async with _client(lambda request: httpx.Response(204)) as client:
        r = await wd.deliver_once(
            client=client, now=NOW, env={"PRIVATE_BOT_WEBHOOK_SECRET": SECRET, "BAD": SECRET}
        )
    assert r.delivered == 1 and r.failed == 1
    assert fake.cursors[("acme", "good-bot")]["last_seq"] == 103


async def test_a_pass_posts_at_most_the_cap_and_the_rest_go_first_next_pass(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every POST may wait the ack window; uncapped, a pass over many busy
    inboxes would run past its deadline and be cancelled mid-delivery."""
    monkeypatch.setattr(wd, "MAX_POSTS_PER_PASS", 2)
    base = fake.inboxes[0]
    fake.inboxes[:] = [{**base, "inbox": f"bot-{i}"} for i in range(5)]
    fake.events[:] = [{**_event(100 + i), "inbox": f"bot-{i}"} for i in range(5)]
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.headers["webhook-id"])
        return httpx.Response(204)

    env = {"PRIVATE_BOT_WEBHOOK_SECRET": SECRET}
    async with _client(handler) as client:
        r1 = await wd.deliver_once(client=client, now=NOW, env=env)
        r2 = await wd.deliver_once(client=client, now=NOW + timedelta(seconds=5), env=env)
    assert (r1.delivered, r1.deferred) == (2, 3) and r1.total == 5
    # the three not reached come before the two just delivered
    assert posted == ["100", "101", "102", "103"] and r2.deferred == 3
