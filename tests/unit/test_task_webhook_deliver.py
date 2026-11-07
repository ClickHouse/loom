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

from server.core import webhooks
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

    async def events_since(self, org, *, since_seq, lag_s, inbox=None, limit=200, **_):
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
    monkeypatch.setattr(webhooks, "vet_host", _public)
    wd._warned_missing_secret.clear()
    fr.inboxes.append(
        {
            "org": "acme",
            "inbox": "private-bot",
            "owner_namespace": "private",
            "webhook_url": "https://bot.example.invalid/hook",
            "webhook_secret_ref": "LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT",
        }
    )
    fr.events.extend([_event(101), _event(102, "accepted")])
    return fr


PUBLIC = "93.184.215.14"


async def _public(host: str, **_: Any) -> tuple[str, str]:
    """Every test host resolves to one public address."""
    return "", PUBLIC


def _client(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ------------------------------------------------------------ signing


def test_signature_is_v1_base64_hmac_over_id_dot_timestamp_dot_body() -> None:
    body = b'{"a":1}'
    key = webhooks.secret_bytes(SECRET)
    assert key == b"0123456789abcdef0123456789abcdef"  # whsec_ marks a base64 key
    assert webhooks.secret_bytes("plain-secret") == b"plain-secret"
    sig = webhooks.sign(key, msg_id="102", timestamp=1_758_369_600, body=body)
    expected = base64.b64encode(
        hmac.new(key, b"102.1758369600." + body, hashlib.sha256).digest()
    ).decode()
    assert sig == f"v1,{expected}"
    headers = webhooks.signed_headers(
        key, msg_id="102", timestamp=1_758_369_600, body=body, user_agent="t"
    )
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
            transport=client, now=NOW, env={"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET}
        )
    assert result.delivered == 1 and result.failed == 0
    assert len(seen) == 1
    req = seen[0]
    # sent to the vetted address, the name kept as the Host header and TLS SNI
    assert str(req.url) == f"https://{PUBLIC}/hook"
    assert req.headers["host"] == "bot.example.invalid"
    assert req.extensions["sni_hostname"] == "bot.example.invalid"
    body = json.loads(req.content)
    assert body["since_seq"] == 0 and body["last_seq"] == 102 and body["count"] == 2
    assert req.headers["webhook-id"] == "102"
    ts = int(req.headers["webhook-timestamp"])
    assert req.headers["webhook-signature"] == webhooks.sign(
        webhooks.secret_bytes(SECRET), msg_id="102", timestamp=ts, body=req.content
    )
    cursor = fake.cursors[("acme", "private-bot")]
    assert cursor["last_seq"] == 102 and cursor["attempts"] == 0 and cursor["last_status"] == 204
    assert cursor["next_try_at"] == NOW.replace(tzinfo=None)
    # the secret is in no log line and in no cursor row
    assert SECRET not in caplog.text and "0123456789abcdef" not in caplog.text
    assert SECRET not in json.dumps(fake.writes, default=str)


@pytest.mark.asyncio
async def test_a_notice_carries_at_most_the_batch_limit(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "task_webhook_batch_limit", 1)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    async with _client(handler) as client:
        await wd.deliver_once(
            transport=client, now=NOW, env={"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET}
        )
    assert [(b["since_seq"], b["last_seq"], b["count"]) for b in bodies] == [(0, 101, 1)]


@pytest.mark.asyncio
async def test_a_5xx_keeps_the_cursor_and_backs_off(fake: FakeRepo) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    env = {"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET}
    async with _client(handler) as client:
        r1 = await wd.deliver_once(transport=client, now=NOW, env=env)
        cur = fake.cursors[("acme", "private-bot")]
        assert r1.failed == 1 and cur["attempts"] == 1 and cur["last_seq"] == 0
        assert cur["next_try_at"] == NOW.replace(tzinfo=None)  # immediate: next pass
        r2 = await wd.deliver_once(transport=client, now=NOW, env=env)
        cur = fake.cursors[("acme", "private-bot")]
        assert r2.failed == 1 and cur["attempts"] == 2
        assert cur["next_try_at"] == (NOW + timedelta(seconds=5)).replace(tzinfo=None)
        # not due yet: skipped without a request
        r3 = await wd.deliver_once(transport=client, now=NOW + timedelta(seconds=2), env=env)
        assert r3.skipped == 1 and r3.failed == 0
        r4 = await wd.deliver_once(transport=client, now=NOW + timedelta(seconds=6), env=env)
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
            transport=client, now=NOW, env={"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET}
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
        r1 = await wd.deliver_once(transport=client, now=NOW, env={})
        r2 = await wd.deliver_once(transport=client, now=NOW, env={})
    assert r1.skipped == 1 and r2.skipped == 1 and calls == 0 and not fake.writes
    assert sum("no usable secret in env" in rec.message for rec in caplog.records) == 1


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
        r = await wd.deliver_once(
            transport=client, now=NOW, env={"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": "x"}
        )
    assert r.skipped == 1 and r.total == 0


@pytest.mark.asyncio
async def test_a_secret_with_a_trailing_newline_signs_with_the_secret(fake: FakeRepo) -> None:
    """A secret read from a file into the variable ends in a newline: it is not
    part of the key."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    env = {"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET + "\n"}
    async with _client(handler) as client:
        r = await wd.deliver_once(transport=client, now=NOW, env=env)
    assert r.delivered == 1
    req = seen[0]
    assert req.headers["webhook-signature"] == webhooks.sign(
        webhooks.secret_bytes(SECRET),
        msg_id=req.headers["webhook-id"],
        timestamp=int(req.headers["webhook-timestamp"]),
        body=req.content,
    )


@pytest.mark.asyncio
async def test_a_secret_wrapped_across_two_lines_signs_with_the_secret(fake: FakeRepo) -> None:
    """A base64 tool wraps a long value at 64 characters: the line break is
    not part of the key."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    key = bytes(range(64))
    encoded = base64.b64encode(key).decode()
    wrapped = "whsec_" + encoded[:64] + "\n" + encoded[64:]
    env = {"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": wrapped}
    async with _client(handler) as client:
        r = await wd.deliver_once(transport=client, now=NOW, env=env)
    assert r.delivered == 1
    req = seen[0]
    assert req.headers["webhook-signature"] == webhooks.sign(
        key,
        msg_id=req.headers["webhook-id"],
        timestamp=int(req.headers["webhook-timestamp"]),
        body=req.content,
    )


@pytest.mark.asyncio
async def test_a_malformed_secret_is_warned_about_once(fake: FakeRepo, caplog) -> None:
    caplog.set_level(logging.WARNING)
    env = {"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": "whsec_not*base64!"}
    async with _client(lambda r: httpx.Response(204)) as client:
        await wd.deliver_once(transport=client, now=NOW, env=env)
        await wd.deliver_once(transport=client, now=NOW, env=env)
    assert sum("secret" in rec.message for rec in caplog.records) == 1


@pytest.mark.asyncio
async def test_an_undecodable_secret_holds_its_inbox_and_not_the_next(fake: FakeRepo) -> None:
    fake.inboxes[:] = [
        {**fake.inboxes[0], "webhook_secret_ref": "LOOM_WEBHOOK_SECRET_ACME__BAD"},
        {
            "org": "acme",
            "inbox": "good-bot",
            "owner_namespace": "good",
            "webhook_url": "https://good.example.invalid/hook",
            "webhook_secret_ref": "LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT",
        },
    ]
    fake.events.append({**_event(103), "inbox": "good-bot"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(204)

    env = {
        "LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET,
        "LOOM_WEBHOOK_SECRET_ACME__BAD": "whsec_abcde",
    }
    async with _client(handler) as client:
        r = await wd.deliver_once(transport=client, now=NOW, env=env)
    assert seen == ["good.example.invalid"]
    assert r.delivered == 1 and r.skipped == 1
    assert ("acme", "private-bot") not in fake.cursors


@pytest.mark.asyncio
async def test_one_broken_inbox_does_not_stop_the_next(fake: FakeRepo) -> None:
    # inbox 1 raises outside httpx.HTTPError (httpx.InvalidURL);
    # inbox 2 must still be delivered and inbox 1's failure lands in its cursor.
    bad_url, bad_secret = "https://bad.example.invalid:port/hook", SECRET
    fake.inboxes[:] = [
        {
            **fake.inboxes[0],
            "webhook_url": bad_url,
            "webhook_secret_ref": "LOOM_WEBHOOK_SECRET_ACME__BAD",
        },
        {
            "org": "acme",
            "inbox": "good-bot",
            "owner_namespace": "good",
            "webhook_url": "https://good.example.invalid/hook",
            "webhook_secret_ref": "LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT",
        },
    ]
    fake.events.append({**_event(103), "inbox": "good-bot"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(204)

    env = {
        "LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET,
        "LOOM_WEBHOOK_SECRET_ACME__BAD": bad_secret,
    }
    async with _client(handler) as client:
        r = await wd.deliver_once(transport=client, now=NOW, env=env)
    assert seen == ["good.example.invalid"]
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
        {
            **fake.inboxes[0],
            "webhook_url": "http://[::1",
            "webhook_secret_ref": "LOOM_WEBHOOK_SECRET_ACME__BAD",
        },
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
            transport=client,
            now=NOW,
            env={
                "LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET,
                "LOOM_WEBHOOK_SECRET_ACME__BAD": SECRET,
            },
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

    env = {"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET}
    async with _client(handler) as client:
        r1 = await wd.deliver_once(transport=client, now=NOW, env=env)
        r2 = await wd.deliver_once(transport=client, now=NOW + timedelta(seconds=5), env=env)
    assert (r1.delivered, r1.deferred) == (2, 3) and r1.total == 5
    # the three not reached come before the two just delivered
    assert posted == ["100", "101", "102", "103"] and r2.deferred == 3


@pytest.mark.asyncio
async def test_each_delivery_has_its_own_client(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connections pool by the pinned address: two inboxes on one address must
    not share a TLS connection verified for the other's name."""
    base = fake.inboxes[0]
    fake.inboxes[:] = [{**base, "inbox": f"bot-{i}"} for i in range(2)]
    fake.events[:] = [{**_event(100 + i), "inbox": f"bot-{i}"} for i in range(2)]
    clients: list[httpx.AsyncClient] = []

    class Recorded(httpx.AsyncClient):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            clients.append(self)

    monkeypatch.setattr(wd.httpx, "AsyncClient", Recorded)
    env = {"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET}
    r = await wd.deliver_once(
        transport=_client(lambda request: httpx.Response(204)), now=NOW, env=env
    )
    assert r.delivered == 2
    assert len(clients) == 2 and all(c.is_closed for c in clients)


# ------------------------------------------------------------ outbound guard


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "resolves_to"),
    [
        ("https://hook.example/x", "10.0.0.1"),
        ("https://hook.example/x", "127.0.0.1"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("https://hook.example/x", "::ffff:192.168.1.1"),
    ],
    ids=["private", "loopback", "metadata", "mapped-private"],
)
async def test_a_webhook_to_a_non_public_address_is_never_sent(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch, url: str, resolves_to: str
) -> None:
    import server.core.hosts as hosts

    monkeypatch.setattr(webhooks, "vet_host", hosts.vet_host)
    monkeypatch.setattr(hosts, "_resolve", lambda host: [resolves_to])
    fake.inboxes[0]["webhook_url"] = url
    seen: list[httpx.Request] = []
    async with _client(lambda r: seen.append(r) or httpx.Response(204)) as client:
        r = await wd.deliver_once(
            transport=client, now=NOW, env={"LOOM_WEBHOOK_SECRET_ACME__PRIVATEBOT": SECRET}
        )
    assert seen == [] and r.failed == 1
    assert fake.cursors[("acme", "private-bot")]["last_status"] == 0


@pytest.mark.asyncio
async def test_pinned_target_connects_to_the_vetted_address_only_over_http_or_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolver(host: str, **_: Any) -> tuple[str, str]:
        return "", "2606:2800:220:1::1"

    monkeypatch.setattr(webhooks, "vet_host", resolver)
    assert await webhooks.pinned_target("https://hook.example:8443/a?b=1") == (
        "",
        "https://[2606:2800:220:1::1]:8443/a?b=1",
        "hook.example:8443",
        "hook.example",
    )
    assert (await webhooks.pinned_target("ftp://hook.example/a"))[0]
    assert (await webhooks.pinned_target("file:///etc/passwd"))[0]


@pytest.mark.asyncio
async def test_an_internationalized_host_goes_as_its_idna_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Host header and the SNI are ASCII: httpx refuses a Unicode header value."""

    async def resolver(host: str, **_: Any) -> tuple[str, str]:
        return "", "203.0.113.7"

    monkeypatch.setattr(webhooks, "vet_host", resolver)
    assert await webhooks.pinned_target("https://bücher.example:8443/h") == (
        "",
        "https://203.0.113.7:8443/h",
        "xn--bcher-kva.example:8443",
        "xn--bcher-kva.example",
    )
    assert (await webhooks.pinned_target("https://a..example/h"))[0] == "the URL is not valid"


@pytest.mark.asyncio
async def test_a_secret_ref_outside_the_prefix_is_never_read(fake: FakeRepo) -> None:
    """A row naming any other variable (written before the prefix rule) would
    sign notices to a caller-chosen URL with a server secret as the key."""
    fake.inboxes[0]["webhook_secret_ref"] = "LOOM_JWT_SECRET"
    seen: list[httpx.Request] = []
    async with _client(lambda r: seen.append(r) or httpx.Response(204)) as client:
        r = await wd.deliver_once(transport=client, now=NOW, env={"LOOM_JWT_SECRET": SECRET})
    assert seen == [] and r.skipped == 1


@pytest.mark.asyncio
async def test_another_orgs_secret_is_never_read(fake: FakeRepo) -> None:
    """A tenant naming another tenant's secret would make Loom sign with it."""
    fake.inboxes[0]["webhook_secret_ref"] = "LOOM_WEBHOOK_SECRET_GLOBEX__BOT"
    seen: list[httpx.Request] = []
    env = {"LOOM_WEBHOOK_SECRET_GLOBEX__BOT": SECRET}
    async with _client(lambda r: seen.append(r) or httpx.Response(204)) as client:
        r = await wd.deliver_once(transport=client, now=NOW, env=env)
    assert seen == [] and r.skipped == 1


@pytest.mark.parametrize(
    ("org", "ref", "ok"),
    [
        ("acme", "LOOM_WEBHOOK_SECRET_ACME__BOT", True),
        ("acme-corp", "LOOM_WEBHOOK_SECRET_ACME_2DCORP__BOT", True),
        ("acme_corp", "LOOM_WEBHOOK_SECRET_ACME_5FCORP__BOT", True),
        ("Acme", "LOOM_WEBHOOK_SECRET__41CME__BOT", True),
        ("acme", "LOOM_WEBHOOK_SECRET_ACME_2DCORP__BOT", False),  # org acme-corp's
        ("acme-corp", "LOOM_WEBHOOK_SECRET_ACME_5FCORP__BOT", False),  # org acme_corp's
        ("acme", "LOOM_WEBHOOK_SECRET__41CME__BOT", False),  # org Acme's
    ],
)
def test_a_secret_ref_is_bound_to_one_org(org: str, ref: str, ok: bool) -> None:
    from server.core.webhook_secrets import webhook_secret_ref_ok

    assert webhook_secret_ref_ok(org, ref) is ok


def test_orgs_that_differ_only_in_punctuation_or_case_get_distinct_prefixes() -> None:
    from server.core.webhook_secrets import webhook_secret_org_prefix

    orgs = ["acme-x", "acme_x", "acme.x", "ACME-X", "acme_2dx", "acmex"]
    assert len({webhook_secret_org_prefix(o) for o in orgs}) == len(orgs)
    assert webhook_secret_org_prefix("acme-x") == "LOOM_WEBHOOK_SECRET_ACME_2DX__"
    assert webhook_secret_org_prefix("acme_x") == "LOOM_WEBHOOK_SECRET_ACME_5FX__"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("webhook_secret_ref", "LOOM_JWT_SECRET"),
        ("webhook_secret_ref", "LOOM_WEBHOOK_SECRET_"),
        ("webhook_secret_ref", "LOOM_WEBHOOK_SECRET_BOT"),
        ("webhook_secret_ref", "LOOM_WEBHOOK_SECRET_GLOBEX__BOT"),  # another org's
        ("webhook_secret_ref", "LOOM_WEBHOOK_SECRET_ACME__"),
        ("webhook_url", "file:///etc/passwd"),
        ("webhook_url", "gopher://hook.example/"),
    ],
)
def test_inbox_register_refuses_a_foreign_secret_ref_and_a_non_http_url(
    field: str, value: str
) -> None:
    from pydantic import ValidationError

    from server.tasks.api.models_task import InboxRegisterInput

    base = {"org": "acme", "namespace": "ns", "agent": "a", "inbox": "i", "owner_agent": "a"}
    with pytest.raises(ValidationError):
        InboxRegisterInput(**base, **{field: value})
    ok = InboxRegisterInput(
        **base,
        webhook_url="https://hook.example/x",
        webhook_secret_ref="LOOM_WEBHOOK_SECRET_ACME__BOT",
    )
    assert ok.webhook_secret_ref == "LOOM_WEBHOOK_SECRET_ACME__BOT"
