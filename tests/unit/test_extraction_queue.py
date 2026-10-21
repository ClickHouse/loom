"""Deferred extraction for memory.set_from_messages.

- `defer=true` persists the request and returns `deferred` + `queue_id`
  without touching the extractor; a queue insert failure is a 503, never a
  silent 200.
- The stored payload rebuilds the exact input with `defer` forced off, so the
  worker replays the unchanged inline path.
- Every queue-row transition mirrors every column (ReplacingMergeTree
  contract), claims carry a lease, and failures back off to `pending` until
  `max_attempts` parks the row `failed`.
- `pending_extraction` on search/ask is cached per scope and fail-quiet.

CH-free: the CH client is a stub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from server.core.errors import ModelCallDeferred, ModelCallRefused
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.llm.gateway import GatewayUnavailable
from server.llm.identity import IdentityUnread
from server.memory.api.models_bulk import MemorySetFromMessagesInput, _Message
from server.memory.app import extraction_queue as q
from server.memory.app import extraction_queue as queue
from server.memory.app import pending_disclosure
from server.memory.workers import extraction_queue as worker
from server.workers.status import get_registry


class StubCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows, settings or {}))

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None


def _input(**kw: Any) -> MemorySetFromMessagesInput:
    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        agent="a",
        user_id="u1",
        messages=[
            _Message(role="user", content="I moved to Lisbon last week"),
            _Message(role="assistant", content="Noted — Lisbon."),
        ],
        observation_date="2026-09-12",
        extraction_instructions="keep places",
        extra_tags=["from_test"],
    )
    base.update(kw)
    return MemorySetFromMessagesInput(**base)


@pytest.fixture(autouse=True)
def _fresh_state():
    pending_disclosure.reset_cache()
    get_registry().reset()
    yield
    pending_disclosure.reset_cache()
    get_registry().reset()


# ---------------------------------------------------------------- payload


def test_payload_excludes_defer_and_rebuild_forces_inline() -> None:
    payload, n_msgs, chars = q.payload_for(_input(defer=True), scope=WHOLE_NAMESPACE)
    data = json.loads(payload)
    assert "defer" not in data
    assert n_msgs == 2
    assert chars == len("I moved to Lisbon last week") + len("Noted — Lisbon.")

    rebuilt = queue.rebuild_input({"payload": payload})
    assert rebuilt.defer is False
    assert rebuilt.observation_date == "2026-09-12"
    assert rebuilt.extraction_instructions == "keep places"
    assert rebuilt.extra_tags == ["from_test"]
    assert [m.content for m in rebuilt.messages] == [
        "I moved to Lisbon last week",
        "Noted — Lisbon.",
    ]
    assert rebuilt.user_id == "u1"


# ---------------------------------------------------------------- enqueue


@pytest.mark.asyncio
async def test_enqueue_writes_one_full_pending_row() -> None:
    ch = StubCH()
    qid = await q.enqueue(_input(defer=True), scope=WHOLE_NAMESPACE, ch=ch)
    assert len(qid) == 32
    assert len(ch.inserts) == 1
    table, rows, settings = ch.inserts[0]
    assert table == q.TABLE
    assert settings["async_insert"] == 0
    row = rows[0]
    assert tuple(row.keys()) == q.COLUMNS
    assert row["status"] == q.STATUS_PENDING
    assert row["queue_id"] == qid
    assert row["message_count"] == 2
    assert row["attempts"] == 0
    assert row["user_id"] == "u1" and row["session_id"] == "-"


# ---------------------------------------------------------------- handler


@pytest.mark.asyncio
async def test_handler_defer_short_circuits_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx

    async def _enqueue(input, *, scope, ch=None):
        assert input.defer is True
        return "abc123"

    async def _boom(*a, **k):
        raise AssertionError("extraction must not run on the defer path")

    monkeypatch.setattr(q, "enqueue", _enqueue)
    monkeypatch.setattr(mx, "_write_source_evidence", _boom)

    out = await mx.handle_memory_set_from_messages(_input(defer=True))
    assert out.deferred is True
    assert out.queue_id == "abc123"
    assert out.written == []
    assert out.tokens_in == 0


@pytest.mark.asyncio
async def test_handler_defer_enqueue_failure_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract.handler as mx

    async def _enqueue(input, *, scope, ch=None):
        raise RuntimeError("ch down")

    monkeypatch.setattr(q, "enqueue", _enqueue)
    with pytest.raises(HTTPException) as ei:
        await mx.handle_memory_set_from_messages(_input(defer=True))
    assert ei.value.status_code == 503
    assert ei.value.detail["code"] == "EXTRACTION_QUEUE_UNAVAILABLE"


def test_defer_defaults_off() -> None:
    assert _input().defer is False


# ---------------------------------------------------------------- read scope


def _pinned_auth(user: str, read_scope: str = "") -> Any:
    from server.auth.jwt import AuthInfo

    return AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        read_user_id=user,
        read_scope=read_scope,
    )


def _in_context(auth: Any, fn: Any) -> Any:
    """Run `fn()` with `auth` bound and its scope resolved, as the transport does."""
    import contextvars

    from server.auth.context import set_current_auth
    from server.identity.domain.read_scope import (
        bind_request_read_filters,
        resolve_read_filters,
    )

    def go() -> Any:
        set_current_auth(auth)
        bind_request_read_filters(
            auth, resolve_read_filters(requested=None, user_id=None, session_id=None, auth=auth)
        )
        return fn()

    return contextvars.Context().run(go)


def test_payload_stores_the_requests_read_scope() -> None:
    payload, _, _ = q.payload_for(
        _input(), scope=ReadFilters(scope="own_user", user_id="u1", session_id=None)
    )
    assert json.loads(payload)[q.READ_SCOPE_KEY] == {
        "scope": "own_user",
        "user_id": "u1",
        "session_id": None,
    }
    assert q.READ_SCOPE_KEY not in queue.rebuild_input({"payload": payload}).model_dump()


@pytest.mark.asyncio
async def test_replay_runs_under_the_stored_scope_and_unbinds_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx
    from server.identity.app.request_scope import request_scope
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput

    seen: list[Any] = []

    async def _handler(input):
        seen.append(request_scope())
        return MemorySetFromMessagesOutput()

    monkeypatch.setattr(mx, "handle_memory_set_from_messages", _handler)
    payload, _, _ = q.payload_for(
        _input(), scope=ReadFilters(scope="own_user", user_id="u1", session_id=None)
    )
    await queue.replay_row({"payload": payload})
    assert (seen[0].scope, seen[0].user_id) == ("own_user", "u1")
    assert request_scope().narrowed is False, "the next replay starts unbound"


def test_a_payload_queued_before_scopes_were_stored_replays_namespace_wide() -> None:
    payload, _, _ = q.payload_for(_input(), scope=WHOLE_NAMESPACE)
    data = json.loads(payload)
    data.pop(q.READ_SCOPE_KEY)
    assert queue.replay_scope({"payload": json.dumps(data)}).narrowed is False


@pytest.mark.parametrize(
    "stored",
    [
        {"scope": "own_user", "user_id": None, "session_id": None},
        {"scope": "own_session", "user_id": "u1", "session_id": None},
        {"scope": "everyone", "user_id": "u1", "session_id": None},
    ],
)
def test_an_incomplete_stored_scope_fails_the_row(stored: dict[str, Any]) -> None:
    payload = json.dumps(
        {**json.loads(q.payload_for(_input(), scope=WHOLE_NAMESPACE)[0]), q.READ_SCOPE_KEY: stored}
    )
    with pytest.raises(ValueError):
        queue.replay_scope({"payload": payload})


def test_defer_with_a_scope_that_does_not_resolve_is_refused_not_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import server.memory.app.extract.handler as mx
    from server.core.errors import IdentityIncompleteError

    async def _enqueue(input, *, scope, ch=None):
        raise AssertionError("nothing is queued")

    monkeypatch.setattr(q, "enqueue", _enqueue)
    unpinned_own_user = _pinned_auth("", read_scope="own_user")
    with pytest.raises(IdentityIncompleteError):
        _in_context(
            unpinned_own_user,
            lambda: asyncio.run(mx.handle_memory_set_from_messages(_input(defer=True))),
        )


# ---------------------------------------------------------------- lifecycle


def _stored_row(**kw: Any) -> dict[str, Any]:
    payload, n, chars = q.payload_for(_input(), scope=WHOLE_NAMESPACE)
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u1",
        "session_id": "-",
        "queue_id": "q1",
        "payload": payload,
        "message_count": n,
        "content_chars": chars,
        "status": q.STATUS_PENDING,
        "attempts": 0,
        "lease_until": datetime(1970, 1, 1, tzinfo=UTC),
        "last_error": "",
        "written": 0,
        "enqueued_at": datetime(2026, 9, 12, 7, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 12, 7, 0, tzinfo=UTC),
        "version": 1,
    }
    row.update(kw)
    return row


@pytest.mark.asyncio
async def test_claim_mirrors_every_column_and_leases() -> None:
    ch = StubCH()
    ch.query_all_returns = [[_stored_row()]]
    before = datetime.now(UTC)
    claimed = await q.claim_batch(limit=5, lease_seconds=600, ch=ch)
    assert len(claimed) == 1
    row = claimed[0]
    assert tuple(row.keys()) == q.COLUMNS
    assert row["status"] == q.STATUS_RUNNING
    assert row["lease_until"] > before
    assert row["payload"] == _stored_row()["payload"]
    assert row["enqueued_at"] == _stored_row()["enqueued_at"]
    assert row["version"] > 1
    # Reclaim predicate: pending OR lease-expired running, oldest first.
    sql, params = ch.query_all_calls[0]
    assert "lease_until < now64(3)" in sql
    assert "ORDER BY enqueued_at ASC" in sql
    assert params["lim"] == 5
    assert ch.inserts[0][0] == q.TABLE and ch.inserts[0][1] == claimed


@pytest.mark.asyncio
async def test_claim_empty_queue_writes_nothing() -> None:
    ch = StubCH()
    assert await q.claim_batch(limit=5, lease_seconds=1, ch=ch) == []
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_error_backs_off_then_parks_failed() -> None:
    ch = StubCH()
    row = _stored_row(status=q.STATUS_RUNNING)
    st = await q.mark_error(row, error="ExtractionError: boom", max_attempts=3, ch=ch)
    assert st == q.STATUS_PENDING
    written = ch.inserts[-1][1][0]
    assert written["attempts"] == 1
    assert written["last_error"].startswith("ExtractionError")
    assert tuple(written.keys()) == q.COLUMNS

    row2 = _stored_row(status=q.STATUS_RUNNING, attempts=2)
    st = await q.mark_error(row2, error="x", max_attempts=3, ch=ch)
    assert st == q.STATUS_FAILED
    assert ch.inserts[-1][1][0]["attempts"] == 3


@pytest.mark.asyncio
async def test_done_records_written_count() -> None:
    ch = StubCH()
    await q.mark_done(_stored_row(status=q.STATUS_RUNNING), written=7, ch=ch)
    written = ch.inserts[-1][1][0]
    assert written["status"] == q.STATUS_DONE
    assert written["written"] == 7
    assert tuple(written.keys()) == q.COLUMNS


# ---------------------------------------------------------------- worker


@pytest.mark.asyncio
async def test_worker_replays_claimed_rows_and_marks_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = StubCH()
    good = _stored_row(queue_id="good")
    bad = _stored_row(queue_id="bad", attempts=2)
    ch.query_all_returns = [[good, bad]]
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    seen: list[MemorySetFromMessagesInput] = []

    async def _fake_handler(inp):
        seen.append(inp)
        if inp.messages[0].content.startswith("FAIL"):
            raise RuntimeError("extractor down")

        class _Out:
            written = [1, 2, 3]

        return _Out()

    from server.memory.app.extract import handler as extract_handler_mod

    monkeypatch.setattr(extract_handler_mod, "handle_memory_set_from_messages", _fake_handler)
    bad_payload = json.loads(bad["payload"])
    bad_payload["messages"][0]["content"] = "FAIL now"
    bad["payload"] = json.dumps(bad_payload)

    res = await worker.run_pass(worker.ExtractionQueueConfig(max_attempts=3))

    assert res.done_count == 1 and res.written_memories == 3
    assert res.failures == ["bad: failed"]
    assert all(inp.defer is False for inp in seen)
    # inserts: 1 claim (2 rows) + done + failed
    statuses = [r["status"] for _, rows, _ in ch.inserts[1:] for r in rows]
    assert statuses == [q.STATUS_DONE, q.STATUS_FAILED]
    snap = get_registry().snapshot()["extraction_queue"]
    assert snap["last_outcome"]["done"] == 1
    assert snap["last_outcome"]["failed"] == 1


# ---------------------------------------------------------------- disclosure


@pytest.mark.asyncio
async def test_pending_disclosure_caches_per_scope_and_fails_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = StubCH()
    ch.query_one_returns = [{"n": 2}]
    n1 = await pending_disclosure.pending_extraction(
        org="o", namespace="n", agent="*", user_id=None, ch=ch
    )
    n2 = await pending_disclosure.pending_extraction(
        org="o", namespace="n", agent="*", user_id=None, ch=ch
    )
    assert (n1, n2) == (2, 2)
    assert len(ch.query_one_calls) == 1  # second call served from cache
    sql, params = ch.query_one_calls[0]
    assert "agent =" not in sql and "user_id =" not in sql  # "*" / None widen

    # A different scope is its own key and narrows by agent + user.
    ch.query_one_returns = [{"n": 1}]
    n3 = await pending_disclosure.pending_extraction(
        org="o", namespace="n", agent="a", user_id="u1", ch=ch
    )
    assert n3 == 1
    sql, params = ch.query_one_calls[1]
    assert "agent =" in sql and params["uid"] == "u1"

    class _Broken:
        async def query_one(self, *a, **k):
            raise RuntimeError("no table")

    pending_disclosure.reset_cache()
    assert (
        await pending_disclosure.pending_extraction(
            org="o", namespace="n", agent="*", user_id=None, ch=_Broken()
        )
        == 0
    )


# ---------------------------------------------------------------- status op


@pytest.mark.asyncio
async def test_extraction_status_counts_pending_and_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx
    from server.memory.api.models_bulk import MemoryExtractionStatusInput

    rows = [
        {
            "queue_id": "q1",
            "agent": "a",
            "user_id": "u1",
            "session_id": "-",
            "status": "pending",
            "attempts": 0,
            "message_count": 2,
            "content_chars": 40,
            "written": 0,
            "last_error": "",
            "enqueued_at": None,
            "updated_at": None,
        },
        {
            "queue_id": "q2",
            "agent": "a",
            "user_id": "u1",
            "session_id": "-",
            "status": "running",
            "attempts": 1,
            "message_count": 2,
            "content_chars": 40,
            "written": 0,
            "last_error": "",
            "enqueued_at": None,
            "updated_at": None,
        },
        {
            "queue_id": "q3",
            "agent": "a",
            "user_id": "u1",
            "session_id": "-",
            "status": "failed",
            "attempts": 3,
            "message_count": 2,
            "content_chars": 40,
            "written": 0,
            "last_error": "ExtractionError: x",
            "enqueued_at": None,
            "updated_at": None,
        },
    ]
    captured: dict[str, Any] = {}

    async def _list(**kw):
        captured.update(kw)
        return rows

    monkeypatch.setattr(q, "list_status", _list)
    out = await mx.handle_memory_extraction_status(
        MemoryExtractionStatusInput(org="o", namespace="n", user_id="u1", status="")
    )
    assert out.pending == 2 and out.failed == 1
    assert [b.queue_id for b in out.batches] == ["q1", "q2", "q3"]
    assert captured["queue_id"] is None and captured["status"] is None
    assert captured["agent"] == "*"
    assert captured["scope"] == WHOLE_NAMESPACE  # the handler resolves and passes it


def test_extraction_status_for_a_user_bound_token_lists_only_its_users_batches() -> None:
    import asyncio

    from server.identity.app.request_scope import request_scope

    ch = StubCH()
    _in_context(
        _pinned_auth("u1"),
        lambda: asyncio.run(
            q.list_status(org="o", namespace="n", scope=request_scope(), user_id="u2", ch=ch)
        ),
    )
    sql, params = ch.query_all_calls[0]
    assert "user_id = {scope_uid:String}" in sql and params["scope_uid"] == "u1"


# ------------------------------------------------- ordering


def _dated_row(queue_id: str, namespace: str, date: str, enqueued_at: datetime) -> dict[str, Any]:
    row = _stored_row(queue_id=queue_id, namespace=namespace, enqueued_at=enqueued_at)
    payload = json.loads(row["payload"])
    payload["namespace"] = namespace  # the stored request names the same namespace as the row
    if date:
        payload["observation_date"] = date
    else:
        payload.pop("observation_date", None)
    row["payload"] = json.dumps(payload)
    return row


def test_order_within_namespace_uses_session_date_then_enqueue_time() -> None:
    t0 = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    rows = [
        _dated_row("late-session-first-enqueued", "n", "2023-06-01", t0),
        _dated_row("early-session", "n", "2023-05-08", t0.replace(minute=1)),
        _dated_row("undated", "n", "", t0.replace(minute=2)),
        _dated_row("other-ns", "m", "2023-01-01", t0.replace(minute=3)),
        _dated_row("mid-session", "n", "2023-05-20", t0.replace(minute=4)),
    ]
    groups = worker.order_within_namespace(rows)
    assert set(groups) == {("o", "n"), ("o", "m")}
    assert [r["queue_id"] for r in groups[("o", "n")]] == [
        "undated",  # no date: enqueue order is the only signal, never waits behind a dated row
        "early-session",
        "mid-session",
        "late-session-first-enqueued",
    ]
    assert [r["queue_id"] for r in groups[("o", "m")]] == ["other-ns"]


@pytest.mark.asyncio
async def test_worker_is_serial_within_a_namespace_and_parallel_across(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    ch = StubCH()
    t0 = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    ch.query_all_returns = [
        [
            _dated_row("n-late", "n", "2023-06-01", t0),
            _dated_row("m-1", "m", "2023-01-01", t0.replace(minute=1)),
            _dated_row("n-early", "n", "2023-05-08", t0.replace(minute=2)),
            _dated_row("m-2", "m", "2023-02-01", t0.replace(minute=3)),
        ]
    ]
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    events: list[tuple[str, str]] = []
    in_flight: dict[str, int] = {"n": 0, "m": 0}
    overlap_seen = {"value": False}

    async def _fake_handler(inp):
        ns = inp.namespace
        in_flight[ns] += 1
        assert in_flight[ns] == 1, "two batches of one namespace ran at once"
        if sum(in_flight.values()) > 1:
            overlap_seen["value"] = True
        events.append((ns, inp.observation_date))
        await asyncio.sleep(0.01)
        in_flight[ns] -= 1

        class _Out:
            written = [1]

        return _Out()

    from server.memory.app.extract import handler as extract_handler_mod

    monkeypatch.setattr(extract_handler_mod, "handle_memory_set_from_messages", _fake_handler)

    res = await worker.run_pass(worker.ExtractionQueueConfig(namespace_concurrency=4))

    assert res.done_count == 4 and not res.failures
    # Within each namespace, session-date order regardless of enqueue order.
    assert [d for ns, d in events if ns == "n"] == ["2023-05-08", "2023-06-01"]
    assert [d for ns, d in events if ns == "m"] == ["2023-01-01", "2023-02-01"]
    # The two namespaces overlapped in time.
    assert overlap_seen["value"] is True


@pytest.mark.asyncio
async def test_namespace_concurrency_one_is_fully_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    ch = StubCH()
    t0 = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    ch.query_all_returns = [
        [_dated_row("a", "n", "2023-01-01", t0), _dated_row("b", "m", "2023-01-01", t0)]
    ]
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    active = {"n": 0}

    async def _fake_handler(inp):
        active["n"] += 1
        assert active["n"] == 1
        await asyncio.sleep(0.005)
        active["n"] -= 1

        class _Out:
            written = []

        return _Out()

    from server.memory.app.extract import handler as extract_handler_mod

    monkeypatch.setattr(extract_handler_mod, "handle_memory_set_from_messages", _fake_handler)
    res = await worker.run_pass(worker.ExtractionQueueConfig(namespace_concurrency=1))
    assert res.done_count == 2


@pytest.mark.asyncio
async def test_one_group_raising_still_waits_for_every_other_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise past `_replay_one`'s own except (the `mark_done` insert failing)
    must not end the pass while another namespace is still replaying: the
    pass waits for every group, then fails with the first error."""
    import asyncio

    ch = StubCH()
    t0 = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    ch.query_all_returns = [
        [_dated_row("a-1", "a", "2023-01-01", t0), _dated_row("b-1", "b", "2023-01-01", t0)]
    ]
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _fake_handler(inp):
        if inp.namespace == "b":
            await asyncio.sleep(0.05)

        class _Out:
            written = [1]

        return _Out()

    from server.memory.app.extract import handler as extract_handler_mod

    monkeypatch.setattr(extract_handler_mod, "handle_memory_set_from_messages", _fake_handler)

    marked: list[str] = []
    real_mark_done = q.mark_done

    async def _mark_done(row, *, written, ch=None):
        if row["namespace"] == "a":
            raise RuntimeError("ch insert failed")
        await real_mark_done(row, written=written, ch=ch)
        marked.append(row["queue_id"])

    monkeypatch.setattr(q, "mark_done", _mark_done)

    with pytest.raises(RuntimeError, match="ch insert failed"):
        await worker.run_pass(worker.ExtractionQueueConfig(namespace_concurrency=4))

    assert marked == ["b-1"], "the pass returned before the slow group finished"
    status = get_registry().snapshot()["extraction_queue"]
    assert status["consecutive_failures"] == 1
    assert "ch insert failed" in status["last_error"]


