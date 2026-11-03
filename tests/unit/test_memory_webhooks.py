"""Memory webhooks: the body carries ids and never content, the write paths'
events, the registry copy, the signed delivery and its retries, fairness
between webhooks, the URL rules, and that an erasure leaves nothing a pass can
send. The integration test that reads the outbox back is the main check of the
write paths; these cover the decisions and the failure cases.

`loom.memory_webhooks`, `loom.memory_webhook_outbox` and
`loom.memory_webhook_attempts` are written whole through `WEBHOOK_COLS`,
`OUTBOX_COLS` and `ATTEMPT_COLS`; the first test holds them to the tables'
columns."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

import server.memory.store as memory_repo
from server.core import webhooks as signed
from server.core.ch import sequential_reads_var
from server.core.errors import ValidationError
from server.memory.api.models_webhook import WebhookRegisterInput
from server.memory.app import webhooks as app
from server.memory.domain.webhook_events import (
    EVENT_TYPES,
    backoff_seconds,
    event_body,
    memory_change,
)
from server.memory.store import MemoryRow
from server.memory.store import webhooks as store
from server.memory.workers import webhook_deliver as worker
from server.observability.budget_shape import current_budget_shape, note_budget_shape
from server.settings import settings
from server.settings.catalog import parse_backoff_steps
from server.workers import pass_context
from tests.unit.ch_fake import RecordingCH
from tests.unit.clock_fake import ManualClock
from tests.unit.schema_ddl import column_names

NOW = datetime(2026, 9, 20, 12, 0)
REF = "LOOM_WEBHOOK_SECRET_ACME__CHAT"
SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
PUBLIC = "93.184.215.14"
KEY = ("acme", "chat", "bot", "u1", "-", "user/pref/tea")
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


def test_the_writers_carry_every_column_of_the_three_tables() -> None:
    assert list(store.WEBHOOK_COLS) == column_names("memory_webhooks")
    assert list(store.OUTBOX_COLS) == column_names("memory_webhook_outbox")
    assert list(store.ATTEMPT_COLS) == column_names("memory_webhook_attempts")


def test_an_attempt_row_names_no_key_and_no_identity() -> None:
    assert set(store.ATTEMPT_COLS).isdisjoint(
        {"org", "namespace", "agent", "user_id", "session_id", "memory_key", "memory_id"}
    )


def _change(**over: Any) -> dict[str, Any]:
    change: dict[str, Any] = dict(zip(store.KEY_COLS, KEY, strict=True))
    change.update(memory_id="11111111-1111-1111-1111-111111111111", version=1_758_369_600 * 10**9)
    return {**change, **over}


def _outbox_row(webhook_id: str = "wh1", event: str = "memory.written", **over: Any) -> Any:
    row = app._outbox_row(event, webhook_id, _change(), actor="user:alice", request_id="", now=NOW)
    return {**row, **over}


# ------------------------------------------------------------ the body and its id


def test_the_body_names_the_change_and_never_its_content() -> None:
    # A memory version row handed to announce carries its value, tags and evidence;
    # the outbox row and the body keep none of them.
    change = _change(value="prefers green tea", tags=["diet"], source_trace="said so in chat")
    row = app._outbox_row(
        "memory.written", "wh1", change, actor="user:alice", request_id="", now=NOW
    )
    body = json.loads(event_body(row))
    assert list(body) == BODY_KEYS
    assert body["type"] == "memory.written"
    assert body["memory_key"] == "user/pref/tea" and body["user_id"] == "u1"
    assert body["version"] == 1_758_369_600 * 10**9
    assert body["occurred_at"] == "2026-09-20T12:00:00.000Z"
    text = json.dumps(body)
    for content in ("green tea", "diet", "said so"):
        assert content not in text


def test_a_repeat_of_one_event_shares_its_id_and_nothing_else_does() -> None:
    same = app.event_id("wh1", "memory.tombstoned", _change())
    assert same == app.event_id("wh1", "memory.tombstoned", _change())
    assert (
        len(
            {
                same,
                app.event_id("wh2", "memory.tombstoned", _change()),
                app.event_id("wh1", "memory.revised", _change()),
                app.event_id("wh1", "memory.tombstoned", _change(version=1)),
                app.event_id("wh1", "memory.tombstoned", _change(memory_id="other")),
            }
        )
        == 5
    )
    erased = {"org": "acme", "namespace": "chat", "erasure_id": "er-1"}
    assert app.event_id("wh1", "memory.erased", erased) != app.event_id(
        "wh1", "memory.erased", {**erased, "erasure_id": "er-2"}
    )


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


# ------------------------------------------------------------ the registry copy


@pytest.fixture
def registry() -> dict[tuple[str, str], Any]:
    """This process's copy of the registry, fresh for the test."""
    hooks: dict[tuple[str, str], Any] = {}
    app._registry._reset_for_tests(hooks, read_age_s=0.0, due_in_s=float("inf"))
    return hooks


def _registration(webhook_id: str, namespace: str = "chat", **over: Any) -> dict[str, Any]:
    return {
        "org": "acme",
        "namespace": namespace,
        "webhook_id": webhook_id,
        "url": f"https://{namespace}.example/hooks/{webhook_id}",
        "secret_ref": REF,
        "events": list(EVENT_TYPES),
        "created_at": NOW,
        "is_deleted": 0,
        "version": 1,
        **over,
    }


def _unread_registry() -> None:
    app._registry._reset_for_tests({}, read_age_s=None, due_in_s=0.0)


@pytest.mark.asyncio
async def test_the_registry_copy_is_read_again_and_kept_when_a_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []
    answer: list[Any] = [[_registration("wh1")]]

    async def live() -> list[dict[str, Any]]:
        reads.append(1)
        if isinstance(answer[0], Exception):
            raise answer[0]
        return answer[0]

    monkeypatch.setattr(store, "live_webhooks", live)
    monkeypatch.setattr(settings, "memory_webhook_refresh_seconds", 30.0)
    _unread_registry()
    assert await app.subscribers("acme", "chat", "memory.written") == ["wh1"]
    # Within the interval the copy serves without a read.
    assert await app.subscribers("acme", "chat", "memory.written") == ["wh1"]
    assert len(reads) == 1
    # A failed read keeps the last copy.
    answer[0] = RuntimeError("ClickHouse is away")
    app.forget_registry()
    assert await app.subscribers("acme", "chat", "memory.written") == ["wh1"]
    assert len(reads) == 2
    # A change made here reads the registry again on the next write.
    answer[0] = []
    app.forget_registry()
    assert await app.subscribers("acme", "chat", "memory.written") == []
    assert await app.namespaces_with_hooks("acme") == []


@pytest.mark.asyncio
async def test_writes_on_a_cold_copy_wait_for_the_first_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two writes at once on a process that has not read the registry yet: both
    see the webhook, neither takes the empty copy for "none"."""
    reads: list[int] = []

    async def slow_live() -> list[dict[str, Any]]:
        reads.append(1)
        await asyncio.sleep(0.05)
        return [_registration("wh1")]

    monkeypatch.setattr(store, "live_webhooks", slow_live)
    _unread_registry()
    both = await asyncio.gather(
        app.subscribers("acme", "chat", "memory.written"),
        app.subscribers("acme", "chat", "memory.written"),
    )
    assert both == [["wh1"], ["wh1"]] and len(reads) == 1


@pytest.mark.asyncio
async def test_a_cold_read_that_fails_announces_nothing_until_a_read_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []
    notes: list[tuple[str, str]] = []
    answer: list[Any] = [RuntimeError("ClickHouse is away")]

    async def slow_live() -> list[dict[str, Any]]:
        reads.append(1)
        await asyncio.sleep(0.05)
        if isinstance(answer[0], Exception):
            raise answer[0]
        return answer[0]

    monkeypatch.setattr(store, "live_webhooks", slow_live)
    monkeypatch.setattr(settings, "memory_webhook_refresh_seconds", 30.0)
    monkeypatch.setattr(app.degradations, "note", lambda p, e, r="": notes.append((p, e)))
    _unread_registry()
    ch = RecordingCH().install(monkeypatch)
    # Two writes at once: the second takes the first's failed read, not a second one.
    # A write of many changes reads the copy, and notes the unread registry, once.
    await asyncio.gather(
        app.announce([("memory.written", _change())] * 3),
        app.announce([("memory.written", _change())]),
    )
    assert len(reads) == 1 and ch.inserted("memory_webhook_outbox") == []
    assert notes == [("memory.webhook", "registry_unread")] * 2
    # The failed read waits its interval like any other; a change made here reads at once.
    answer[0] = [_registration("wh1")]
    app.forget_registry()
    await app.announce([("memory.written", _change())])
    assert len(reads) == 2
    assert [r["webhook_id"] for r in ch.inserted("memory_webhook_outbox")] == ["wh1"]


@pytest.mark.asyncio
async def test_a_change_made_during_a_read_reads_the_registry_again_on_the_next_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A register here while a read is in flight: that read may predate it, so
    the next write reads again rather than waiting out the interval."""
    answers = [[], [_registration("wh1")]]

    async def live() -> list[dict[str, Any]]:
        answer = answers.pop(0)
        if answers:  # the first read: the register lands while it runs
            app.forget_registry()
        return answer

    monkeypatch.setattr(store, "live_webhooks", live)
    monkeypatch.setattr(settings, "memory_webhook_refresh_seconds", 30.0)
    _unread_registry()
    assert await app.subscribers("acme", "chat", "memory.written") == []
    assert await app.subscribers("acme", "chat", "memory.written") == ["wh1"]
    assert answers == []


