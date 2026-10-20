"""Memory webhooks: the body carries ids and never content, the write path's
outbox rows, the signed delivery and its retries, the URL rules, and that an
erasure leaves nothing a pass can send.

`loom.memory_webhooks`, `loom.memory_webhook_outbox` and
`loom.memory_webhook_attempts` are written whole through `WEBHOOK_COLS`,
`OUTBOX_COLS` and `ATTEMPT_COLS`; the first test holds them to the tables'
columns."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from server.core import webhooks as signed
from server.core.errors import ValidationError
from server.memory.api.models_crud import MemorySetInput
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
KEY = ("acme", "chat", "bot", "u1", "-", "user/pref/tea")


def test_the_writers_carry_every_column_of_the_three_tables() -> None:
    assert list(store.WEBHOOK_COLS) == column_names("memory_webhooks")
    assert list(store.OUTBOX_COLS) == column_names("memory_webhook_outbox")
    assert list(store.ATTEMPT_COLS) == column_names("memory_webhook_attempts")


def test_an_attempt_row_names_no_key_and_no_identity() -> None:
    assert set(store.ATTEMPT_COLS).isdisjoint(
        {"org", "namespace", "agent", "user_id", "session_id", "memory_key", "memory_id"}
    )


def _outbox_row(**over: Any) -> dict[str, Any]:
    change = dict(zip(store.KEY_COLS, KEY, strict=True))
    change.update(memory_id="11111111-1111-1111-1111-111111111111", version=1_758_369_600 * 10**9)
    row = app._outbox_row("memory.written", "wh1", change, actor="user:alice", now=NOW)
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
    assert body["version"] == 1_758_369_600 * 10**9
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
        **dict(zip(store.KEY_COLS, KEY, strict=True)),
        "session_id": "s1",
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
    assert (row["memory_key"], row["memory_id"], row["memory_version"]) == (KEY[5], "m", 7)
    assert "secret text" not in json.dumps(rows, default=str)


@pytest.mark.asyncio
async def test_a_namespace_without_webhooks_pays_no_insert(
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


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[dict[str, Any]]]]:
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def record(event: str, changes: Any) -> None:
        calls.append((event, [dict(c) for c in changes]))

    monkeypatch.setattr(app, "announce", record)
    return calls


def _patch_set_phases(monkeypatch: pytest.MonkeyPatch, *, existing: bool, embedded: bool) -> None:
    """memory.set's phases up to the insert, as no-ops that build a row."""
    from server.memory.app import write

    async def resolve(input: Any) -> Any:
        return input, input.memory_type, ""

    async def fetch(input: Any) -> Any:
        return None, (SimpleNamespace() if existing else None)

    async def expiry(*a: Any) -> None:
        return None

    async def prepare(w: Any, _restated: bool) -> None:
        return None

    async def check(w: Any) -> None:
        return None

    def build(w: Any) -> None:
        w.had_existing_row = existing
        w.memory_id = "m1"
        w.memory_row = {
            **dict(zip(store.KEY_COLS, KEY, strict=True)),
            "memory_id": "m1",
            "version": 42,
        }

    async def width(*a: Any) -> None:
        return None

    async def embed(w: Any) -> None:
        return None

    monkeypatch.setattr(write, "_set_resolve_type", resolve)
    monkeypatch.setattr(write, "_set_fetch_existing", fetch)
    monkeypatch.setattr(write, "_guard_existing_row", lambda *a: (False, None))
    monkeypatch.setattr(write, "_set_expiry", expiry)
    monkeypatch.setattr(write, "_set_prepare_value", prepare)
    monkeypatch.setattr(write, "_set_check_if_match", check)
    monkeypatch.setattr(write, "_set_build_memory_row", build)
    monkeypatch.setattr(write, "is_embedded", lambda *a: embedded)
    monkeypatch.setattr(write.codes_state, "refuse_width", width)
    monkeypatch.setattr(write, "get_embedder", lambda: SimpleNamespace(dim=8))
    monkeypatch.setattr(write, "_set_embed", embed)
    monkeypatch.setattr(write, "get_client", lambda: None)


def _set_input(**over: Any) -> MemorySetInput:
    return MemorySetInput(
        org="acme", namespace="chat", agent="bot", memory_key=KEY[5], value="v", **over
    )