def test_the_supervisor_pass_reads_its_config_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "extraction_queue_max_batches_per_pass", 5)
    monkeypatch.setattr(settings, "extraction_queue_lease_seconds", 120)
    monkeypatch.setattr(settings, "extraction_queue_max_attempts", 2)
    monkeypatch.setattr(settings, "extraction_queue_namespace_concurrency", 1)
    assert worker._config_from_settings() == worker.ExtractionQueueConfig(
        max_batches_per_pass=5, lease_seconds=120, max_attempts=2, namespace_concurrency=1
    )


# ---------------------------------------------------------------- held, backoff, TTL


def _replaying(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> StubCH:
    """A queue holding one claimed row (two attempts spent) whose replay raises `error`."""
    ch = StubCH()
    ch.query_all_returns = [[_stored_row(queue_id="q1", attempts=2)]]
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _fake_handler(inp: Any) -> Any:
        raise error

    from server.memory.app.extract import handler as extract_handler_mod

    monkeypatch.setattr(extract_handler_mod, "handle_memory_set_from_messages", _fake_handler)
    return ch


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


def test_a_failed_attempt_waits_15_s_then_a_minute_then_5_then_30() -> None:
    assert [q.backoff_s(n) for n in range(1, 8)] == [15, 60, 300, 1800, 1800, 1800, 1800]


@pytest.mark.asyncio
async def test_a_failed_attempt_is_claimed_again_only_after_its_backoff() -> None:
    ch = StubCH()
    before = datetime.now(UTC)
    row = _stored_row(status=q.STATUS_RUNNING, attempts=1)
    assert await q.mark_error(row, error="x", max_attempts=8, ch=ch) == q.STATUS_PENDING
    written = ch.inserts[-1][1][0]
    assert written["attempts"] == 2
    assert timedelta(seconds=59) < written["lease_until"] - before <= timedelta(seconds=61)
    last = _stored_row(status=q.STATUS_RUNNING, attempts=7)
    assert await q.mark_error(last, error="x", max_attempts=8, ch=ch) == q.STATUS_FAILED
    assert ch.inserts[-1][1][0]["lease_until"] == datetime(1970, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("no route to the gateway"),
        httpx.ReadTimeout("the gateway did not answer"),
        httpx.RemoteProtocolError("the answer was cut off"),
        GatewayUnavailable("the inference gateway's token could not be renewed"),
        TimeoutError(),
        _status_error(502),
        _status_error(503),
        _status_error(504),
    ],
)
async def test_an_unreachable_provider_spends_no_attempt(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    ch = _replaying(monkeypatch, error)
    before = datetime.now(UTC)
    res = await worker.run_pass(worker.ExtractionQueueConfig(max_attempts=3))
    written = ch.inserts[-1][1][0]
    assert (res.unreachable, res.failures) == (1, [])
    assert (written["status"], written["attempts"]) == (q.STATUS_PENDING, 2)
    assert written["last_error"].startswith("unreachable: ")
    # the wait the next attempt would have: the third's, 5 minutes
    assert timedelta(seconds=299) < written["lease_until"] - before <= timedelta(seconds=301)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [_status_error(500), httpx.UnsupportedProtocol("no such scheme: htps")]
)
async def test_a_failure_of_the_request_itself_still_spends_an_attempt(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    ch = _replaying(monkeypatch, error)
    res = await worker.run_pass(worker.ExtractionQueueConfig(max_attempts=8))
    written = ch.inserts[-1][1][0]
    assert res.unreachable == 0 and res.failures == ["q1: pending"]
    assert written["attempts"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "hold"),
    [
        (ModelCallRefused("refused", hold_s=600.0, gateway_status=429), 600),
        (ModelCallDeferred("wait", hold_s=1.0, gateway_status=429, retry_after_s=1), 1),
        (IdentityUnread("the identity headers could not read namespace o/n"), 30),
    ],
)
async def test_a_held_replay_is_marked_held_with_its_reason_until_its_hold_ends(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, hold: int
) -> None:
    ch = _replaying(monkeypatch, error)
    before = datetime.now(UTC)
    res = await worker.run_pass(worker.ExtractionQueueConfig(max_attempts=3))
    written = ch.inserts[-1][1][0]
    assert (res.skipped, res.failures) == (1, [])
    assert (written["status"], written["attempts"]) == (q.STATUS_HELD, 2)  # not `running`
    assert written["last_error"].startswith(f"held: {type(error).__name__}")
    left = written["lease_until"] - before
    assert timedelta(seconds=hold - 1) < left <= timedelta(seconds=hold + 1)


