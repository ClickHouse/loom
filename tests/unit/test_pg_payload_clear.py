"""The payload clearing pass (`server/memory/workers/pg_payload_clear.py`): its horizon,
when it stays idle and what it clears."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from server.core import paths, pg, settings_overrides
from server.core.errors import BackendTimeoutError, BackendUnavailableError, OverloadedError
from server.memory.store import history, pg_memory
from server.memory.workers import pg_payload_clear
from server.settings import settings
from server.workers import pass_context
from tests.unit.clock_fake import ManualClock


def _revision(seq: int, *, sha: str = "a" * 64) -> pg_memory.Clearable:
    return pg_memory.Clearable(
        delivered=datetime(2026, 1, 2, tzinfo=UTC),
        org="o",
        namespace="n",
        seq=seq,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key=f"k{seq}",
        version=seq,
        memory_id=f"00000000-0000-0000-0000-{seq:012d}",
        content_sha256=sha,
    )


def test_the_index_predicate_is_the_clearing_reads_word_for_word() -> None:
    """The partial index serves the pass's reads only when they repeat its predicate."""
    migration = (paths.PG_MIGRATIONS_DIR / "010_revision_delivered_at.sql").read_text()
    assert f"WHERE {pg_memory.CLEARABLE};" in migration


def test_the_operator_delete_names_the_park_the_code_writes() -> None:
    """The operator's delete matches a nothing-to-send park by its `last_error` text: a
    reworded PAYLOAD_CLEARED would leave it deleting nothing."""
    page = paths.DOCS_DIR / "operations" / "postgres.md"
    assert f"last_error = '{pg_memory.PAYLOAD_CLEARED}'" in page.read_text()


@pytest.mark.parametrize(
    ("configured", "interval", "expected"),
    [
        (0, 1440, 2 * 1440 + 60),  # auto: covers one failed backup
        (10, 1440, 1440 + 60),  # under the backup interval plus the margin: raised
        (5000, 1440, 5000),
    ],
)
def test_the_horizon_is_never_under_the_backup_interval_plus_the_margin(
    monkeypatch: pytest.MonkeyPatch, configured: int, interval: int, expected: int
) -> None:
    monkeypatch.setattr(settings, "pg_payload_restore_horizon_minutes", configured)
    monkeypatch.setattr(settings, "ch_backup_interval_minutes", interval)
    monkeypatch.setattr(pg, "insert_bound_s", lambda: 60.0)
    assert pg_payload_clear.effective_horizon_minutes() == expected


def test_the_horizon_is_never_under_the_insert_bound_plus_the_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pg_payload_restore_horizon_minutes", 1)
    monkeypatch.setattr(settings, "ch_backup_interval_minutes", 1)
    monkeypatch.setattr(pg, "insert_bound_s", lambda: 3 * 3600.0 + 1)
    assert pg_payload_clear.effective_horizon_minutes() == 181 + 60


@pytest.fixture
def clearing(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A pass over fake stores: `found` is what the journal offers, `held` what
    ClickHouse's history holds; `cleared` records what the pass asks to clear."""
    state: dict[str, Any] = {"found": [], "held": None, "cleared": [], "after": [], "idle": 0}
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "insert_bound_s", lambda: 60.0)
    monkeypatch.setattr(settings_overrides, "_adopted", True)
    monkeypatch.setattr(settings, "pg_delivery_batch_rows", 2)
    # The cursor the supervisor keeps in loom.worker_status between passes.
    state["cursor"] = ""
    monkeypatch.setattr(pass_context, "resume_cursor", lambda: state["cursor"])
    monkeypatch.setattr(pass_context, "set_cursor", lambda v: state.__setitem__("cursor", v))

    async def _clearable(horizon_s: float, after: Any, limit: int) -> list[Any]:
        state["after"].append(after)
        return [r for r in state["found"] if after is None or r.cursor > after][:limit]

    async def _held(revisions: list[Any], **_: Any) -> list[Any]:
        held = state["held"]
        return revisions if held is None else [r for r in revisions if r.seq in held]

    async def _clear(horizon_s: float, held: list[Any]) -> int:
        if held:  # as the store: no statement for none
            state["cleared"].append([r.seq for r in held])
        return len(held)

    def _idle() -> None:
        state["idle"] += 1

    monkeypatch.setattr(pg_memory, "clearable_revisions", _clearable)
    monkeypatch.setattr(history, "versions_held", _held)
    monkeypatch.setattr(pg_memory, "clear_payloads", _clear)
    monkeypatch.setattr(pg_payload_clear.pass_context, "mark_idle", _idle)
    return state