def _out(*, dry_run: bool = False) -> Any:
    return SimpleNamespace(dry_run=dry_run)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing", "embedded", "event"),
    [
        (False, False, "memory.written"),
        (True, False, "memory.revised"),
        (False, True, "memory.written"),
        (True, True, "memory.revised"),
    ],
)
async def test_memory_set_announces_the_row_it_wrote(
    monkeypatch: pytest.MonkeyPatch,
    announced: list[Any],
    existing: bool,
    embedded: bool,
    event: str,
) -> None:
    from server.memory.app import write

    _patch_set_phases(monkeypatch, existing=existing, embedded=embedded)

    async def plain(w: Any, ch: Any) -> Any:
        return _out()

    async def verdict(w: Any, cfg: Any) -> Any:
        return None, SimpleNamespace(verdict="clear"), [], None

    async def dual(*a: Any) -> None:
        return None

    async def after(*a: Any) -> Any:
        return _out()

    monkeypatch.setattr(write, "_set_plain_insert", plain)
    monkeypatch.setattr(write, "_set_conflict_verdict", verdict)
    monkeypatch.setattr(write, "_stamp_lifecycle", lambda *a: "")
    monkeypatch.setattr(write, "_set_dual_write", dual)
    monkeypatch.setattr(write, "_set_after_write", after)
    await write._memory_set_phases(_set_input())
    assert [(e, [c["version"] for c in cs]) for e, cs in announced] == [(event, [42])]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["merge", "dry_run", "plain_dry_run", "pin_refusal"])
async def test_memory_set_announces_nothing_it_did_not_write(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any], path: str
) -> None:
    from server.memory.app import write

    _patch_set_phases(monkeypatch, existing=True, embedded=path != "plain_dry_run")
    verdict_name = "merge" if path == "merge" else "clear"

    async def plain(w: Any, ch: Any) -> Any:
        return _out(dry_run=True)

    async def verdict(w: Any, cfg: Any) -> Any:
        return None, SimpleNamespace(verdict=verdict_name), [], None

    async def merge(*a: Any) -> Any:
        return _out()

    async def must_not_write(*a: Any) -> None:
        raise AssertionError("no write on this path")

    monkeypatch.setattr(write, "_set_plain_insert", plain)
    monkeypatch.setattr(write, "_set_conflict_verdict", verdict)
    monkeypatch.setattr(write, "_set_merge", merge)
    monkeypatch.setattr(write, "_stamp_lifecycle", lambda *a: "")
    monkeypatch.setattr(write, "_set_dry_run_answer", lambda *a: _out(dry_run=True))
    monkeypatch.setattr(write, "_set_dual_write", must_not_write)
    if path == "pin_refusal":
        monkeypatch.setattr(write, "_guard_existing_row", lambda *a: (False, _out(dry_run=True)))
    await write._memory_set_phases(_set_input(dry_run=path.endswith("dry_run")))
    assert announced == []


@pytest.mark.asyncio
async def test_bulk_set_splits_written_and_revised(announced: list[Any]) -> None:
    from server.memory.app import bulk_set

    state = SimpleNamespace(
        existing_ids={"old": "m-old"},
        memory_rows=[
            {"memory_key": "new", "version": 1},
            {"memory_key": "old", "version": 2},
        ],
    )
    await bulk_set._bulk_announce(state)  # type: ignore[arg-type]
    assert [(e, [c["memory_key"] for c in cs]) for e, cs in announced] == [
        ("memory.written", ["new"]),
        ("memory.revised", ["old"]),
    ]