@pytest.mark.asyncio
async def test_a_loaded_copy_serves_while_a_refresh_is_under_way() -> None:
    hooks = {("acme", "chat"): (("wh1", frozenset(EVENT_TYPES)),)}
    app._registry._reset_for_tests(hooks, read_age_s=0.0, due_in_s=0.0)  # due again
    async with app._registry._lock:  # another write is refreshing
        got = await asyncio.wait_for(app.subscribers("acme", "chat", "memory.written"), 1)
    assert got == ["wh1"]


# ------------------------------------------------------------ announcing


@pytest.mark.asyncio
async def test_announce_writes_one_row_per_subscribed_webhook(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    registry[("acme", "chat")] = (
        ("wh1", frozenset({"memory.written"})),
        ("wh2", frozenset({"memory.tombstoned"})),
    )
    ch = RecordingCH().install(monkeypatch)
    wrote = await app.announce([("memory.written", _change(session_id="s1", value="secret text"))])
    assert wrote is True
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
    assert (row["memory_key"], row["memory_version"]) == (KEY[5], 1_758_369_600 * 10**9)
    assert "secret text" not in json.dumps(rows, default=str)


@pytest.mark.asyncio
async def test_a_background_pass_is_the_actor_of_its_changes(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    registry[("acme", "chat")] = (("wh1", frozenset({"memory.tombstoned"})),)
    ch = RecordingCH().install(monkeypatch)
    pass_context.begin("memory_expiry_sweep")
    try:
        await app.announce([("memory.tombstoned", _change())])
    finally:
        pass_context.clear()
    assert ch.inserted("memory_webhook_outbox")[0]["actor"] == "worker:memory_expiry_sweep"


@pytest.mark.asyncio
async def test_a_pass_is_the_actor_of_a_row_another_principal_wrote(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    # A version row carries its writer's actor forward; the event names the pass.
    registry[("acme", "chat")] = (("wh1", frozenset({"memory.tombstoned"})),)
    ch = RecordingCH().install(monkeypatch)
    tombstone = memory_repo.build_memory_version_row(
        _memory_row("user/pref/tea", actor="user:alice"), version=20, tombstoned_at=NOW
    )
    assert tombstone["actor"] == "user:alice"
    pass_context.begin("memory_expiry_sweep")
    try:
        await app.announce([("memory.tombstoned", tombstone)])
    finally:
        pass_context.clear()
    assert ch.inserted("memory_webhook_outbox")[0]["actor"] == "worker:memory_expiry_sweep"


@pytest.mark.asyncio
async def test_a_namespace_without_webhooks_pays_no_insert(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    ch = RecordingCH().install(monkeypatch)
    assert await app.announce([("memory.written", _change())]) is False
    assert ch.calls == []


@pytest.mark.asyncio
async def test_a_failed_outbox_insert_never_fails_the_write(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    registry[("acme", "chat")] = (("wh1", frozenset({"memory.written"})),)

    async def broken(rows: Any) -> None:
        raise RuntimeError("ClickHouse is away")

    monkeypatch.setattr(store, "insert_outbox", broken)
    # a webhook took the change, so the call waited on its insert
    assert await app.announce([("memory.written", _change())]) is True


@pytest.mark.asyncio
async def test_a_failed_registry_read_sends_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> Any:
        raise RuntimeError("ClickHouse is away")

    monkeypatch.setattr(app, "_fresh_hooks", broken)
    assert await app.announce([("memory.written", _change())]) is False


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


@pytest.mark.asyncio
async def test_an_erased_event_names_no_principal(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    registry[("acme", "chat")] = (("wh1", frozenset({"memory.erased"})),)
    monkeypatch.setattr(app.provenance, "writer_actor", lambda *_: "human:dpo@acme.test")
    token = app.request_id_var.set("r-1")
    try:
        ch = RecordingCH().install(monkeypatch)
        await app.announce_erasure("acme", ["chat"], "er-1")
    finally:
        app.request_id_var.reset(token)
    (row,) = ch.inserted("memory_webhook_outbox")
    assert row["actor"] == "-" and row["request_id"] == ""


@pytest.mark.asyncio
async def test_a_receivers_token_cannot_turn_a_users_erasure_id_into_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user's erasure reaches every webhook of the org, so its id reaches
    receivers whose namespaces never held the user. Their flat tokens read
    nothing for it; the org admin who could request it reads the status."""
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.erasure.api.erasure import ErasureStatusInput
    from server.erasure.app import handlers as erasure_handlers
    from server.operations.base import Role

    saga = {
        "id": "er-1",
        "org": "acme",
        "scope_type": "user_id",
        "scope_value": "u-gone",
        "requester": "dpo@acme.test",
        "status": "completed",
        "current_step": 3,
        "requested_at": NOW,
        "completed_at": NOW,
        "failure_reason": "",
    }

    async def fetch(erasure_id: str) -> dict[str, Any] | None:
        return saga if erasure_id == "er-1" else None

    monkeypatch.setattr(erasure_handlers.erasure_ledger, "fetch_request_status", fetch)

    def flat(role: Role) -> AuthInfo:
        return AuthInfo(
            subject="notes-bot",
            org="acme",
            namespaces=("notes",),
            roles=(role,),
            is_service_account=True,
        )

    try:
        for role in (Role.VIEWER, Role.WRITER, Role.ADMIN):
            set_current_auth(flat(role))
            out = await erasure_handlers.handle_erasure_status(
                ErasureStatusInput(erasure_id="er-1")
            )
            assert out.found is False and out.scope_value is None, role
            assert "u-gone" not in out.model_dump_json() and "dpo@" not in out.model_dump_json()
        set_current_auth(flat(Role.ORG_ADMIN))
        out = await erasure_handlers.handle_erasure_status(ErasureStatusInput(erasure_id="er-1"))
        assert out.found is True and out.scope_value == "u-gone"
    finally:
        set_current_auth(None)


@pytest.fixture
def announced(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> list[list[tuple[str, dict[str, Any]]]]:
    """Each `announce` call with changes: its (event, change) pairs. A call with changes
    sent outbox rows unless the test parametrizes the fixture with False (no webhook
    took the events)."""
    calls: list[list[tuple[str, dict[str, Any]]]] = []
    sends = getattr(request, "param", True)

    async def record(changes: Any) -> bool:
        if changes:
            calls.append([(event, dict(c)) for event, c in changes])
        return sends and bool(changes)

    monkeypatch.setattr(app, "announce", record)
    return calls


def _events(announced: list[Any]) -> set[tuple[str, str]]:
    return {(e, c["memory_key"]) for call in announced for e, c in call}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing", "restated", "event"),
    [(False, False, "memory.written"), (True, False, "memory.revised"), (True, True, None)],
)
async def test_memory_set_announces_a_change_and_never_a_restatement(
    announced: list[Any], existing: bool, restated: bool, event: str | None
) -> None:
    from server.memory.app.set_commit import _announce_set

    # A row as memory.set builds it: a class, files and a source the live row also holds.
    source = {"source_class": "asserted", "files": ["src/a.py"], "source_resource_id": "doc:x"}
    live = _memory_row("k", tags=["tea"], **source) if existing else None
    row = {"memory_key": "k", "version": 42, "tags": ["tea"], "expires_at": None}
    row |= {"memory_type": "episodic", "confidence": 0.8, "valid_at": None, "valid_to": None}
    row |= source
    w = SimpleNamespace(had_existing_row=existing, restated=restated, memory_row=row, existing=live)
    await _announce_set(w)  # type: ignore[arg-type]
    assert announced == ([[(event, row)]] if event else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"tags": ["tea", "green"]},
        {"expires_at": datetime.now(UTC) + timedelta(seconds=60)},
        {"confidence": 0.5},
        {"valid_at": NOW},
        {"valid_to": NOW},
        {"memory_type": "semantic"},
        {"sensitivity": "high"},
        {"files": ["src/main.py"]},
        {"source_resource_id": "knowledge:docs/plan.md"},
        {"source_class": "derived"},
    ],
)
async def test_memory_set_of_the_same_value_with_new_fields_is_revised(
    announced: list[Any], change: dict[str, Any]
) -> None:
    from server.memory.app.set_commit import _announce_set

    row = {"memory_key": "k", "version": 42, "tags": ["tea"], "expires_at": None}
    row |= {"memory_type": "episodic", "confidence": 0.8, "valid_at": None, "valid_to": None}
    row |= change
    live = _memory_row("k", tags=["tea"])
    w = SimpleNamespace(had_existing_row=True, restated=True, memory_row=row, existing=live)
    await _announce_set(w)  # type: ignore[arg-type]
    assert announced == [[("memory.revised", row)]]


@pytest.mark.asyncio
async def test_memory_set_restating_a_rejected_row_is_revised(announced: list[Any]) -> None:
    from server.memory.app.set_commit import _announce_set

    # The restatement resets the status to active, so Loom serves the row again.
    live = _memory_row("k", tags=["tea"], status="rejected")
    row = {"memory_key": "k", "version": 42, "tags": ["tea"], "expires_at": None}
    row |= {"memory_type": "episodic", "confidence": 0.8, "valid_at": None, "valid_to": None}
    row |= {"status": "active"}
    w = SimpleNamespace(had_existing_row=True, restated=True, memory_row=row, existing=live)
    await _announce_set(w)  # type: ignore[arg-type]
    assert announced == [[("memory.revised", row)]]


@pytest.mark.asyncio
async def test_memory_set_restatement_compares_at_the_stored_precision(
    announced: list[Any],
) -> None:
    from server.memory.app.set_commit import _announce_set

    # The live row as ClickHouse reads it back: Float32 confidence, whole-second DateTime.
    live = _memory_row("k", tags=["tea"], confidence=0.800000011920929, valid_at=NOW)
    row = {"memory_key": "k", "version": 42, "memory_type": "episodic", "tags": ["tea"]}
    row |= {"expires_at": None, "confidence": 0.8, "valid_at": NOW.replace(microsecond=250000)}
    w = SimpleNamespace(had_existing_row=True, restated=True, memory_row=row, existing=live)
    await _announce_set(w)  # type: ignore[arg-type]
    assert announced == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("announced", "shape"), [(True, "webhook"), (False, "")], indirect=["announced"]
)
async def test_bulk_set_splits_written_and_revised_and_a_repeated_key_is_revised(
    announced: list[Any], shape: str
) -> None:
    from server.memory.app import bulk_set

    state = SimpleNamespace(
        existing_ids={"old": "m-old"},
        memory_rows=[
            {"memory_key": "new", "version": 1},
            {"memory_key": "old", "version": 2},
            {"memory_key": "new", "version": 3},
        ],
    )
    note_budget_shape("")
    await bulk_set._bulk_announce(state)  # type: ignore[arg-type]
    # held to the budget with the outbox-row wait only when outbox rows were sent
    assert current_budget_shape() == shape
    (call,) = announced  # one insert for the batch
    assert [(e, c["version"]) for e, c in call] == [
        ("memory.written", 1),
        ("memory.revised", 2),
        ("memory.revised", 3),
    ]


@pytest.mark.asyncio
async def test_bulk_set_announces_nothing_for_an_unchanged_batch(announced: list[Any]) -> None:
    from server.memory.app import bulk_set

    # Unchanged restatements write no row: the batch's memory_rows are empty.
    note_budget_shape("")
    await bulk_set._bulk_announce(SimpleNamespace(existing_ids={"k": "m"}, memory_rows=[]))  # type: ignore[arg-type]
    assert announced == [] and current_budget_shape() == ""


def _memory_row(key: str, version: int = 10, **over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "22222222-2222-2222-2222-222222222222",
        "org": "acme",
        "namespace": "chat",
        "agent": "bot",
        "user_id": "u1",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "episodic",
        "value": "v",
        "confidence": 0.8,
        "version": version,
        "pinned": False,
        "created_at": NOW - timedelta(days=1),
        "expires_at": None,
        "entities": [],
    }
    return MemoryRow(**(base | over))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("announced", "shape"), [(True, "webhook"), (False, "")], indirect=["announced"]
)
async def test_a_finished_delete_version_is_announced_with_its_tombstone(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any], shape: str
) -> None:
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
    note_budget_shape("")
    out = await handlers.finish_delete_check(_memory_row("k"), {"checked": 5, "tombstone": 9})
    assert out.tombstoned is True
    assert [(e, c["version"]) for call in announced for e, c in call] == [("memory.tombstoned", 9)]
    # held to the budget with the outbox-row wait only when outbox rows were sent
    assert current_budget_shape() == shape


@pytest.mark.asyncio
async def test_a_delete_version_whose_key_moved_on_is_not_announced(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    from server.memory.api.models_crud import MemoryDeleteVersionOutput
    from server.memory.app import handlers

    async def moved(*a: Any, **k: Any) -> Any:
        return MemoryDeleteVersionOutput(tombstoned=False, reason="version_changed")

    async def record(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(handlers, "_key_moved_on", moved)
    monkeypatch.setattr(memory_repo, "record_delete_check", record)
    note_budget_shape("")
    await handlers.finish_delete_check(_memory_row("k"), {"checked": 5, "tombstone": 9})
    assert announced == [] and current_budget_shape() == ""


@pytest.mark.asyncio
async def test_a_rollback_announces_revisions_revivals_and_tombstones(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    from server.memory.app import namespace_rollback as rb
    from server.memory.domain.rollback import RollbackAction, RollbackPlan

    historical = [
        _memory_row("changed", 10),
        _memory_row("revived", 11),
        _memory_row("was_gone", 12, tombstoned_at=NOW - timedelta(days=2)),
    ]

    async def fetch(*a: Any, **k: Any) -> list[MemoryRow]:
        return historical

    async def head(ident: Any, key: str, **k: Any) -> MemoryRow:
        return _memory_row(key, 30)

    async def noop(*a: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(memory_repo, "fetch_version_rows", fetch)
    monkeypatch.setattr(memory_repo, "get_by_key", head)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", noop)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", noop)
    monkeypatch.setattr(memory_repo, "fence_vectors", noop)
    monkeypatch.setattr(rb, "insert_memory_versions", noop)

    def act(key: str, action: str, to: int, dead_head: bool = False) -> RollbackAction:
        return RollbackAction("bot", "u1", "-", key, action, 20, to, dead_head)

    plan = RollbackPlan(
        reinstate=[
            act("changed", "reinstate", 10),
            act("revived", "reinstate", 11, dead_head=True),
            act("was_gone", "reinstate", 12),
        ],
        remove=[act("born_later", "remove", 0)],
    )
    await rb.apply_rollback("acme", "chat", plan, applied_at=NOW, ch=object())
    assert len(announced) == 1  # one insert for the rollback
    assert _events(announced) == {
        ("memory.revised", "changed"),
        ("memory.restored", "revived"),
        ("memory.tombstoned", "was_gone"),
        ("memory.tombstoned", "born_later"),
    }


@pytest.mark.asyncio
async def test_a_rollback_names_its_caller_not_the_historical_writer(
    monkeypatch: pytest.MonkeyPatch, registry: dict[tuple[str, str], Any]
) -> None:
    from server.memory.app import namespace_rollback as rb
    from server.memory.domain.rollback import RollbackAction, RollbackPlan

    registry[("acme", "chat")] = (("wh1", frozenset({"memory.revised"})),)
    historical = [_memory_row("changed", 10, actor="user:alice")]

    async def fetch(*a: Any, **k: Any) -> list[MemoryRow]:
        return historical

    async def head(ident: Any, key: str, **k: Any) -> MemoryRow:
        return _memory_row(key, 30, actor="user:alice")

    async def noop(*a: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(memory_repo, "fetch_version_rows", fetch)
    monkeypatch.setattr(memory_repo, "get_by_key", head)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", noop)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", noop)
    monkeypatch.setattr(memory_repo, "fence_vectors", noop)
    monkeypatch.setattr(rb, "insert_memory_versions", noop)
    monkeypatch.setattr(app.provenance, "writer_actor", lambda *_: "user:bob")
    ch = RecordingCH().install(monkeypatch)
    plan = RollbackPlan(
        reinstate=[RollbackAction("bot", "u1", "-", "changed", "reinstate", 20, 10, False)],
        remove=[],
    )
    await rb.apply_rollback("acme", "chat", plan, applied_at=NOW, ch=object())
    (row,) = ch.inserted("memory_webhook_outbox")
    assert (row["event_type"], row["actor"]) == ("memory.revised", "user:bob")


@pytest.mark.asyncio
async def test_a_blacklist_sweep_announces_the_rows_it_tombstones(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    from server.memory.app import blacklist_sweep
    from server.memory.store import lifecycle

    rows = {
        "m-a": _memory_row("user/phone", memory_id="m-a"),
        "m-b": _memory_row("user/contact", memory_id="m-b"),
    }
    written: list[dict[str, Any]] = []

    class _Matches:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [{"memory_id": mid, "namespace": "chat"} for mid in rows]

    async def get(memory_id: str, **k: Any) -> MemoryRow:
        return rows[memory_id]

    async def insert(built: list[dict[str, Any]], **k: Any) -> None:
        written.extend(built)

    async def mirror(*a: Any, **k: Any) -> int:
        return 0

    # The real cascade, so the events carry the version it wrote.
    monkeypatch.setattr(lifecycle, "get_by_id", get)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", mirror)
    result = await blacklist_sweep.sweep_pattern(
        org="acme",
        namespace="chat",
        pattern_id="p1",
        pattern="555-0100",
        pattern_type="substring",
        scope="namespace",
        ch=_Matches(),
    )
    assert result.tombstoned == 2
    (call,) = announced  # one insert for the sweep
    assert {(e, c["memory_key"], c["version"]) for e, c in call} == {
        ("memory.tombstoned", r["memory_key"], r["version"]) for r in written
    }
    assert {r["memory_key"] for r in written} == {"user/phone", "user/contact"}


@pytest.mark.asyncio
async def test_a_sweep_cut_short_announces_the_rows_it_relabelled_in_one_insert(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    from server.memory.app import sensitivity_sweep
    from server.memory.store.sensitivity_sweep_sql import SORT_KEY

    def candidate(key: str) -> sensitivity_sweep.SweepCandidate:
        ident = ("acme", "chat", "bot", "u1", "-", key)
        return sensitivity_sweep.SweepCandidate(
            f"m-{key}",
            *ident,
            level="medium",
            shapes=("email",),
            sort_key=dict(zip(SORT_KEY, ident, strict=True)),
        )

    async def write(org: str, ids: list[str], update: Any, **k: Any) -> int:
        if ids == ["m-second"]:
            raise asyncio.CancelledError  # the pass deadline
        return 1

    async def tag(c: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", write)
    monkeypatch.setattr(sensitivity_sweep, "_tag_embeddings", tag)
    with pytest.raises(asyncio.CancelledError):
        await sensitivity_sweep.apply(
            [candidate("zero"), candidate("first"), candidate("second")], ch=object()
        )
    (call,) = announced
    assert {(e, c["memory_key"]) for e, c in call} == {
        ("memory.revised", "zero"),
        ("memory.revised", "first"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "tombstoned"),
    [("keep_canonical", "dup/key"), ("keep_duplicate", "canon/key"), ("keep_both", None)],
)
async def test_dedup_resolve_announces_the_side_it_tombstones(
    monkeypatch: pytest.MonkeyPatch,
    announced: list[Any],
    decision: str,
    tombstoned: str | None,
) -> None:
    from server.memory.api.models_graph import MemoryDedupResolveInput
    from server.memory.app import dedup

    canon_id, dup_id = (
        "11111111-1111-1111-1111-111111111111",
        "33333333-3333-3333-3333-333333333333",
    )
    rows = {
        canon_id: _memory_row("canon/key", memory_id=canon_id),
        dup_id: _memory_row("dup/key", memory_id=dup_id, status="duplicate", duplicate_of=canon_id),
    }

    async def get(memory_id: str, **k: Any) -> MemoryRow:
        return rows[memory_id]

    async def tombstone(org: str, memory_id: str, **k: Any) -> list[dict[str, Any]]:
        return [memory_change(rows[memory_id], 42)]

    async def clear(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(memory_repo, "get_by_id", get)
    monkeypatch.setattr(memory_repo, "tombstone_by_id", tombstone)
    monkeypatch.setattr(memory_repo, "clear_duplicate_tag", clear)
    await dedup.handle_memory_dedup_resolve(
        MemoryDedupResolveInput(
            org="acme",
            namespace="chat",
            canonical_id=canon_id,
            duplicate_id=dup_id,
            decision=decision,  # type: ignore[arg-type]
            confirm_canonical_key="canon/key",
        )
    )
    if tombstoned is None:
        assert announced == []
        return
    # The event carries the version the tombstone was written at.
    assert [[(e, c["memory_key"], c["version"]) for e, c in call] for call in announced] == [
        [("memory.tombstoned", tombstoned, 42)]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["demote", "expire"])
async def test_run_retention_announces_the_rows_it_revises(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any], mode: str
) -> None:
    from server.memory.app import apply as memory_apply
    from server.memory.app import run_retention

    live = {"m-a": _memory_row("run/a"), "m-b": _memory_row("run/b")}
    written: list[dict[str, Any]] = []

    async def load(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"memory_id": mid, "memory_key": r.memory_key} for mid, r in live.items()]

    async def no_promoted(*a: Any, **k: Any) -> set[str]:
        return set()

    async def write(org: str, ids: list[str], update: Any, **k: Any) -> int:
        written.extend(update(live[mid]) for mid in ids)
        return len(ids)

    async def mirror(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(run_retention, "load_run_rows", load)
    monkeypatch.setattr(run_retention, "promoted_keys_of", no_promoted)
    monkeypatch.setattr(memory_repo, "write_versions_by_ids", write)
    monkeypatch.setattr(memory_apply, "_mirror", mirror)
    await run_retention.apply(
        org="acme",
        namespace="chat",
        run_id="r1",
        mode=mode,
        ch=object(),
        now=NOW,
        demote_factor=0.5,
        demote_floor=0.1,
        expire_days=7,
    )
    (call,) = announced  # one insert for the run
    assert {(e, c["memory_key"]) for e, c in call} == {
        ("memory.revised", "run/a"),
        ("memory.revised", "run/b"),
    }
    # Each event names the version its row was written at.
    assert {c["version"] for _, c in call} == {w["version"] for w in written}


@pytest.mark.asyncio
async def test_a_move_that_keeps_a_redirect_announces_the_source_revised(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    from server.memory.app import move

    async def noop(*a: Any, **k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(memory_repo, "insert_memory_versions", noop)
    monkeypatch.setattr(memory_repo, "embedding_mirror_for_keys", noop)
    monkeypatch.setattr(memory_repo, "restamp_embedding_rows", noop)
    await move._redirect_source(_memory_row("old/key"), "dest-id", ch=None)
    assert _events(announced) == {("memory.revised", "old/key")}


@pytest.mark.asyncio
async def test_the_expiry_sweep_announces_its_tombstones_and_demotions(
    monkeypatch: pytest.MonkeyPatch, announced: list[Any]
) -> None:
    from server.memory.app import expiry

    expired = NOW - timedelta(days=1)
    rows = [
        _memory_row("gone", on_expire="tombstone", expires_at=expired),
        _memory_row("faded", on_expire="demote", expires_at=expired),
    ]

    async def find(**k: Any) -> list[MemoryRow]:
        return rows

    async def noop(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(memory_repo, "find_expired_candidates", find)
    monkeypatch.setattr(expiry, "insert_memory_versions", noop)
    monkeypatch.setattr(expiry, "_mirror_embeddings_batch", noop)
    await expiry.run_pass()
    assert len(announced) == 1  # one insert for the sweep's batch
    assert _events(announced) == {("memory.tombstoned", "gone"), ("memory.revised", "faded")}


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

    async def checkpoint(*a: Any, status: str, **k: Any) -> None:
        if status == "completed":
            # A crash at the checkpoint finds the erasure already announced.
            assert calls, "announced after the completed checkpoint"
        calls.append(("checkpoint", status))

    monkeypatch.setattr(saga, "_saga_user_id", steps)
    monkeypatch.setattr(saga, "_saga_namespace", steps)
    monkeypatch.setattr(saga, "checkpoint", checkpoint)
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
    # Before the checkpoint: a crash in between reopens the saga, which announces again.
    assert calls == [("acme", namespaces), ("checkpoint", "completed")]


@pytest.mark.asyncio
async def test_a_failed_saga_announces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = await _run_saga(monkeypatch, scope_type="namespace", value="chat", fail=True)
    assert calls == [("checkpoint", "failed")]


# ------------------------------------------------------------ delivery


class FakeStore:
    """The three tables in memory, read with the statements' semantics."""

    KEY_COLS = store.KEY_COLS
    KEEP_DAYS = store.KEEP_DAYS

    def __init__(self, outbox: list[dict[str, Any]], hooks: list[dict[str, Any]]) -> None:
        self.outbox = outbox
        self.hooks = hooks
        self.removed: list[tuple[str, str, str]] = []
        self.removed_at = NOW
        self.attempts: list[dict[str, Any]] = []
        self.history: set[tuple[str, ...]] = {KEY}

    def latest(self, event_id: str) -> dict[str, Any] | None:
        rows = [a for a in self.attempts if a["event_id"] == event_id]
        return max(rows, key=lambda a: a["version"]) if rows else None

    def _of(self, hooks: list[Any]) -> list[dict[str, Any]]:
        """The events of `hooks`, oldest first; of an event announced twice, the earliest."""
        wanted = set(hooks)
        events: dict[str, dict[str, Any]] = {}
        for o in sorted(self.outbox, key=lambda o: o["occurred_at"]):
            if (o["org"], o["namespace"], o["webhook_id"]) in wanted:
                events.setdefault(o["event_id"], o)
        return list(events.values())

    async def due_events(
        self, now: datetime, *, since: datetime, hooks: list[Any], per_webhook: int, limit: int
    ) -> list[dict[str, Any]]:
        turns: dict[str, int] = {}
        due = []
        for o in self._of(hooks):
            a = self.latest(o["event_id"])
            if a is None or (a["status"] in ("pending", "leased") and a["next_attempt_at"] <= now):
                turns[o["webhook_id"]] = turns.get(o["webhook_id"], 0) + 1
                if turns[o["webhook_id"]] <= per_webhook:
                    tried = (
                        {k: a[k] for k in ("attempts", "last_status", "last_error")} if a else {}
                    )
                    untried = {"attempts": 0, "last_status": 0, "last_error": ""}
                    due.append((turns[o["webhook_id"]], {**o, **untried, **tried}))
        due.sort(key=lambda t: (t[0], t[1]["occurred_at"]))
        return [row for _turn, row in due][:limit]

    async def mark_dead(
        self, now: datetime, *, since: Any, hooks: list[Any], reason: str, version: int, before=None
    ) -> None:
        for o in self._of(hooks):
            a = self.latest(o["event_id"])
            if (before is None or o["occurred_at"] < before) and (
                a is None or a["status"] in ("pending", "leased")
            ):
                self.attempts.append(
                    worker._attempt_row(
                        o["event_id"],
                        status="dead",
                        attempts=a["attempts"] if a else 0,
                        last_status=a["last_status"] if a else 0,
                        last_error=reason,
                    )
                )

    async def live_webhooks(self) -> list[dict[str, Any]]:
        return [dict(h) for h in self.hooks]

    async def removed_webhooks(self, since: datetime) -> list[tuple[str, str, str]]:
        return list(self.removed) if self.removed_at >= since else []

    async def webhook_removed_since(self, hook: tuple[str, str, str], since: datetime) -> bool:
        return hook in await self.removed_webhooks(since)

    async def insert_attempts(self, rows: list[dict[str, Any]]) -> None:
        self.attempts.extend(dict(r) for r in rows)

    async def keys_with_history(self, keys: list[tuple[str, ...]]) -> set[tuple[str, ...]]:
        return {k for k in keys if k in self.history}

    def erase_user(self, user_id: str) -> None:
        """What the erasure saga does: the user's events and history go; attempt
        rows name no user and stay to expire."""
        self.outbox = [o for o in self.outbox if o["user_id"] != user_id]
        self.history = {k for k in self.history if k[3] != user_id}

    def last(self, event_id: str = "ev-1") -> dict[str, Any]:
        latest = self.latest(event_id)
        assert latest is not None
        return latest


async def _public(host: str, **_: Any) -> tuple[str, str]:
    return "", PUBLIC


# The worker's clock (`naive_utc_now`) in these tests: set by `_pass`, moved by a handler.
CLOCK = ManualClock()


def _at(when: datetime) -> float:
    return when.replace(tzinfo=UTC).timestamp()


def _clock_now() -> datetime:
    return datetime.fromtimestamp(CLOCK(), UTC).replace(tzinfo=None)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fs = FakeStore([_outbox_row(event_id="ev-1")], [_registration("wh1")])
    monkeypatch.setattr(worker, "store", fs)
    monkeypatch.setattr(worker, "_health", {})
    monkeypatch.setattr(worker, "_warned_missing_secret", set())
    monkeypatch.setattr(signed, "vet_host", _public)
    CLOCK.t = _at(NOW)
    monkeypatch.setattr(worker, "naive_utc_now", _clock_now)
    return fs


async def _pass(handler: Any, *, now: datetime = NOW, env: dict[str, str] | None = None) -> Any:
    CLOCK.t = _at(now)
    async with httpx.MockTransport(handler) as transport:
        return await worker.deliver_once(
            transport=transport, env={REF: SECRET} if env is None else env
        )


@pytest.mark.asyncio
async def test_a_delivered_event_is_signed_over_its_timestamp_and_body(fake: FakeStore) -> None:
    seen: list[httpx.Request] = []
    result = await _pass(lambda r: seen.append(r) or httpx.Response(204))
    assert result.delivered == 1
    req = seen[0]
    assert str(req.url) == f"https://{PUBLIC}/hooks/wh1"
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
async def test_an_event_waits_while_its_secret_is_not_set_and_dies_before_the_ttl(
    fake: FakeStore,
) -> None:
    seen: list[httpx.Request] = []
    for minute in range(20):  # far past max_attempts passes; none of them counts
        result = await _pass(
            lambda r: seen.append(r) or httpx.Response(204),
            now=NOW + timedelta(minutes=minute),
            env={},
        )
        assert result.waiting == 1 and result.total == 0
    assert seen == [] and fake.attempts == []
    # The secret arrives: the event goes out on its first attempt.
    assert (await _pass(lambda r: seen.append(r) or httpx.Response(204))).delivered == 1
    assert fake.last()["attempts"] == 1
    # A second event whose secret never comes is dead before the outbox drops it.
    fake.outbox.append(_outbox_row(event_id="ev-2"))
    await _pass(lambda r: httpx.Response(204), now=NOW + timedelta(days=2), env={})
    assert fake.latest("ev-2") is None
    await _pass(lambda r: httpx.Response(204), now=NOW + timedelta(days=2, hours=13), env={})
    assert fake.last("ev-2")["status"] == "dead"
    assert fake.last("ev-2")["last_error"] == "no usable secret"


@pytest.mark.asyncio
async def test_an_unset_secret_is_warned_about_once(
    fake: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    await _pass(lambda r: httpx.Response(204), env={})
    await _pass(lambda r: httpx.Response(204), env={})
    warnings = [r.getMessage() for r in caplog.records if "no usable secret" in r.getMessage()]
    assert len(warnings) == 1
    assert REF in warnings[0] and "LOOM_WEBHOOK_SECRET_ACME__" in warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "whsec_not*base64!",
        "whsec_abc\u2026",  # a typographic ellipsis pasted into the value
        "abc\udcff",  # an environment byte that is not UTF-8
    ],
)
async def test_a_malformed_secret_is_not_usable_and_is_not_logged(
    fake: FakeStore, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    caplog.set_level(logging.WARNING)
    assert signed.read_secret("acme", REF, {REF: bad}) is None
    # A trailing newline (a secret file read into the variable) is not part of the key.
    assert signed.read_secret("acme", REF, {REF: SECRET + "\n"}) == base64.b64decode(
        SECRET.removeprefix("whsec_")
    )
    result = await _pass(lambda r: httpx.Response(204), env={REF: bad})
    assert result.waiting == 1 and result.total == 0
    assert REF in caplog.text and bad not in caplog.text


@pytest.mark.parametrize("value", ["whsec_", "whsec_\n", "whsec_ \n "])
def test_an_empty_whsec_secret_is_not_usable(value: str) -> None:
    # An empty HMAC key signs what anyone can sign: the events wait instead.
    assert signed.read_secret("acme", REF, {REF: value}) is None


@pytest.mark.asyncio
async def test_events_of_a_removed_webhook_are_dead_in_one_batch_without_a_send(
    fake: FakeStore,
) -> None:
    fake.outbox += [_outbox_row(event_id=f"ev-{i}") for i in range(2, 30)]
    fake.hooks = []
    fake.removed = [("acme", "chat", "wh1")]
    seen: list[httpx.Request] = []
    await _pass(lambda r: seen.append(r) or httpx.Response(204))
    assert seen == []
    assert {fake.last(o["event_id"])["status"] for o in fake.outbox} == {"dead"}
    assert fake.last()["last_error"] == "the webhook was removed"


@pytest.mark.asyncio
async def test_a_webhook_removed_during_a_pass_gets_no_further_post(fake: FakeStore) -> None:
    fake.outbox += [_outbox_row(event_id=f"ev-{i}") for i in range(2, 5)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        fake.removed = [("acme", "chat", "wh1")]  # webhook.remove while the turn is out
        fake.removed_at = NOW + timedelta(seconds=1)
        return httpx.Response(204)

    result = await _pass(handler)
    assert len(seen) == 1 and result.delivered == 1 and result.dead == 3
    assert fake.last("ev-4")["last_error"] == "the webhook was removed"


@pytest.mark.asyncio
async def test_a_webhook_removed_before_the_window_costs_no_statement(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.hooks = []
    fake.removed = [("acme", "chat", "wh1")]
    marked: list[list[Any]] = []

    async def mark_dead(now: datetime, *, hooks: list[Any], **_: Any) -> None:
        marked.append(hooks)

    monkeypatch.setattr(fake, "mark_dead", mark_dead)
    late = NOW + worker._removal_window() + timedelta(seconds=1)
    await _pass(lambda r: httpx.Response(204), now=late)
    assert ("acme", "chat", "wh1") not in [h for hooks in marked for h in hooks]
    await _pass(lambda r: httpx.Response(204), now=NOW + worker._removal_window())
    assert [("acme", "chat", "wh1")] in marked


@pytest.mark.asyncio
async def test_an_event_whose_memory_was_erased_is_never_sent(fake: FakeStore) -> None:
    """An older build's erasure leaves the outbox row but not the key's history."""
    await _pass(lambda r: httpx.Response(503))
    fake.history = set()
    seen: list[httpx.Request] = []
    await _pass(lambda r: seen.append(r) or httpx.Response(204), now=NOW + timedelta(seconds=6))
    assert seen == []
    dead = fake.last()
    assert dead["status"] == "dead" and dead["last_error"] == "the memory was erased"
    # The receiver's answer to the attempts before the erasure stays.
    assert dead["attempts"] == 1 and dead["last_status"] == 503


@pytest.mark.asyncio
async def test_an_erasure_event_names_no_key_and_is_sent(fake: FakeStore) -> None:
    fake.outbox = [
        _outbox_row(
            event_id="ev-e", event="memory.erased", erasure_id="er-1", memory_key="", user_id="-"
        )
    ]
    fake.history = set()
    assert (await _pass(lambda r: httpx.Response(204))).delivered == 1


@pytest.mark.asyncio
async def test_another_orgs_secret_is_never_read(fake: FakeStore) -> None:
    fake.hooks = [_registration("wh1", secret_ref="LOOM_WEBHOOK_SECRET_GLOBEX__CHAT")]
    seen: list[httpx.Request] = []
    await _pass(
        lambda r: seen.append(r) or httpx.Response(204),
        env={"LOOM_WEBHOOK_SECRET_GLOBEX__CHAT": SECRET},
    )
    assert seen == []


@pytest.mark.asyncio
async def test_one_webhooks_backlog_never_delays_another_namespaces_event(
    fake: FakeStore,
) -> None:
    """Three stuck webhooks with deep backlogs (no secret, removed, not
    answering) and one healthy one: the healthy one's event goes out in the
    first pass, and the unanswering webhook cools down after five failures."""
    stuck = {"wh-nosecret": "LOOM_WEBHOOK_SECRET_ACME__NONE", "wh-down": REF}
    fake.hooks = [_registration(w, namespace="busy", secret_ref=r) for w, r in stuck.items()]
    fake.hooks.append(_registration("wh-ok", namespace="quiet"))
    fake.removed = [("acme", "busy", "wh-removed")]
    fake.outbox = [
        _outbox_row(webhook_id=w, event_id=f"{w}-{i}", namespace="busy")
        for w in ("wh-nosecret", "wh-removed", "wh-down")
        for i in range(100)
    ]
    fake.outbox.append(
        _outbox_row(webhook_id="wh-ok", event_id="ok-1", namespace="quiet", occurred_at=NOW)
    )
    fake.history = {(*KEY[:1], ns, *KEY[2:]) for ns in ("busy", "quiet")}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204 if request.headers["host"] == "quiet.example" else 503)

    first = await _pass(handler)
    assert fake.last("ok-1")["status"] == "delivered"
    # wh-down failed five POSTs in a row: its other events wait out the cooldown.
    assert first.delivered == 1 and first.failed == settings.memory_webhook_failures_before_cooldown
    second = await _pass(handler, now=NOW + timedelta(seconds=10))
    assert second.total == 0 and second.waiting == 2


@pytest.mark.asyncio
async def test_turns_go_round_every_webhook_and_a_slow_one_holds_one_slot(
    fake: FakeStore,
) -> None:
    """Five healthy webhooks with deep backlogs and one slow receiver: the fresh
    event of another org goes out in the first pass, and the slow receiver never
    has more than one POST in flight."""
    names = [f"wh-{i}" for i in range(5)] + ["wh-slow"]
    fake.hooks = [_registration(w, namespace=w) for w in names]
    fake.hooks.append(
        _registration(
            "wh-fresh",
            namespace="fresh",
            org="globex",
            secret_ref="LOOM_WEBHOOK_SECRET_GLOBEX__CHAT",
        )
    )
    fake.outbox = [
        _outbox_row(
            webhook_id=w, namespace=w, event_id=f"{w}-{i}", occurred_at=NOW - timedelta(hours=1)
        )
        for w in names
        for i in range(30)
    ]
    fake.outbox.append(
        _outbox_row(webhook_id="wh-fresh", namespace="fresh", org="globex", event_id="fresh-1")
    )
    fake.history = {(org, ns, *KEY[2:]) for org in ("acme", "globex") for ns in names + ["fresh"]}
    in_flight = {"now": 0, "most": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "wh-slow.example":
            in_flight["now"] += 1
            in_flight["most"] = max(in_flight["most"], in_flight["now"])
            await asyncio.sleep(0.01)
            in_flight["now"] -= 1
        return httpx.Response(204)

    env = {REF: SECRET, "LOOM_WEBHOOK_SECRET_GLOBEX__CHAT": SECRET}
    result = await _pass(handler, env=env)
    assert fake.last("fresh-1")["status"] == "delivered"
    assert result.delivered == settings.memory_webhook_max_posts_per_pass
    assert in_flight["most"] == 1


@pytest.mark.asyncio
async def test_the_receivers_last_status_survives_a_lease_and_a_cooldown_wait(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _pass(lambda r: httpx.Response(503))
    assert fake.last()["last_status"] == 503

    # The retry's lease keeps it while the POST is out.
    leases: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        leases.append(fake.last())
        return httpx.Response(503)

    await _pass(handler, now=NOW + timedelta(seconds=6))
    assert leases[0]["status"] == "leased" and leases[0]["last_status"] == 503

    # A cooldown wait keeps it too, with its reason as the error: an older event fails
    # first and cools the webhook down, then ev-1 waits.
    monkeypatch.setattr(settings, "memory_webhook_failures_before_cooldown", 1)
    fake.outbox.append(_outbox_row(event_id="ev-0", occurred_at=NOW - timedelta(minutes=1)))
    await _pass(lambda r: httpx.Response(502), now=NOW + timedelta(minutes=10))
    assert fake.last("ev-0")["last_status"] == 502
    waited = fake.last()
    assert waited["last_error"] == "the webhook is cooling down"
    assert waited["last_status"] == 503 and waited["attempts"] == 2


@pytest.mark.asyncio
async def test_a_receiver_that_is_always_down_has_every_event_dead_before_the_ttl(
    fake: FakeStore,
) -> None:
    fake.outbox = [_outbox_row(event_id=f"ev-{i}") for i in range(15)]
    when = NOW
    while when < NOW + timedelta(days=store.KEEP_DAYS):
        await _pass(lambda r: httpx.Response(503), now=when)
        when += timedelta(hours=1)
        if all(
            fake.latest(o["event_id"]) and fake.latest(o["event_id"])["status"] == "dead"
            for o in fake.outbox
        ):
            break
    assert when < NOW + timedelta(days=store.KEEP_DAYS)
    reasons = {fake.last(o["event_id"])["last_error"] for o in fake.outbox}
    assert "not delivered in time" in reasons


def test_the_backoff_levels_off_at_its_ceiling() -> None:
    steps = parse_backoff_steps(settings.memory_webhook_backoff_steps_seconds)
    assert steps == (5, 30, 120, 600, 1800)
    assert [backoff_seconds(n, steps, 3600) for n in (0, 1, 5, 6, 60)] == [5, 5, 1800, 3600, 3600]
    assert [backoff_seconds(n, steps, 60) for n in (1, 3, 9)] == [5, 60, 60]
    assert [backoff_seconds(n, (), 60) for n in (1, 2)] == [60, 60]


@pytest.mark.asyncio
async def test_a_backoff_is_measured_from_the_failure(fake: FakeStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        CLOCK.advance(7)  # the receiver takes 7 s to fail
        return httpx.Response(500)

    await _pass(handler)
    out = fake.last()
    assert out["recorded_at"] == NOW + timedelta(seconds=7)
    assert out["next_attempt_at"] == NOW + timedelta(seconds=7 + 5)


def _two_webhooks(fake: FakeStore, b_events: int = 1) -> None:
    fake.hooks = [_registration("wh-a", namespace="a"), _registration("wh-b", namespace="b")]
    fake.outbox = [_outbox_row(webhook_id="wh-a", namespace="a", event_id="a-1")] + [
        _outbox_row(webhook_id="wh-b", namespace="b", event_id=f"b-{i}") for i in range(b_events)
    ]
    fake.history = {(KEY[0], ns, *KEY[2:]) for ns in ("a", "b")}


@pytest.mark.asyncio
async def test_a_check_that_fails_leaves_its_event_leased_and_the_pass_going(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_webhooks(fake)
    real = fake.keys_with_history

    async def flaky(keys: list[tuple[str, ...]]) -> set[tuple[str, ...]]:
        if keys[0][1] == "a":
            raise RuntimeError("ClickHouse is away")
        return await real(keys)

    monkeypatch.setattr(fake, "keys_with_history", flaky)
    seen: list[str] = []
    result = await _pass(lambda r: seen.append(r.headers["host"]) or httpx.Response(204))
    assert seen == ["b.example"] and result.delivered == 1
    assert fake.last("b-0")["status"] == "delivered"
    assert fake.last("a-1")["status"] == "leased"


@pytest.mark.asyncio
async def test_an_error_in_one_webhooks_turn_stops_every_post_and_keeps_the_finished(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_webhooks(fake, b_events=3)
    seen: list[str] = []
    b_second_out = asyncio.Event()
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        if request.headers["host"] == "a.example":
            await b_second_out.wait()  # b's second POST is out when a's turn fails
        elif seen.count("b.example") > 1:
            b_second_out.set()
            await never.wait()
        return httpx.Response(204)

    real = worker._note_post

    def broken(webhook_id: str, ok: bool) -> None:
        if webhook_id == "wh-a":
            raise RuntimeError("a bug")
        real(webhook_id, ok)

    monkeypatch.setattr(worker, "_note_post", broken)
    with pytest.raises(ExceptionGroup):
        await _pass(handler)
    # The pass returns only once the task group has ended b's turn, so b's third POST
    # never goes out.
    assert sorted(seen) == ["a.example", "b.example", "b.example"]
    assert fake.last("b-0")["status"] == "delivered"  # finished, so recorded
    assert {fake.last(e)["status"] for e in ("a-1", "b-1", "b-2")} == {"leased"}


@pytest.mark.asyncio
async def test_a_pass_cancelled_from_outside_still_records_what_it_sent(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_webhooks(fake)
    b_is_out = asyncio.Event()
    a_answered = asyncio.Event()
    never = asyncio.Event()
    real = worker._note_post

    def note(webhook_id: str, ok: bool) -> None:
        real(webhook_id, ok)
        if webhook_id == "wh-a":
            a_answered.set()  # a-1's outcome is recorded right after this, in the same step

    monkeypatch.setattr(worker, "_note_post", note)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "b.example":
            b_is_out.set()
            await never.wait()
        return httpx.Response(204)

    task = asyncio.create_task(_pass(handler))
    await asyncio.wait_for(asyncio.gather(b_is_out.wait(), a_answered.wait()), 5)
    # a-1's answer is in; b-0's is not
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.last("a-1")["status"] == "delivered"
    assert fake.last("b-0")["status"] == "leased"


@pytest.mark.asyncio
async def test_a_pass_cut_at_its_budget_still_records_what_it_sent(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "_post_budget_seconds", lambda: 0.3)
    fake.hooks = [
        _registration("wh-fast", namespace="fast"),
        _registration("wh-slow", namespace="slow"),
    ]
    fake.outbox = [
        _outbox_row(webhook_id="wh-fast", namespace="fast", event_id="fast-1"),
        _outbox_row(webhook_id="wh-slow", namespace="slow", event_id="slow-1"),
    ]
    fake.history = {(KEY[0], ns, *KEY[2:]) for ns in ("fast", "slow")}
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "slow.example":
            await never.wait()  # only the budget ends this POST
        return httpx.Response(204)

    result = await _pass(handler)
    assert result.delivered == 1
    assert fake.last("fast-1")["status"] == "delivered"
    # Unsent by the budget: it keeps its lease and goes again once the lease ends.
    assert fake.last("slow-1")["status"] == "leased"


@pytest.mark.asyncio
async def test_the_post_budget_counts_from_the_start_of_the_pass(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reads before the POSTs spend the budget too, so a slow due read leaves the
    POSTs only what is left of it and the pass still ends inside its deadline."""
    monkeypatch.setattr(worker, "_post_budget_seconds", lambda: 0.3)
    read_due = fake.due_events

    async def slow_due_events(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(0.4)  # longer than the whole budget
        return await read_due(*args, **kwargs)

    monkeypatch.setattr(fake, "due_events", slow_due_events)

    async def handler(request: httpx.Request) -> httpx.Response:
        # Well inside a budget of its own, so only a budget counted from the start of
        # the pass cuts it.
        await asyncio.sleep(0.1)
        return httpx.Response(204)

    result = await _pass(handler)
    assert result.delivered == 0
    assert fake.last("ev-1")["status"] == "leased"


@pytest.mark.parametrize(("concurrency", "per_webhook"), [(4, 10), (8, 10), (4, 20)])
def test_the_post_budget_fits_one_webhooks_whole_turn(
    monkeypatch: pytest.MonkeyPatch, concurrency: int, per_webhook: int
) -> None:
    """One webhook's POSTs go one after another: more slots or a longer turn must not
    cut a slow receiver's turn short."""
    monkeypatch.setattr(settings, "memory_webhook_max_posts_per_pass", 40)
    monkeypatch.setattr(settings, "memory_webhook_deliver_concurrency", concurrency)
    monkeypatch.setattr(settings, "memory_webhook_posts_per_webhook", per_webhook)
    monkeypatch.setattr(settings, "memory_webhook_ack_timeout_seconds", 10.0)
    monkeypatch.setattr(settings, "memory_webhook_deliver_max_pass_seconds", 600.0)
    monkeypatch.setattr(settings, "memory_webhook_deliver_reserve_seconds", 20.0)
    budget = worker._post_budget_seconds()
    assert budget >= per_webhook * 10.0
    assert budget >= 40 / concurrency * 10.0
    assert budget == max(40 / concurrency, per_webhook) * 10.0


def test_the_post_budget_stops_short_of_the_pass_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "memory_webhook_posts_per_webhook", 20)
    monkeypatch.setattr(settings, "memory_webhook_ack_timeout_seconds", 10.0)
    monkeypatch.setattr(settings, "memory_webhook_deliver_max_pass_seconds", 120.0)
    monkeypatch.setattr(settings, "memory_webhook_deliver_reserve_seconds", 20.0)
    assert worker._post_budget_seconds() == 100.0


@pytest.mark.asyncio
async def test_an_erasure_while_an_event_waits_for_a_slot_stops_its_post(
    fake: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_webhook_deliver_concurrency", 1)
    fake.hooks = [_registration("wh-a", namespace="a"), _registration("wh-b", namespace="b")]
    fake.outbox = [
        _outbox_row(webhook_id="wh-a", namespace="a", event_id="a-1"),
        _outbox_row(webhook_id="wh-b", namespace="b", event_id="b-1", user_id="u2"),
    ]
    fake.history = {(KEY[0], "a", *KEY[2:]), (KEY[0], "b", KEY[2], "u2", *KEY[4:])}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        fake.erase_user("u2")  # while b-1 waits for the only slot
        return httpx.Response(204)

    await _pass(handler)
    assert seen == ["a.example"]
    assert fake.last("b-1")["status"] == "dead"
    assert fake.last("b-1")["last_error"] == "the memory was erased"


@pytest.mark.asyncio
async def test_a_second_pass_overlapping_the_first_passes_over_its_leased_events(
    fake: FakeStore,
) -> None:
    second: list[Any] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        # The first pass is mid-POST: its lease is in, its outcome is not.
        second.append(await worker.deliver_once(env={REF: SECRET}))
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
    # The lease and the pass's outcome stay; they name no key or user.
    assert [a["status"] for a in fake.attempts] == ["leased", "pending"]
    assert "u1" not in json.dumps(fake.attempts, default=str)
    assert KEY[5] not in json.dumps(fake.attempts, default=str)
    seen: list[httpx.Request] = []
    later = await _pass(
        lambda r: seen.append(r) or httpx.Response(204), now=NOW + timedelta(hours=1)
    )
    assert later.total == 0 and seen == []


# ------------------------------------------------------------ reads


class _SeqCH:
    """Records whether each read ran with sequential consistency."""

    def __init__(self) -> None:
        self.sequential: list[bool] = []

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[Any]:
        self.sequential.append(bool(sequential_reads_var.get()))
        return []

    async def execute(self, sql: str, params: Any = None, **_: Any) -> None:
        self.sequential.append(bool(sequential_reads_var.get()))


@pytest.mark.asyncio
async def test_the_workers_reads_see_the_latest_writes() -> None:
    ch = _SeqCH()
    await store.keys_with_history([KEY], ch=ch)  # type: ignore[arg-type]
    await store.due_events(
        NOW,
        since=NOW,
        hooks=[("acme", "chat", "wh1")],
        per_webhook=1,
        limit=1,
        ch=ch,  # type: ignore[arg-type]
    )
    await store.mark_dead(
        NOW,
        since=NOW,
        hooks=[("acme", "chat", "wh1")],
        reason="the webhook was removed",
        version=1,
        ch=ch,  # type: ignore[arg-type]
    )
    assert ch.sequential == [True, True, True]


# ------------------------------------------------------------ the POST


async def _local_server(respond: Any) -> tuple[asyncio.Server, str]:
    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}/hook"


@pytest.mark.asyncio
async def test_a_receiver_that_drips_its_answer_is_cut_at_the_deadline() -> None:
    async def drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        for byte in b"HTTP/1.1 200 OK\r\n":
            writer.write(bytes([byte]))
            await writer.drain()
            await asyncio.sleep(0.2)
        writer.close()

    server, url = await _local_server(drip)
    async with server:
        started = asyncio.get_running_loop().time()
        out = await signed.post(
            None, url, {}, b"{}", private_hosts={"127.0.0.1"}, ack_timeout_seconds=0.5
        )
        assert asyncio.get_running_loop().time() - started < 2
    assert out == signed.Delivery(0, "no answer: timeout")


@pytest.mark.asyncio
async def test_an_endless_answer_body_is_never_read() -> None:
    async def endless(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        try:
            while True:
                writer.write(b"400\r\n" + b"x" * 1024 + b"\r\n")
                await writer.drain()
        except (ConnectionError, OSError):
            pass

    server, url = await _local_server(endless)
    async with server:
        out = await asyncio.wait_for(
            signed.post(
                None, url, {}, b"{}", private_hosts={"127.0.0.1"}, ack_timeout_seconds=10.0
            ),
            timeout=5,
        )
    assert out.ok and out.status == 200


@pytest.mark.asyncio
async def test_a_slow_name_lookup_is_inside_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(host: str, **_: Any) -> tuple[str, str]:
        await asyncio.sleep(5)
        return "", PUBLIC

    monkeypatch.setattr(signed, "vet_host", slow)
    post = signed.post(None, "https://hook.example/x", {}, b"{}", ack_timeout_seconds=0.2)
    out = await asyncio.wait_for(post, 2)
    assert out.error == "no answer: timeout"


# ------------------------------------------------------------ URL rules and registration


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
async def test_a_url_resolving_inward_is_refused_without_naming_the_address(
    monkeypatch: pytest.MonkeyPatch, url: str, resolves_to: str
) -> None:
    import server.core.hosts as hosts

    monkeypatch.setattr(hosts, "_resolve", lambda host: [resolves_to])
    monkeypatch.setattr(settings, "memory_webhook_private_hosts", "")
    refusal = await app.url_refusal(url)
    assert "a private or local address" in refusal and resolves_to not in refusal


@pytest.mark.asyncio
async def test_a_url_that_does_not_parse_is_refused() -> None:
    assert await app.url_refusal("https://[::1/x") == "the URL is not valid"
    assert (await signed.pinned_target("https://[::1/x"))[0] == "the URL is not valid"


@pytest.mark.asyncio
async def test_http_and_userinfo_are_refused_unless_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(signed, "vet_host", _public)
    monkeypatch.setattr(settings, "memory_webhook_allow_http", False)
    assert await app.url_refusal("http://chat.example/hook")
    assert await app.url_refusal("ftp://chat.example/hook")
    assert await app.url_refusal("https://chat.example/hook") == ""
    assert await app.url_refusal("https://user:pw@chat.example/hook")
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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ManualClock:
    """The app's clock seam (`naive_utc_now`) on a manual clock."""
    clock = ManualClock(t=NOW.replace(tzinfo=UTC).timestamp())
    monkeypatch.setattr(
        app, "naive_utc_now", lambda: datetime.fromtimestamp(clock(), UTC).replace(tzinfo=None)
    )
    return clock


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch, clock: ManualClock) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def listed(org: str, namespace: str) -> list[dict[str, Any]]:
        return [r for r in rows if not r["is_deleted"]]

    async def insert(row: dict[str, Any]) -> None:
        rows[:] = [r for r in rows if r["webhook_id"] != row["webhook_id"]] + [row]

    async def no_refusal(url: str) -> str:
        return ""

    monkeypatch.setattr(store, "namespace_webhooks", listed)
    monkeypatch.setattr(store, "insert_webhook", insert)
    monkeypatch.setattr(app, "url_refusal", no_refusal)
    return rows


async def _register(url: str) -> Any:
    out = await app.handle_webhook_register(
        WebhookRegisterInput(org="acme", namespace="chat", url=url, secret_ref=REF)
    )
    return out.webhook


@pytest.mark.asyncio
async def test_a_namespace_holds_ten_webhooks_and_a_url_again_replaces_its_own(
    registered: list[dict[str, Any]],
) -> None:
    first = await _register("https://chat.example/0")
    for i in range(1, settings.memory_webhook_max_per_namespace):
        await _register(f"https://chat.example/{i}")
    with pytest.raises(ValidationError, match="at most 10"):
        await _register("https://chat.example/10")
    again = await _register("https://chat.example/0")
    assert again.webhook_id == first.webhook_id and len(registered) == 10


@pytest.mark.asyncio
async def test_a_registration_keeps_no_principal_id_for_an_erasure_to_leave_behind(
    registered: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registrant is u-1; a user erasure of u-1 has nothing to rewrite here.
    monkeypatch.setattr(app.provenance, "writer_actor", lambda *_: "user:u-1")
    info = await _register("https://chat.example/h")
    (row,) = registered
    assert set(row) == set(store.WEBHOOK_COLS)
    assert all("u-1" not in str(v) for v in row.values())
    assert "u-1" not in info.model_dump_json()


@pytest.mark.asyncio
async def test_a_url_registered_again_after_a_remove_gets_a_new_id(
    registered: list[dict[str, Any]], clock: ManualClock
) -> None:
    from server.memory.api.models_webhook import WebhookRemoveInput

    first = await _register("https://chat.example/h")
    await app.handle_webhook_remove(
        WebhookRemoveInput(org="acme", namespace="chat", webhook_id=first.webhook_id)
    )
    clock.advance(1)
    second = await _register("https://chat.example/h")
    assert second.webhook_id != first.webhook_id


def test_the_worker_is_registered_on_its_settings() -> None:
    from server.workers.registry import enabled_workers, worker_meta

    spec = {s.name: s for s in enabled_workers()}["memory_webhook_deliver"]
    assert spec.interval_seconds == settings.memory_webhook_deliver_interval_seconds == 5
    assert spec.idle_interval_seconds == settings.memory_webhook_deliver_idle_interval_seconds
    assert spec.max_pass_seconds == 120.0
    assert worker_meta()["memory_webhook_deliver"]["enabled"] is (
        settings.memory_webhook_deliver_enabled
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_webhook_deliver_interval_seconds", 0),
        ("memory_webhook_deliver_idle_interval_seconds", 0),
        ("memory_webhook_deliver_max_pass_seconds", 29),
        ("memory_webhook_deliver_reserve_seconds", 0),
        ("memory_webhook_lease_margin_seconds", 0),
        ("memory_webhook_max_attempts", 0),
        ("memory_webhook_failures_before_cooldown", 0),
        ("memory_webhook_cooldown_seconds", 0),
        ("memory_webhook_max_cooldown_seconds", 0),
        ("memory_webhook_refresh_seconds", 0),
        ("memory_webhook_max_per_namespace", 0),
        ("memory_webhook_max_posts_per_pass", 0),
        ("memory_webhook_posts_per_webhook", 0),
        ("memory_webhook_deliver_concurrency", 0),
        ("memory_webhook_ack_timeout_seconds", 0),
        ("memory_webhook_max_backoff_seconds", 0),
        ("memory_webhook_removal_window_leases", 0),
        ("memory_webhook_backoff_steps_seconds", "5,0,120"),
        ("memory_webhook_backoff_steps_seconds", "5,-30"),
        ("memory_webhook_backoff_steps_seconds", "5,soon"),
        ("memory_webhook_backoff_steps_seconds", ""),
        ("memory_webhook_backoff_steps_seconds", ", ,"),
    ],
)
def test_a_memory_webhook_setting_below_its_floor_is_refused(field: str, value: object) -> None:
    from server.settings.catalog import MemoryWebhookSettings

    with pytest.raises(PydanticValidationError):
        MemoryWebhookSettings(**{field: value})


def test_a_delivery_reserve_not_below_the_pass_deadline_is_refused() -> None:
    from server.settings.catalog import MemoryWebhookSettings

    with pytest.raises(PydanticValidationError, match="RESERVE_SECONDS must be less"):
        MemoryWebhookSettings(
            memory_webhook_deliver_reserve_seconds=120,
            memory_webhook_deliver_max_pass_seconds=120,
        )