def test_adopting_the_overrides_opens_the_clearing_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate the pass waits on is the one the overrides' adoption sets."""
    monkeypatch.setattr(settings_overrides, "_adopted", False)
    monkeypatch.setattr(settings_overrides, "_applied", {})
    assert not settings_overrides.adopted()
    settings_overrides.adopt({})
    assert settings_overrides.adopted()


async def test_a_process_that_has_not_adopted_the_overrides_clears_nothing(
    clearing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the overrides are adopted, a lowered backup interval or horizon an
    operator stored would not be in force yet."""
    monkeypatch.setattr(settings_overrides, "_adopted", False)
    clearing["found"] = [_revision(1)]
    result = await pg_payload_clear.run_pass()
    assert (result.cleared, clearing["after"]) == (0, [])


async def test_a_pass_clears_only_what_clickhouse_holds(clearing: dict[str, Any]) -> None:
    clearing["found"] = [_revision(1), _revision(2)]
    clearing["held"] = {2}
    result = await pg_payload_clear.run_pass()
    assert (result.cleared, result.missing_in_clickhouse) == (1, 1)
    assert clearing["cleared"] == [[2]]


async def test_a_pass_reads_full_batches_until_a_short_one_and_the_next_starts_over(
    clearing: dict[str, Any],
) -> None:
    clearing["found"] = [_revision(1), _revision(2), _revision(3)]
    result = await pg_payload_clear.run_pass()
    assert (result.cleared, clearing["cleared"]) == (3, [[1, 2], [3]])
    assert clearing["after"] == [None, _revision(2).cursor]
    await pg_payload_clear.run_pass()
    assert clearing["after"][2] is None


async def test_a_pass_ending_on_a_full_batch_reads_one_more_and_is_not_idle(
    clearing: dict[str, Any],
) -> None:
    clearing["found"] = [_revision(1), _revision(2)]
    await pg_payload_clear.run_pass()
    assert (clearing["cleared"], clearing["after"]) == ([[1, 2]], [None, _revision(2).cursor])
    assert clearing["idle"] == 0


async def test_a_pass_that_finds_nothing_is_idle(clearing: dict[str, Any]) -> None:
    await pg_payload_clear.run_pass()
    assert (clearing["cleared"], clearing["idle"]) == ([], 1)


async def test_a_later_batch_clickhouse_cannot_read_in_time_ends_the_pass_with_its_result(
    clearing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the retry budget, a batch after the first ends the pass with what it cleared,
    its cursor before that batch; the first would raise."""
    from server.workers import registry

    clearing["found"] = [_revision(1), _revision(2), _revision(3)]
    calls: list[int] = []
    clock = ManualClock()

    async def _second_fails(revisions: list[Any], **_: Any) -> list[Any]:
        calls.append(1)
        if len(calls) == 2:
            clock.advance(10)  # the read outlasts the budget
            raise BackendUnavailableError("ClickHouse is unavailable")
        return revisions

    monkeypatch.setattr(history, "versions_held", _second_fails)
    monkeypatch.setattr(registry, "pass_deadline_seconds", lambda _: 2.0)
    monkeypatch.setattr(pg_payload_clear, "_clock", clock)
    ctx = pass_context.begin("pg_payload_clear")
    try:
        result = await pg_payload_clear.run_pass()
    finally:
        pass_context.clear()
    assert len(calls) == 2
    assert (result.cleared, clearing["cleared"]) == (2, [[1, 2]])
    assert pg_payload_clear._cursor_of(clearing["cursor"]) == _revision(2).cursor
    # The supervisor reads the pass as failing.
    assert (ctx.count, ctx.failures[0][0]) == (1, "versions_held")


@pytest.mark.parametrize(
    "failure",
    [BackendTimeoutError("read past its deadline"), DatabaseError("Code: 47. Unknown column")],
)
async def test_a_clickhouse_failure_that_does_not_clear_by_itself_is_not_retried(
    clearing: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """A read that hung once, or a refusal, is not run again."""
    clearing["found"] = [_revision(1)]
    calls: list[int] = []
    clock = ManualClock()

    async def _fails(revisions: list[Any], **_: Any) -> list[Any]:
        calls.append(1)
        raise failure

    monkeypatch.setattr(history, "versions_held", _fails)
    monkeypatch.setattr(pg_payload_clear, "_clock", clock)
    monkeypatch.setattr(pg_payload_clear, "_sleep", clock.async_sleep)
    with pytest.raises(type(failure)):
        await pg_payload_clear.run_pass()
    assert (len(calls), clock.sleeps, clearing["cleared"]) == (1, [], [])


async def test_a_pass_stopped_during_its_clickhouse_read_clears_nothing(
    clearing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    clearing["found"] = [_revision(1)]
    ctx = pass_context.begin("pg_payload_clear")

    async def _stopped_meanwhile(revisions: list[Any], **_: Any) -> list[Any]:
        ctx.stopped.set()
        return revisions

    monkeypatch.setattr(history, "versions_held", _stopped_meanwhile)
    try:
        with pytest.raises(pass_context.PassStopped):
            await pg_payload_clear.run_pass()
    finally:
        pass_context.clear()
    assert clearing["cleared"] == []


async def test_a_stopped_pass_clears_nothing(clearing: dict[str, Any]) -> None:
    clearing["found"] = [_revision(1)]
    ctx = pass_context.begin("pg_payload_clear")
    ctx.stopped.set()
    try:
        with pytest.raises(pass_context.PassStopped):
            await pg_payload_clear.run_pass()
    finally:
        pass_context.clear()
    assert clearing["cleared"] == []


async def test_a_first_batch_clickhouse_cannot_read_in_time_fails_the_pass(
    clearing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.workers import registry

    clearing["found"] = [_revision(1)]
    clock = ManualClock()

    async def _down(revisions: list[Any], **_: Any) -> list[Any]:
        clock.advance(10)  # the read outlasts the budget
        raise BackendUnavailableError("ClickHouse is unavailable")

    monkeypatch.setattr(history, "versions_held", _down)
    monkeypatch.setattr(registry, "pass_deadline_seconds", lambda _: 2.0)
    monkeypatch.setattr(pg_payload_clear, "_clock", clock)
    with pytest.raises(BackendUnavailableError):
        await pg_payload_clear.run_pass()
    assert clearing["cleared"] == []


def test_the_cursor_reads_back_what_a_pass_left_and_starts_over_otherwise() -> None:
    """A pass in another process or colour resumes from the stored cursor; the end of the
    index, no cursor, or one this build cannot read starts from the beginning."""
    after = _revision(2).cursor
    assert pg_payload_clear._cursor_of(pg_payload_clear._cursor_text(after)) == after
    assert pg_payload_clear._cursor_text(None) != ""  # the supervisor drops an empty one
    for text in (pg_payload_clear._cursor_text(None), "", "[1, 2]", "not json"):
        assert pg_payload_clear._cursor_of(text) is None


def test_a_cursor_too_long_to_store_resumes_at_its_delivery_time() -> None:
    """The stored cursor holds MAX_CURSOR characters: one naming a long org and namespace
    keeps only its delivery time, so the next pass reads again only what was delivered
    at that instant, not the whole index."""
    long = dataclasses.replace(_revision(2), namespace="名" * 600).cursor
    text = pg_payload_clear._cursor_text(long)
    assert len(text) <= pass_context.MAX_CURSOR
    assert pg_payload_clear._cursor_of(text) == (long[0], "", "", -1)
    short = dataclasses.replace(_revision(2), namespace="名" * 50).cursor
    assert pg_payload_clear._cursor_of(pg_payload_clear._cursor_text(short)) == short


async def test_a_pass_out_of_time_stops_after_its_batch_and_the_next_resumes_past_it(
    clearing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.workers import registry

    monkeypatch.setattr(registry, "pass_deadline_seconds", lambda _: 0.0)
    clearing["found"] = [_revision(1), _revision(2), _revision(3)]
    await pg_payload_clear.run_pass()
    await pg_payload_clear.run_pass()
    assert clearing["after"] == [None, _revision(2).cursor]
    assert clearing["cleared"] == [[1, 2], [3]]


@pytest.mark.parametrize(
    "failure",
    [BackendUnavailableError("ClickHouse is unavailable"), OverloadedError("pool busy")],
)
async def test_a_clickhouse_failure_that_clears_by_itself_is_retried_within_the_pass(
    clearing: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    clearing["found"] = [_revision(1)]
    calls: list[int] = []
    clock = ManualClock()

    async def _flaky(revisions: list[Any], **_: Any) -> list[Any]:
        calls.append(1)
        if len(calls) == 1:
            raise failure
        return revisions

    monkeypatch.setattr(history, "versions_held", _flaky)
    monkeypatch.setattr(pg_payload_clear, "_clock", clock)
    monkeypatch.setattr(pg_payload_clear, "_sleep", clock.async_sleep)
    result = await pg_payload_clear.run_pass()
    assert (len(calls), result.cleared, clock.sleeps) == (2, 1, [pg_payload_clear._RETRY_WAIT_S])


class _History:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.params: dict[str, Any] = {}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.params = params or {}
        return self.rows


async def test_clickhouse_holds_a_version_only_with_its_id_and_content_hash() -> None:
    same, other_hash, other_id = _revision(1), _revision(2), _revision(3)
    rows = [
        {
            "org": r.org,
            "namespace": r.namespace,
            "agent": r.agent,
            "user_id": r.user_id,
            "session_id": r.session_id,
            "memory_key": r.memory_key,
            "version": r.version,
            "memory_id": mid,
            "sha": sha,
        }
        for r, mid, sha in (
            (same, same.memory_id, same.content_sha256),
            (other_hash, other_hash.memory_id, "b" * 64),
            (other_id, "00000000-0000-0000-0000-999999999999", other_id.content_sha256),
        )
    ]
    ch = _History(rows)
    assert await history.versions_held([same, other_hash, other_id], ch=ch) == [same]
    assert ch.params["keys"][0] == ("o", "n", "a", "-", "-", "k1", 1)


async def test_a_batch_too_large_for_one_parameter_is_checked_a_slice_at_a_time() -> None:
    """One bound parameter travels as one form field, which ClickHouse caps."""
    revisions = [
        dataclasses.replace(_revision(i), memory_key="k" * 400 + str(i)) for i in range(1, 501)
    ]
    ch = _Counting()
    assert await history.versions_held(revisions, ch=ch) == []
    assert ch.calls > 1
    assert sum(ch.sizes) == len(revisions)


class _Counting:
    def __init__(self) -> None:
        self.calls = 0
        self.sizes: list[int] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.calls += 1
        self.sizes.append(len((params or {})["keys"]))
        return []


def test_the_worker_is_switched_and_paced_by_its_own_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LOOM_PG_PAYLOAD_CLEAR_ENABLED` turns the worker on (off by default) and
    `LOOM_PG_PAYLOAD_CLEAR_INTERVAL_SECONDS` paces it."""
    from server.workers import registry

    (row,) = [w for w in registry._WORKERS if w.name == "pg_payload_clear"]
    assert (row.enabled, row.interval) == (
        "pg_payload_clear_enabled",
        "pg_payload_clear_interval_seconds",
    )
    assert settings.pg_payload_clear_enabled is False
    monkeypatch.setattr(settings, "pg_payload_clear_interval_seconds", 90)
    assert registry.worker_meta()["pg_payload_clear"]["interval_seconds"] == 90