@pytest.mark.asyncio
async def test_a_finished_delete_version_is_announced_once_with_its_tombstone(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import handlers

    async def not_moved(*a: Any, **k: Any) -> None:
        return None

    async def retire(*a: Any, **k: Any) -> tuple[int, int]:
        return 0, 0

    async def record(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(handlers, "_key_moved_on", not_moved)
    monkeypatch.setattr(handlers, "_retire_derived", retire)
    monkeypatch.setattr(memory_repo, "record_delete_check", record)
    existing = SimpleNamespace(**dict(zip(store.KEY_COLS, KEY, strict=True)), memory_id="m1")
    out = await handlers.finish_delete_check(existing, {"checked": 5, "tombstone": 9})
    assert out.tombstoned is True
    assert [(e, cs[0]["memory_id"], cs[0]["version"]) for e, cs in announced] == [
        ("memory.tombstoned", "m1", 9)
    ]


@pytest.mark.asyncio
async def test_a_delete_version_whose_key_moved_on_is_not_announced(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemoryDeleteVersionOutput
    from server.memory.app import handlers

    async def moved(*a: Any, **k: Any) -> Any:
        return MemoryDeleteVersionOutput(tombstoned=False, reason="version_changed")

    async def record(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(handlers, "_key_moved_on", moved)
    monkeypatch.setattr(memory_repo, "record_delete_check", record)
    existing = SimpleNamespace(**dict(zip(store.KEY_COLS, KEY, strict=True)), memory_id="m1")
    await handlers.finish_delete_check(existing, {"checked": 5, "tombstone": 9})
    assert announced == []


# ------------------------------------------------------------ erasure


async def _run_saga(monkeypatch: pytest.MonkeyPatch, *, scope_type: str, value: str, fail: bool):
    import server.erasure.app.saga as saga
    from server.erasure.api.erasure import ErasureRequestInput

    calls: list[tuple[str, Any]] = []

    async def steps(*a: Any, **k: Any) -> list[Any]:
        if fail:
            raise RuntimeError("a backend refused")
        return []

    async def noop(*a: Any, **k: Any) -> None:
        return None

    async def erased(org: str, namespaces: Any, erasure_id: str) -> None:
        calls.append((org, namespaces))

    monkeypatch.setattr(saga, "_saga_user_id", steps)
    monkeypatch.setattr(saga, "_saga_namespace", steps)
    monkeypatch.setattr(saga, "checkpoint", noop)
    monkeypatch.setattr(saga, "_census_tail", noop)
    monkeypatch.setattr(saga.audit, "emit", noop)
    monkeypatch.setattr(app, "announce_erasure", erased)
    await saga.run_saga(
        input=ErasureRequestInput(
            org="acme", scope_type=scope_type, scope_value=value, requester="t"
        ),
        row={"id": "e1"},
        erasure_id="e1",
        requested_at=NOW,
        previously_completed=set(),
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_type", "value", "namespaces"),
    [
        ("namespace", "chat", ["chat"]),
        ("namespace_list", '{"namespaces": ["chat", "notes"], "purge": false}', ["chat", "notes"]),
        ("user_id", "u1", None),
    ],
)
async def test_a_completed_saga_announces_the_erasure_to_its_scope(
    monkeypatch: pytest.MonkeyPatch, scope_type: str, value: str, namespaces: Any
) -> None:
    calls = await _run_saga(monkeypatch, scope_type=scope_type, value=value, fail=False)
    assert calls == [("acme", namespaces)]


@pytest.mark.asyncio
async def test_a_failed_saga_announces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _run_saga(monkeypatch, scope_type="namespace", value="chat", fail=True) == []


# ------------------------------------------------------------ delivery


class FakeStore:
    """The three tables in memory, read with the statements' semantics."""

    KEY_COLS = store.KEY_COLS
    KEEP_DAYS = store.KEEP_DAYS

    def __init__(self, outbox: list[dict[str, Any]], hooks: list[dict[str, Any]]) -> None:
        self.outbox = outbox
        self.hooks = hooks
        self.attempts: list[dict[str, Any]] = []
        self.history: set[tuple[str, ...]] = {KEY}

    def latest(self, event_id: str) -> dict[str, Any] | None:
        rows = [a for a in self.attempts if a["event_id"] == event_id]
        return max(rows, key=lambda a: a["version"]) if rows else None

    async def due_events(self, now: datetime, *, since: datetime, limit: int, **_: Any) -> Any:
        due = []
        for o in sorted(self.outbox, key=lambda o: o["occurred_at"]):
            a = self.latest(o["event_id"])
            if a is None or (a["status"] in ("pending", "leased") and a["next_attempt_at"] <= now):
                due.append({**o, "attempts": a["attempts"] if a else 0})
        return due[:limit]

    async def live_webhooks(self) -> list[dict[str, Any]]:
        return [dict(h) for h in self.hooks]

    async def insert_attempts(self, rows: list[dict[str, Any]]) -> None:
        self.attempts.extend(dict(r) for r in rows)

    async def keys_with_history(self, keys: list[tuple[str, ...]]) -> set[tuple[str, ...]]:
        return {k for k in keys if k in self.history}

    def erase_user(self, user_id: str) -> None:
        """What the erasure saga does: the events and their attempts, the history."""
        gone = {o["event_id"] for o in self.outbox if o["user_id"] == user_id}
        self.outbox = [o for o in self.outbox if o["event_id"] not in gone]
        self.attempts = [a for a in self.attempts if a["event_id"] not in gone]
        self.history = {k for k in self.history if k[3] != user_id}

    def last(self) -> dict[str, Any]:
        return self.attempts[-1]


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
    # leased first, then the outcome; attempt rows carry only their own columns
    lease, out = fake.attempts
    assert lease["status"] == "leased" and lease["next_attempt_at"] > NOW
    assert out["status"] == "delivered" and out["last_status"] == 204 and out["attempts"] == 1
    assert all(set(a) == set(store.ATTEMPT_COLS) for a in fake.attempts)
    assert SECRET not in json.dumps(fake.attempts, default=str)


@pytest.mark.asyncio
async def test_a_5xx_is_retried_with_backoff_and_then_marked_dead(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await _pass(lambda r: httpx.Response(500))).failed == 1
    out = fake.last()
    assert out["status"] == "pending" and out["attempts"] == 1 and out["last_status"] == 500
    assert out["next_attempt_at"] == NOW + timedelta(seconds=5)
    # not due before its backoff
    assert (await _pass(lambda r: httpx.Response(204), now=NOW + timedelta(seconds=4))).total == 0
    monkeypatch.setattr(settings, "memory_webhook_max_attempts", 2)
    result = await _pass(lambda r: httpx.Response(502), now=NOW + timedelta(seconds=6))
    assert result.dead == 1 and fake.last()["status"] == "dead"
    assert (await _pass(lambda r: httpx.Response(204), now=NOW + timedelta(days=1))).total == 0


@pytest.mark.asyncio
async def test_an_event_waits_uncounted_while_its_secret_is_not_set(fake: FakeStore) -> None:
    seen: list[httpx.Request] = []
    for minute in range(20):  # far past max_attempts waits, none of them counted
        await _pass(
            lambda r: seen.append(r) or httpx.Response(204),
            now=NOW + timedelta(minutes=minute),
            env={},
        )
    assert seen == []
    out = fake.last()
    assert out["status"] == "pending" and out["attempts"] == 0
    assert out["last_error"] == f"no secret in env {REF}"
    result = await _pass(
        lambda r: seen.append(r) or httpx.Response(204), now=NOW + timedelta(hours=1)
    )
    assert result.delivered == 1 and fake.last()["attempts"] == 1


@pytest.mark.asyncio
async def test_an_event_of_a_removed_webhook_is_dead_without_a_send(fake: FakeStore) -> None:
    fake.hooks = []
    seen: list[httpx.Request] = []
    await _pass(lambda r: seen.append(r) or httpx.Response(204))
    assert seen == []
    assert fake.last()["status"] == "dead"
    assert fake.last()["last_error"] == "the webhook was removed"


@pytest.mark.asyncio
async def test_an_event_whose_memory_was_erased_is_never_sent(fake: FakeStore) -> None:
    """An older build's erasure leaves the outbox row but not the key's history."""
    fake.history = set()
    seen: list[httpx.Request] = []
    await _pass(lambda r: seen.append(r) or httpx.Response(204))
    assert seen == []
    assert fake.last()["status"] == "dead"
    assert fake.last()["last_error"] == "the memory was erased"


@pytest.mark.asyncio
async def test_an_erasure_event_names_no_key_and_is_sent(fake: FakeStore) -> None:
    fake.outbox = [
        {
            **_outbox_row(event_id="ev-e", event_type="memory.erased", erasure_id="er-1"),
            "memory_key": "",
            "user_id": "-",
        }
    ]
    fake.history = set()
    assert (await _pass(lambda r: httpx.Response(204))).delivered == 1


@pytest.mark.asyncio
async def test_another_orgs_secret_is_never_read(fake: FakeStore) -> None:
    fake.hooks = [{**_hook(), "secret_ref": "LOOM_WEBHOOK_SECRET_GLOBEX__CHAT"}]
    seen: list[httpx.Request] = []
    await _pass(
        lambda r: seen.append(r) or httpx.Response(204),
        env={"LOOM_WEBHOOK_SECRET_GLOBEX__CHAT": SECRET},
    )
    assert seen == []


@pytest.mark.asyncio
async def test_a_second_pass_overlapping_the_first_passes_over_its_leased_events(
    fake: FakeStore,
) -> None:
    second: list[Any] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        # The first pass is mid-POST: its lease is in, its outcome is not.
        second.append(await worker.deliver_once(now=NOW, env={REF: SECRET}))
        return httpx.Response(204)

    first = await _pass(handler)
    assert first.delivered == 1
    assert [r.total for r in second] == [0]


@pytest.mark.asyncio
async def test_an_erasure_between_a_pass_read_and_its_writes_leaves_nothing_to_send(
    fake: FakeStore,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        fake.erase_user("u1")  # the saga's step runs while the POST is out
        return httpx.Response(500)

    await _pass(handler)
    assert fake.outbox == []
    # The pass's outcome landed after the step; it names no key or user.
    assert [a["status"] for a in fake.attempts] == ["pending"]
    assert "u1" not in json.dumps(fake.attempts, default=str)
    assert KEY[5] not in json.dumps(fake.attempts, default=str)
    seen: list[httpx.Request] = []
    later = await _pass(
        lambda r: seen.append(r) or httpx.Response(204), now=NOW + timedelta(hours=1)
    )
    assert later.total == 0 and seen == []


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
async def test_register_refuses_a_secret_ref_of_another_org() -> None:
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