class _Queue:
    """loom.memory_extraction_queue as the claim reads it: each row's newest version,
    the waiting rows past their lease, less every enqueuer, namespace and user with
    a request held now, the oldest first, at most `lim`."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {r["queue_id"]: r for r in rows}
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.sql.append(sql)
        now = datetime.now(UTC)
        held = {
            _attribution(r)
            for r in self.rows.values()
            if r["status"] == q.STATUS_HELD and r["lease_until"] >= now
        }
        live = [
            r
            for r in self.rows.values()
            if r["status"] in params["waiting"]
            and r["lease_until"] < now
            and _attribution(r) not in held
        ]
        return [dict(r) for r in sorted(live, key=lambda r: r["enqueued_at"])[: params["lim"]]]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.rows.update({r["queue_id"]: r for r in rows})


def _attribution(r: dict[str, Any]) -> tuple[str, str, str, str]:
    return (r["org"], r["namespace"], r["user_id"], q.enqueued_actor(r))


def _queued(queue_id: str, by: str, minute: int, **kw: Any) -> dict[str, Any]:
    payload, _, _ = q.payload_for(_input(), scope=WHOLE_NAMESPACE, enqueued_by=by)
    enqueued = datetime(2026, 9, 12, 8, minute, tzinfo=UTC)
    return _stored_row(queue_id=queue_id, payload=payload, enqueued_at=enqueued, **kw)


@pytest.mark.asyncio
async def test_the_claim_takes_other_work_while_an_enqueuer_is_held() -> None:
    later = datetime.now(UTC) + timedelta(minutes=10)
    table = _Queue(
        [
            _queued("a-held", "process:sa:t:agent-a", 0, status=q.STATUS_HELD, lease_until=later),
            _queued("a-next", "process:sa:t:agent-a", 1),  # the oldest pending
            _queued("b-1", "process:sa:t:agent-b", 2),
            _queued("b-2", "process:sa:t:agent-b", 3),
        ]
    )
    claimed = await q.claim_batch(limit=1, lease_seconds=600, ch=table)
    assert [r["queue_id"] for r in claimed] == ["b-1"]  # not agent-a's, which would be held
    assert "WHERE status = {held:String} AND lease_until >= now64(3)" in table.sql[0]
    table.rows["a-held"]["lease_until"] = datetime.now(UTC) - timedelta(seconds=1)
    claimed = await q.claim_batch(limit=3, lease_seconds=600, ch=table)
    assert [r["queue_id"] for r in claimed] == ["a-held", "a-next", "b-2"]  # the hold is over


@pytest.mark.asyncio
async def test_a_request_still_waiting_on_its_last_day_is_dropped_with_a_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import drops

    ch = StubCH()
    held = _stored_row(queue_id="h", status=q.STATUS_HELD, last_error="held: refused")
    ch.query_all_returns = [[held, _stored_row(queue_id="p")]]
    recorded: list[tuple[list[Any], str, str]] = []

    async def write_drops(items: list[Any], stage: str, reason: str, *, ch: Any = None) -> None:
        recorded.append((items, stage, reason))

    monkeypatch.setattr(drops, "write_drops", write_drops)
    assert await q.drop_expiring(ch=ch) == 2
    sql, params = ch.query_all_calls[0]
    assert "toIntervalDay({days:UInt32})" in sql and params["days"] == q.TTL_DAYS - 1
    assert set(params["waiting"]) == {q.STATUS_PENDING, q.STATUS_RUNNING, q.STATUS_HELD}
    assert recorded == [
        ([("o", "n", "h")], "extraction_queue", "held_past_ttl"),
        ([("o", "n", "p")], "extraction_queue", "waited_past_ttl"),
    ]
    marked = ch.inserts[-1][1]
    assert [r["status"] for r in marked] == [q.STATUS_FAILED, q.STATUS_FAILED]
    assert "(held: refused)" in marked[0]["last_error"]


@pytest.mark.asyncio
async def test_held_batches_count_as_not_yet_extracted() -> None:
    ch = StubCH()
    ch.query_one_returns = [{"n": 3}]
    assert await q.pending_count(org="o", namespace="n", ch=ch) == 3
    sql, params = ch.query_one_calls[0]
    assert "status IN {waiting:Array(String)}" in sql and q.STATUS_HELD in params["waiting"]


def test_the_ttl_drop_follows_the_tables_ttl() -> None:
    from pathlib import Path

    ddl = (Path(__file__).resolve().parents[2] / "schemas" / "schema.sql").read_text()
    table = ddl.split("CREATE TABLE IF NOT EXISTS loom.memory_extraction_queue", 1)[1]
    assert f"TTL enqueued_at + toIntervalDay({q.TTL_DAYS})" in table.split(";", 1)[0]
