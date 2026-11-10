"""`loom reconcile` after a restore: what runs again, in which order, and what refuses."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.cli import restore as cli_restore
from server.erasure.app import replay
from server.erasure.app.saga_steps import NAMESPACE_STEPS, PG_ONLY_STEPS, USER_STEPS
from server.erasure.store.ledger import trusted_row as _trusted_row
from server.knowledge.app import restore_reconcile as knowledge_reconcile
from server.memory.store import pg_memory
from server.settings import settings
from tests.unit import clickhouse_local


def _names(steps: Any) -> set[str]:
    return {name for name, _ in steps}


async def _zero_knowledge(*a: Any, **k: Any) -> int:
    return 0


@pytest.fixture(autouse=True)
def _restored_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """No knowledge source held and no restored ClickHouse row, unless a case says so."""
    from server.erasure.store import ledger

    async def none(*a: Any, **k: Any) -> Any:
        return None

    async def no_sources() -> list[Any]:
        return []

    monkeypatch.setattr(ledger, "trusted_row", none)
    monkeypatch.setattr(knowledge_reconcile, "held_sources", no_sources)


@pytest.fixture(autouse=True)
def _registry_read(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """`reconcile` reads the code registry first; these cases read it."""
    from server.code.app.index import repo_registry

    answer = [True]

    async def read(ch: Any = None) -> bool:
        return answer[0]

    monkeypatch.setattr(repo_registry, "read_registry", read)
    return answer


@pytest.mark.parametrize(
    "scope_type, steps", [("user_id", USER_STEPS), ("namespace", NAMESPACE_STEPS)]
)
def test_a_clickhouse_restore_runs_every_step_but_the_postgres_ones(
    scope_type: str, steps: Any
) -> None:
    ran = _names(replay._replayed(scope_type, "clickhouse"))
    assert ran == _names(steps) - PG_ONLY_STEPS
    # No census: Postgres holds the user's later writes, which a census would count.
    assert "verify.residue" not in ran


@pytest.mark.parametrize(
    "scope_type, steps", [("user_id", USER_STEPS), ("namespace", NAMESPACE_STEPS)]
)
def test_a_postgres_restore_runs_only_the_postgres_steps(scope_type: str, steps: Any) -> None:
    ran = _names(replay._replayed(scope_type, "postgres"))
    # The replay answers' cache has a Postgres copy the restore brought back.
    assert ran == _names(steps) & replay.PG_STEPS
    assert PG_ONLY_STEPS <= ran and "idempotency.delete" in ran


def _row(**kw: Any) -> dict[str, Any]:
    return {"memory_type": "semantic", "sensitivity": "", **kw}


def test_a_republished_head_owes_its_document_or_the_retirement_below_it() -> None:
    base = pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW
    assert pg_memory.republish_targets(_row(), is_head=False) == base
    assert pg_memory.republish_targets(_row(), is_head=True) == base | pg_memory.SEARCH_DOC
    # A delete: whatever vectors ClickHouse restored below it are retired.
    deleted = _row(tombstoned_at="2026-01-01 00:00:00")
    targets = pg_memory.republish_targets(deleted, is_head=True)
    assert targets & pg_memory.OLD_VECTORS and not targets & pg_memory.SEARCH_DOC
    decision = _row(memory_type="decision", tombstoned_at="2026-01-01 00:00:00")
    assert pg_memory.republish_targets(decision, is_head=True) & pg_memory.DECISION_RECORD


class _CH:
    def __init__(self, live: int = 0) -> None:
        self.live = live
        self.params: Any = None

    async def query_one(self, sql: str, params: Any = None) -> dict[str, int]:
        assert "worker_status" in sql
        self.params = params
        return {"n": self.live}


def _now() -> datetime:
    return datetime.now(UTC)


def test_a_point_in_the_future_refuses() -> None:
    later = _now() + timedelta(hours=1)
    assert "later than now" in asyncio.run(
        cli_restore._refusal("clickhouse", later, live=True, ch=_CH())
    )


def test_both_stores_restored_refuses() -> None:
    why = asyncio.run(cli_restore._refusal("both", _now(), live=False, ch=_CH()))
    assert "audit export" in why


def test_a_point_older_than_the_ledger_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "erasure_ledger_days", 7)
    old = _now() - timedelta(days=8)
    assert "LOOM_ERASURE_LEDGER_DAYS" in asyncio.run(
        cli_restore._refusal("clickhouse", old, live=False, ch=_CH())
    )


def test_the_ledger_days_stop_inside_what_the_clickhouse_ledger_reads() -> None:
    """Its reads keep `LIVE_LEDGER`'s 89 days: a point past them would miss erasures."""
    from server.erasure.store.ledger import LIVE_LEDGER
    from server.settings.catalog import ErasureSettings

    field = ErasureSettings.model_fields["erasure_ledger_days"]
    most = next(m.le for m in field.metadata if getattr(m, "le", None) is not None)
    assert f"INTERVAL {most} DAY" in LIVE_LEDGER
    assert field.default <= most


def test_a_live_worker_refuses_unless_allowed() -> None:
    ch = _CH(live=1)
    assert "stop every server" in asyncio.run(
        cli_restore._refusal("postgres", _now(), live=False, ch=ch)
    )
    # A pass within two workers-lease terms counts as live.
    assert ch.params["s"] == 2 * settings.supervisor_lease_ttl_s
    assert asyncio.run(cli_restore._refusal("postgres", _now(), live=True, ch=_CH(live=1))) == ""


def test_a_clickhouse_restore_holds_then_erases_then_publishes_then_redacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing republished lands before the erasures it predates run again, and the
    redactions scrub what was published."""
    from server.memory.app import restore_reconcile

    order: list[str] = []

    async def completed_since(restored: str, after: datetime) -> list[dict[str, Any]]:
        return [{"id": "e1", "scope_type": "user_id"}]

    async def still_open(restored: str) -> list[dict[str, Any]]:
        return [{"id": "e2", "scope_type": "user_id"}]

    async def requeue(entry: Any, restored: str, point: datetime) -> None:
        order.append(f"requeue:{restored}")

    async def behind(ch: Any) -> int:
        order.append("behind")
        return 0

    async def documents_behind(ch: Any) -> int:
        order.append("documents_behind")
        return 0

    async def redactions(restored: str, after: datetime) -> list[dict[str, Any]]:
        return [{"memory_id": "m", "version": 1}]

    async def hold(after: datetime) -> int:
        order.append("hold")
        return 1

    class _Out:
        status = "completed"
        redacted = True
        refused_head = False

    async def run(entry: Any, restored: str, point: datetime) -> str:
        order.append(f"erase:{restored}")
        return "completed"

    async def publish() -> int:
        order.append("publish")
        return 1

    async def redact(entry: Any, restored: str) -> str:
        order.append("redact")
        return "redacted"

    monkeypatch.setattr(replay, "completed_since", completed_since)
    monkeypatch.setattr(replay, "still_open", still_open)
    monkeypatch.setattr(replay, "requeue", requeue)
    monkeypatch.setattr(restore_reconcile, "hold_heads_behind", behind)
    monkeypatch.setattr(knowledge_reconcile, "hold_documents_behind", documents_behind)
    monkeypatch.setattr(replay, "replay", run)
    monkeypatch.setattr(restore_reconcile, "redactions_lost_by", redactions)
    monkeypatch.setattr(restore_reconcile, "hold", hold)
    monkeypatch.setattr(restore_reconcile, "publish_held", publish)
    monkeypatch.setattr(knowledge_reconcile, "hold", _zero_knowledge)
    monkeypatch.setattr(knowledge_reconcile, "publish_held", _zero_knowledge)
    monkeypatch.setattr(restore_reconcile, "replay_redaction", redact)
    rc = asyncio.run(cli_restore.reconcile("clickhouse", _now(), apply=True, live=True, ch=_CH()))
    assert rc == 0
    assert order == [
        "hold",
        "behind",
        "documents_behind",
        "erase:clickhouse",
        "requeue:clickhouse",
        "publish",
        "redact",
    ]


def test_a_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import restore_reconcile

    async def none(*a: Any) -> list[Any]:
        return []

    async def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("a dry run wrote")

    monkeypatch.setattr(replay, "completed_since", none)
    monkeypatch.setattr(replay, "still_open", none)
    monkeypatch.setattr(restore_reconcile, "redactions_lost_by", none)
    monkeypatch.setattr(restore_reconcile, "hold", forbidden)
    monkeypatch.setattr(replay, "replay", forbidden)
    rc = asyncio.run(cli_restore.reconcile("clickhouse", _now(), apply=False, live=True, ch=_CH()))
    assert rc == 0


def test_the_ledger_row_is_written_before_the_clickhouse_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app import saga
    from server.erasure.store import pg_ledger

    order: list[str] = []

    async def record(row: Any) -> None:
        order.append("ledger")

    async def checkpoint(**kw: Any) -> None:
        order.append("checkpoint")

    async def emit(**kw: Any) -> None:
        return None

    async def verify() -> None:
        return None

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "verify_deployment", verify)
    monkeypatch.setattr(pg_ledger, "record", record)
    monkeypatch.setattr(saga, "checkpoint", checkpoint)
    monkeypatch.setattr(saga.audit, "emit", emit)
    asyncio.run(
        saga.begin_erasure(
            ErasureRequestInput(org="o", scope_type="user_id", scope_value="u", requester="r")
        )
    )
    assert order == ["ledger", "checkpoint"]


def _open(steps: list[str]) -> dict[str, Any]:
    return {
        "org": "o",
        "id": "00000000-0000-0000-0000-000000000001",
        "scope_type": "user_id",
        "scope_value": "u",
        "purge": True,
        "requested_at": datetime(2026, 1, 1),
        "steps_completed": steps,
        "attempts": 2,
    }


@pytest.mark.parametrize("restored", ["clickhouse", "postgres"])
def test_a_requeued_erasure_reopens_what_the_restore_undid(
    monkeypatch: pytest.MonkeyPatch, restored: str
) -> None:
    from server.erasure.store import pg_ledger

    written: list[dict[str, Any]] = []

    async def checkpoint(**kw: Any) -> None:
        written.append(kw)

    async def record(row: Any) -> None:
        return None

    async def marked(org: str, erasure_id: str) -> set[str]:
        return {"pg_memory.delete"}  # committed before the point: the copy has it

    monkeypatch.setattr(replay, "checkpoint", checkpoint)
    monkeypatch.setattr(pg_ledger, "record", record)
    monkeypatch.setattr(pg_ledger, "pg_steps", marked)
    done = ["pg_memory.delete", "pg_knowledge.delete", "idempotency.delete", "memory.tombstone"]
    asyncio.run(replay.requeue(_open(done), restored, _now()))
    (ck,) = written
    assert ck["status"] == "in_progress"
    # The resumed saga's rows name the worker, which a later reconcile does not trust.
    assert ck["row"]["actor"] == "erasure-recovery-worker"
    if restored == "clickhouse":
        assert ck["steps_completed"] == []  # ClickHouse lost them all: every step again
    else:
        # The restore undid the Postgres steps that committed after the point and the
        # replay answers' Postgres copy; the one the restored copy records stays done.
        assert ck["steps_completed"] == ["memory.tombstone", "pg_memory.delete"]
        assert ck["row"]["attempts"] == 2


def _steps(monkeypatch: pytest.MonkeyPatch, order: list[str], fail: bool = False) -> None:
    """Fake saga steps that record their name, scope and purge flag, or fail."""
    from server.core import namespace_write_marks
    from server.erasure.app import saga_steps

    def step(name: str) -> Any:
        async def run(org: str, scope: Any) -> int:
            order.append(name)
            if fail:
                raise RuntimeError("a mutation timed out")
            return 0

        return run

    async def bump(*a: Any) -> None:
        order.append("bump")

    for steps in ("USER_STEPS", "NAMESPACE_STEPS"):
        fakes = tuple((n, step(n)) for n, _ in getattr(saga_steps, steps))
        monkeypatch.setattr(replay, steps, fakes)
    monkeypatch.setattr(namespace_write_marks, "bump", bump)


def _completed(**kw: Any) -> dict[str, Any]:
    return {**_open([]), "completed_at": datetime(2026, 1, 2), **kw}


def test_a_postgres_replay_runs_only_the_postgres_steps_then_completes_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.store import pg_ledger

    order: list[str] = []

    async def record(row: Any) -> None:
        order.append("record")

    async def complete(org: str, erasure_id: str, at: datetime) -> None:
        order.append(f"complete:{at.isoformat()}")

    async def no_checkpoint(**kw: Any) -> None:
        raise AssertionError("a Postgres replay wrote a ClickHouse checkpoint")

    async def no_audit(**kw: Any) -> None:
        raise AssertionError("ClickHouse's audit trail already holds the erasure")

    async def none_marked(org: str, erasure_id: str) -> set[str]:
        return set()

    _steps(monkeypatch, order)
    monkeypatch.setattr(pg_ledger, "record", record)
    monkeypatch.setattr(pg_ledger, "pg_steps", none_marked)
    monkeypatch.setattr(pg_ledger, "complete", complete)
    monkeypatch.setattr(replay, "checkpoint", no_checkpoint)
    monkeypatch.setattr(replay.audit, "emit", no_audit)
    status = asyncio.run(replay.replay(_completed(), "postgres", _now()))
    assert status == "completed"
    # Recorded open, the steps, then completed at the time ClickHouse records.
    assert order[:2] == ["record", "bump"] and order[-1] == "complete:2026-01-02T00:00:00"
    assert set(order[2:-1]) == replay.PG_STEPS


def _clickhouse_replay(
    monkeypatch: pytest.MonkeyPatch, fail: bool, done: list[str] | None = None, **entry: Any
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    from server.erasure.store import ledger

    order: list[str] = []
    checkpoints: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    async def checkpoint(**kw: Any) -> None:
        checkpoints.append(kw)
        order.append(f"checkpoint:{kw['status']}")

    async def emit(**kw: Any) -> None:
        audits.append(kw)

    async def restored(org: str, erasure_id: str, **kw: Any) -> dict[str, Any] | None:
        if not done:
            return None
        return {"status": "in_progress", "completed_at": None, "done": set(done)}

    _steps(monkeypatch, order, fail=fail)
    monkeypatch.setattr(ledger, "trusted_row", restored)
    monkeypatch.setattr(replay, "checkpoint", checkpoint)
    monkeypatch.setattr(replay.audit, "emit", emit)
    asyncio.run(replay.replay(_completed(**entry), "clickhouse", _now()))
    return order, checkpoints, audits


def test_a_clickhouse_replay_is_pending_until_every_step_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.domain.failure_reason import REPLAY_PENDING_REASON

    order, checkpoints, audits = _clickhouse_replay(monkeypatch, fail=False)
    pending, done = checkpoints
    assert pending["status"] == "failed" and pending["failure_reason"] == REPLAY_PENDING_REASON
    assert done["status"] == "completed" and done["current_step"] == 99
    assert done["completed_at"] == datetime(2026, 1, 2)
    assert order[0] == "checkpoint:failed" and order[-1] == "checkpoint:completed"
    ((audit,),) = [audits]
    assert audit["event_type"] == "erasure.completed" and audit["actor"] == "loom-reconcile"


def test_a_clickhouse_replay_whose_step_fails_stays_pending_and_names_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its row stays the pending one, which the recovery worker never resumes, and no
    census runs: the reconcile reports the step and is run again."""
    from server.erasure.app.saga_steps import USER_STEPS, ErasureStepError

    first = next(n for n, _ in USER_STEPS if n not in PG_ONLY_STEPS)
    with pytest.raises(ErasureStepError) as e:
        _clickhouse_replay(monkeypatch, fail=True)
    assert e.value.step == first


@pytest.mark.parametrize(
    "scope_type, scope_value, purge, scope, purged",
    [
        ("user_id", "u", True, "u", True),
        ("namespace", "n", True, "n", False),  # a namespace's erasure never purges
        ("namespace_list", '{"namespaces":["a","b"],"purge":true}', False, ["a", "b"], True),
    ],
)
def test_the_replay_runs_each_step_over_the_scope_its_saga_did(
    monkeypatch: pytest.MonkeyPatch,
    scope_type: str,
    scope_value: str,
    purge: bool,
    scope: Any,
    purged: bool,
) -> None:
    from server.erasure.app import saga_steps
    from server.erasure.store import mutations

    seen: list[tuple[Any, bool, str]] = []

    def step(name: str) -> Any:
        async def run(org: str, s: Any) -> int:
            seen.append((s, mutations.purge_mode(), name))
            return 0

        return run

    async def none(**kw: Any) -> None:
        return None

    steps = saga_steps.USER_STEPS if scope_type == "user_id" else saga_steps.NAMESPACE_STEPS
    attr = "USER_STEPS" if scope_type == "user_id" else "NAMESPACE_STEPS"
    monkeypatch.setattr(replay, attr, tuple((n, step(n)) for n, _ in steps))
    monkeypatch.setattr(replay, "checkpoint", none)

    async def nothing_done(org: str, erasure_id: str, **kw: Any) -> None:
        return None

    from server.erasure.store import ledger

    monkeypatch.setattr(ledger, "trusted_row", nothing_done)
    monkeypatch.setattr(replay.audit, "emit", none)

    async def bump(*a: Any) -> None:
        return None

    from server.core import namespace_write_marks

    monkeypatch.setattr(namespace_write_marks, "bump", bump)
    entry = _completed(scope_type=scope_type, scope_value=scope_value, purge=purge)
    asyncio.run(replay.replay(entry, "clickhouse", _now()))
    assert seen and all(s == scope and p == purged for s, p, _ in seen)


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_no_ledger_read_that_resumes_or_requeues_returns_a_pending_replay() -> None:
    """Of a pending replay, an ordinary failed saga and an abandoned one, the recovery
    worker's reads and `still_open` return the ones they act on, never the pending one."""
    from server.erasure.domain.failure_reason import (
        ABANDONED_REASON_PREFIX,
        REPLAY_PENDING_REASON,
        caller_reason,
    )
    from server.erasure.store import ledger

    rows = {
        "00000000-0000-0000-0000-00000000000a": (REPLAY_PENDING_REASON, 1),
        "00000000-0000-0000-0000-00000000000b": ("memory.tombstone: TIMEOUT", 1),
        "00000000-0000-0000-0000-00000000000c": (f"{ABANDONED_REASON_PREFIX}5) exceeded", 5),
    }
    values = ", ".join(
        f"('{id}', 'o', 'user_id', 'u', 'failed', '{reason}', {attempts}, 1, "
        "now() - INTERVAL 1 HOUR)"
        for id, (reason, attempts) in rows.items()
    )
    setup = [
        clickhouse_local.ddl("erasure_requests"),
        "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, status, "
        f"failure_reason, attempts, version, last_checkpoint_at) VALUES {values}",
    ]

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            return clickhouse_local.run(setup, sql, params)

    def ids(found: list[Any]) -> list[str]:
        return sorted(str(r["id"])[-1] for r in found)

    reads = {"st": 60, "ma": 5, "bs": 10}
    stuck = ledger.list_stuck_in_progress(
        stale_threshold_seconds=reads["st"],
        max_attempts=reads["ma"],
        batch_size=reads["bs"],
        ch=_CH(),
    )
    exhausted = ledger.list_unabandoned_exhausted(
        stale_threshold_seconds=reads["st"], max_attempts=1, batch_size=reads["bs"], ch=_CH()
    )
    assert ids(asyncio.run(stuck)) == ["b"]
    assert ids(asyncio.run(exhausted)) == ["b"]  # the abandoned one is already terminal
    assert ids(asyncio.run(ledger.still_open(ch=_CH()))) == ["b"]
    # A requester reads the reason whole.
    assert caller_reason(REPLAY_PENDING_REASON) == REPLAY_PENDING_REASON


def test_re_verify_neither_counts_nor_resumes_a_pending_replay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its census would count the user's writes made after the erasure, and a resume
    would delete them: `loom reconcile` owns it."""
    import argparse

    from server.cli import hygiene
    from server.erasure.app import saga
    from server.erasure.domain.failure_reason import REPLAY_PENDING_REASON
    from server.erasure.store import census, ledger
    from server.erasure.store import health as erasure_health

    row = {**_open([]), "failure_reason": REPLAY_PENDING_REASON, "actor": "loom-reconcile"}

    async def failed(**kw: Any) -> list[Any]:
        return [row]

    async def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("re-verify counted or resumed a pending replay")

    monkeypatch.setattr(ledger, "list_failed_requests", failed)
    monkeypatch.setattr(census, "evidence_census", never)
    monkeypatch.setattr(saga, "run_saga", never)

    async def gauges(**kw: Any) -> dict[str, int]:
        return {"orphan_entity_vectors": 0, "orphan_graph_namespaces": 0}

    monkeypatch.setattr(erasure_health, "integrity_counts", gauges)
    args = argparse.Namespace(
        completed_since=None, org="", id=[], orphan_graph=False, resume=True, skip_external=False
    )
    assert asyncio.run(hygiene._re_verify_run(object(), args)) == 1
    assert REPLAY_PENDING_REASON in capsys.readouterr().out


def test_a_replay_skips_the_steps_the_restored_copy_shows_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The saga was still running at the point: a step done before it has its effect in
    the restored copy, and run again it would delete what the user wrote after it."""
    from server.erasure.app.saga_steps import USER_STEPS
    from server.erasure.store import pg_ledger

    ran = [n for n, _ in USER_STEPS if n not in PG_ONLY_STEPS]
    done = ran[:3]
    order, checkpoints, _ = _clickhouse_replay(monkeypatch, fail=False, done=done)
    assert checkpoints[0]["steps_completed"] == sorted(done)  # kept for a run again
    assert [n for n in order if n in ran] == ran[3:]

    order = []

    async def record(row: Any) -> None:
        return None

    async def marked(org: str, erasure_id: str) -> set[str]:
        return {"pg_memory.delete"}

    async def complete(*a: Any) -> None:
        return None

    _steps(monkeypatch, order)
    monkeypatch.setattr(pg_ledger, "record", record)
    monkeypatch.setattr(pg_ledger, "pg_steps", marked)
    monkeypatch.setattr(pg_ledger, "complete", complete)
    asyncio.run(replay.replay(_completed(), "postgres", _now()))
    assert set(order) - {"bump"} == replay.PG_STEPS - {"pg_memory.delete"}


def test_a_postgres_step_is_marked_in_the_transaction_it_commits() -> None:
    """`_mark_step` writes on the step's own connection, under the running saga and
    step; outside a saga it writes nothing."""
    from server.erasure.store import mutations, steps_pg

    class _Conn:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        async def execute(self, sql: str, *args: Any) -> str:
            self.calls.append((sql, args))
            return "UPDATE 1"

    conn = _Conn()
    asyncio.run(steps_pg._mark_step(conn, "o"))
    assert conn.calls == []
    saga = "00000000-0000-0000-0000-000000000001"
    with mutations.saga_scope(saga), mutations.step_scope("pg_memory.delete"):
        asyncio.run(steps_pg._mark_step(conn, "o"))
    ((sql, args),) = conn.calls
    assert "array_append(pg_steps" in sql
    assert args[0] == "o" and str(args[1]) == saga and args[2] == "pg_memory.delete"


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_an_erasure_completed_in_the_points_second_is_replayed() -> None:
    """`completed_at` keeps whole seconds: one stored at the point's second completed at
    or after it as often as before."""
    from server.erasure.store import ledger

    setup = [
        clickhouse_local.ddl("erasure_requests"),
        "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, status, "
        "completed_at, version) VALUES ('00000000-0000-0000-0000-00000000000a', 'o', "
        "'user_id', 'u', 'completed', '2026-01-02 12:00:00', 1), "
        "('00000000-0000-0000-0000-00000000000b', 'o', 'user_id', 'u', 'completed', "
        "'2026-01-02 11:59:59', 1)",
    ]

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
            return clickhouse_local.run(setup, sql, params)

    point = datetime(2026, 1, 2, 12, 0, 0, 500_000, tzinfo=UTC)
    rows = asyncio.run(ledger.completed_since(point, ch=_CH()))
    assert [str(r["id"])[-1] for r in rows] == ["a"]


@pytest.mark.parametrize("restored", ["clickhouse", "postgres"])
def test_a_requeue_keeps_the_postgres_steps_the_authority_records(
    monkeypatch: pytest.MonkeyPatch, restored: str
) -> None:
    """After a ClickHouse restore the live Postgres holds what its committed steps did;
    after a Postgres restore the copy's marks say which committed before the point, a
    mark landing before ClickHouse's checkpoint names its step."""
    from server.erasure.store import pg_ledger

    written: list[dict[str, Any]] = []

    async def checkpoint(**kw: Any) -> None:
        written.append(kw)

    async def record(row: Any) -> None:
        return None

    async def marked(org: str, erasure_id: str) -> set[str]:
        return {"pg_memory.delete", "pg_knowledge.delete"}

    monkeypatch.setattr(replay, "checkpoint", checkpoint)
    monkeypatch.setattr(pg_ledger, "record", record)
    monkeypatch.setattr(pg_ledger, "pg_steps", marked)
    entry = {**_open(["pg_memory.delete"]), "pg_steps": ["pg_memory.delete"]}
    asyncio.run(replay.requeue(entry, restored, _now()))  # type: ignore[arg-type]
    (ck,) = written
    if restored == "clickhouse":
        assert ck["steps_completed"] == ["pg_memory.delete"]
    else:  # marked in the copy, though ClickHouse's checkpoint had not named it yet
        assert ck["steps_completed"] == ["pg_knowledge.delete", "pg_memory.delete"]


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_restored_ledger_is_trusted_only_before_the_point_or_from_the_reconcile() -> None:
    """A `BACKUP` copies its tables one after another from its start: a row checkpointed
    after it may be restored without its deletes in a table copied earlier. A step a
    census reopened is not done."""
    from server.erasure.domain.failure_reason import ABANDONED_REASON_PREFIX

    def row(
        k: str,
        version: int,
        status: str,
        at: str,
        *,
        actor: str = "human:r",
        reason: str = "",
        steps: str = "[]",
        residue: str = "",
    ) -> str:
        return (
            f"('00000000-0000-0000-0000-00000000000{k}', 'o', 'user_id', 'u', '{status}', "
            f"'{reason}', '{at}', {steps}, '{residue}', '{actor}', {version}, '{at}')"
        )  # fmt: skip

    before, after = "2026-01-02 11:00:00", "2026-01-02 13:00:00"
    rows = [
        # a: running before the point, completed after it by the saga: not trusted.
        row("a", 1, "in_progress", before, steps="['memory.retire', 'agent_events.scrub']",
            residue='{"loom.memory": 2}'),
        row("a", 2, "completed", after),
        # b: completed by a run of the reconcile, after the point: trusted.
        row("b", 1, "completed", after, actor="loom-reconcile"),
        # c: abandoned before the point.
        row("c", 1, "failed", before, reason=f"{ABANDONED_REASON_PREFIX}5) exceeded"),
        # d: only after the point.
        row("d", 1, "completed", after),
        # e: a saga a reconcile requeued, which the recovery worker completed after the
        # point: its rows name the worker, and are not trusted.
        row("e", 1, "completed", after, actor="erasure-recovery-worker"),
    ]  # fmt: skip
    # One insert per row, merges stopped: each checkpoint is its own row, as the saga
    # writes them, until a merge keeps only the latest.
    setup = [
        clickhouse_local.ddl("erasure_requests"),
        "SYSTEM STOP MERGES loom.erasure_requests",
        *(
            "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, status, "
            "failure_reason, completed_at, steps_completed, residue, actor, version, "
            f"last_checkpoint_at) VALUES {r}"
            for r in rows
        ),
    ]

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            got = clickhouse_local.run(setup, sql, params)
            return got[0] if got else None

    def trusted(k: str) -> Any:
        # The real read: the autouse fixture stubs `ledger.trusted_row`.
        return asyncio.run(
            _trusted_row(
                "o",
                f"00000000-0000-0000-0000-00000000000{k}",
                before=datetime(2026, 1, 2, 12, 0, 0, 500_000),
                actor="loom-reconcile",
                ch=_CH(),
            )
        )

    a = trusted("a")
    assert a["status"] == "in_progress"
    assert a["done"] == {"agent_events.scrub"}  # the residue's owner runs again
    assert trusted("b")["status"] == "completed"
    assert trusted("c")["status"] == "abandoned"
    assert trusted("d") is None
    assert trusted("e") is None


def test_a_resumed_saga_registers_its_ledger_row_and_unmarks_what_its_census_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from server.core import pg
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app import saga
    from server.erasure.store import pg_ledger

    calls: list[Any] = []

    async def noop(*a: Any, **k: Any) -> None:
        return None

    async def record(row: Any) -> None:
        calls.append(("record", str(row["id"])))

    async def unmark(org: str, erasure_id: str, steps: Any) -> None:
        calls.append(("unmark", list(steps)))

    async def user_saga(
        org: str, user_id: str, *, completed: Any, row: Any, verify: Any
    ) -> list[Any]:
        calls.append(("steps", "pg_memory.delete" in completed))
        verify.update({"per_table": {}, "count": 0, "verified_at": datetime(2026, 1, 1)})
        return []

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "verify_deployment", noop)
    monkeypatch.setattr(pg_ledger, "record", record)
    monkeypatch.setattr(pg_ledger, "unmark_steps", unmark)
    monkeypatch.setattr(pg_ledger, "complete", noop)
    monkeypatch.setattr(saga, "_saga_user_id", user_saga)
    monkeypatch.setattr(saga, "checkpoint", noop)
    monkeypatch.setattr(saga.audit, "emit", noop)
    erasure_id = "00000000-0000-0000-0000-000000000001"
    asyncio.run(
        saga.run_saga(
            input=ErasureRequestInput(
                org="o", scope_type="user_id", scope_value="u", requester="r"
            ),
            row={"id": erasure_id, "org": "o", "actor": "human:r"},  # type: ignore[typeddict-item]
            erasure_id=erasure_id,
            requested_at=datetime(2026, 1, 1),
            previously_completed={"pg_memory.delete"},
            residue=json.dumps({"pg:loom.memory_current": 1}),
        )
    )
    assert calls == [("record", erasure_id), ("unmark", ["pg_memory.delete"]), ("steps", False)]


@pytest.mark.parametrize("ended", ["completed", "abandoned"])
def test_a_requeue_marks_the_ledger_row_of_an_erasure_the_restored_row_ended(
    monkeypatch: pytest.MonkeyPatch, ended: str
) -> None:
    """The Postgres ledger missed how the erasure ended (`saga._complete_ledger_row`,
    `recovery._abandon`): the restored ClickHouse row says so, and it is not run again."""
    from server.erasure.store import ledger, pg_ledger

    marked: list[Any] = []

    async def restored(org: str, erasure_id: str, **kw: Any) -> dict[str, Any]:
        return {"status": ended, "completed_at": datetime(2026, 1, 2), "done": set()}

    def mark(name: str) -> Any:
        async def run(org: str, erasure_id: str, at: datetime) -> None:
            marked.append((name, at))

        return run

    async def no_checkpoint(**kw: Any) -> None:
        raise AssertionError("requeued an erasure that ended")

    monkeypatch.setattr(ledger, "trusted_row", restored)
    monkeypatch.setattr(pg_ledger, "complete", mark("completed"))
    monkeypatch.setattr(pg_ledger, "abandon", mark("abandoned"))
    monkeypatch.setattr(replay, "checkpoint", no_checkpoint)
    asyncio.run(replay.requeue(_open([]), "clickhouse", _now()))  # type: ignore[arg-type]
    assert marked == [(ended, datetime(2026, 1, 2))]


def test_a_clickhouse_restore_names_each_knowledge_namespace_to_rechunk(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A document's chunks live in ClickHouse alone, so those of a held head are gone."""
    from server.memory.app import restore_reconcile

    async def none(*a: Any) -> list[Any]:
        return []

    async def held(*a: Any) -> int:
        return 2

    async def zero(*a: Any) -> int:
        return 0

    async def published() -> int:
        return 2

    async def sources() -> list[dict[str, Any]]:
        many = [{"org": "o", "namespace": "big", "source_uri": f"kb://{i}"} for i in range(21)]
        odd = {"org": "o", "namespace": "docs", "source_uri": "https://h/d?id=1&rev=2 x"}
        return [{"org": "o", "namespace": "docs", "source_uri": "kb://a"}, odd, *many]

    monkeypatch.setattr(replay, "completed_since", none)
    monkeypatch.setattr(replay, "still_open", none)
    monkeypatch.setattr(restore_reconcile, "redactions_lost_by", none)
    monkeypatch.setattr(restore_reconcile, "hold", held)
    monkeypatch.setattr(restore_reconcile, "hold_heads_behind", zero)
    monkeypatch.setattr(knowledge_reconcile, "hold_documents_behind", zero)
    monkeypatch.setattr(restore_reconcile, "publish_held", published)
    monkeypatch.setattr(knowledge_reconcile, "hold", _zero_knowledge)
    monkeypatch.setattr(knowledge_reconcile, "publish_held", _zero_knowledge)
    monkeypatch.setattr(knowledge_reconcile, "held_sources", sources)
    rc = asyncio.run(cli_restore.reconcile("clickhouse", _now(), apply=True, live=True, ch=_CH()))
    assert rc == 0
    out = capsys.readouterr().out
    # A few sources one by one; a namespace with many, whole, saying so.
    assert "loom rechunk --org o --namespace docs --source-uri kb://a --apply" in out
    # Quoted for the shell the operator pastes it into.
    assert "--source-uri 'https://h/d?id=1&rev=2 x' --apply" in out
    assert "loom rechunk --org o --namespace big --apply  (21 sources;" in out


def test_an_unread_code_registry_refuses_before_any_write(
    monkeypatch: pytest.MonkeyPatch, _registry_read: list[bool]
) -> None:
    from server.memory.app import restore_reconcile

    async def none(*a: Any) -> list[Any]:
        return []

    async def no_write(*a: Any, **k: Any) -> Any:
        raise AssertionError("wrote before the registry was read")

    monkeypatch.setattr(replay, "completed_since", none)
    monkeypatch.setattr(replay, "still_open", none)
    monkeypatch.setattr(restore_reconcile, "redactions_lost_by", none)
    monkeypatch.setattr(restore_reconcile, "hold", no_write)
    _registry_read[0] = False
    rc = asyncio.run(cli_restore.reconcile("clickhouse", _now(), apply=True, live=True, ch=_CH()))
    assert rc == 2


def test_erasures_are_read_from_the_point_and_revisions_from_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erasure completed before the point is in the restored store: running it again
    would delete what the user wrote after it. Revisions and redactions are read from a
    margin earlier."""
    from server.memory.app import restore_reconcile

    seen: dict[str, datetime] = {}

    async def erasures(restored: str, after: datetime) -> list[Any]:
        seen["erasures"] = after
        return []

    async def redactions(restored: str, after: datetime) -> list[Any]:
        seen["redactions"] = after
        return []

    async def none(*a: Any) -> list[Any]:
        return []

    monkeypatch.setattr(replay, "completed_since", erasures)
    monkeypatch.setattr(replay, "still_open", none)
    monkeypatch.setattr(restore_reconcile, "redactions_lost_by", redactions)
    point = _now()
    assert asyncio.run(cli_restore.reconcile("postgres", point, apply=False, ch=_CH())) == 0
    assert seen["erasures"] == point
    assert seen["redactions"] == point - cli_restore.margin()


def test_a_failed_step_does_not_stop_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """What `hold` held is published, and the redactions run, whatever an erasure did."""
    from server.memory.app import restore_reconcile

    order: list[str] = []

    async def completed_since(restored: str, after: datetime) -> list[dict[str, Any]]:
        return [{"id": "e1", "scope_type": "user_id"}]

    async def none(*a: Any) -> list[Any]:
        return []

    async def boom(entry: Any, restored: str, point: datetime) -> str:
        raise RuntimeError("the step failed")

    async def held(*a: Any) -> int:
        order.append("held")
        return 0

    async def zero(ch: Any) -> int:
        return 0

    async def publish() -> int:
        order.append("publish")
        return 0

    async def redactions(restored: str, after: datetime) -> list[dict[str, Any]]:
        return [{"memory_id": "m", "version": 1}]

    async def redact(entry: Any, restored: str) -> str:
        order.append("redact")
        return "redacted"

    monkeypatch.setattr(replay, "completed_since", completed_since)
    monkeypatch.setattr(replay, "still_open", none)
    monkeypatch.setattr(replay, "replay", boom)
    monkeypatch.setattr(restore_reconcile, "hold", held)
    monkeypatch.setattr(restore_reconcile, "hold_heads_behind", zero)
    monkeypatch.setattr(knowledge_reconcile, "hold_documents_behind", zero)
    monkeypatch.setattr(restore_reconcile, "publish_held", publish)
    monkeypatch.setattr(knowledge_reconcile, "hold", _zero_knowledge)
    monkeypatch.setattr(knowledge_reconcile, "publish_held", _zero_knowledge)
    monkeypatch.setattr(restore_reconcile, "redactions_lost_by", redactions)
    monkeypatch.setattr(restore_reconcile, "replay_redaction", redact)
    rc = asyncio.run(cli_restore.reconcile("clickhouse", _now(), apply=True, live=True, ch=_CH()))
    assert rc == 1
    assert order == ["held", "publish", "redact"]


def test_the_recovery_pass_keeps_the_postgres_ledger_and_marks_an_abandonment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each pass copies ClickHouse's ledger in and sweeps old rows, counting both, and a
    failure there is noted without stopping the pass; an abandoned saga's row is marked
    abandoned, so a reconcile never runs it again."""
    from server.core import pg
    from server.erasure.app import saga
    from server.erasure.store import pg_ledger
    from server.erasure.workers import recovery

    calls: list[Any] = []

    async def copied(limit: int) -> int:
        return 3

    async def swept(limit: int) -> int:
        return 1

    async def noop(*a: Any, **k: Any) -> None:
        return None

    async def abandon(org: str, erasure_id: str, at: datetime) -> None:
        calls.append(("abandon", org, erasure_id))

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_ledger, "backfill", copied)
    monkeypatch.setattr(pg_ledger, "sweep", swept)
    monkeypatch.setattr(pg_ledger, "abandon", abandon)
    monkeypatch.setattr(recovery.audit, "emit", noop)
    monkeypatch.setattr(saga, "checkpoint", noop)
    result, cfg = recovery.RecoveryResult(), recovery.RecoveryConfig()
    asyncio.run(recovery._keep_pg_ledger(result, cfg))
    assert (result.ledger_copied, result.ledger_swept) == (3, 1)

    async def down(limit: int) -> int:
        raise ConnectionError("the authority is unreachable")

    noted: list[str] = []
    monkeypatch.setattr(pg_ledger, "backfill", down)
    monkeypatch.setattr(recovery.pass_context, "note_failure", lambda e, what: noted.append(what))
    asyncio.run(recovery._keep_pg_ledger(recovery.RecoveryResult(), cfg))
    assert noted == ["erasure.ledger"]

    row = {
        **_open(["memory.tombstone"]),
        "actor": "human:r",
        "requester": "r",
        "urgency": "normal",
        "failure_reason": "",
    }
    asyncio.run(
        recovery._abandon(
            row=row,  # type: ignore[arg-type]
            cfg=cfg,
            result=result,
            attempts=5,
            last_error="",
            completed={"memory.tombstone"},
            verify={},
        )
    )
    assert calls == [("abandon", "o", row["id"])]

    async def ledger_down(*a: Any) -> None:
        raise ConnectionError("the authority is unreachable")

    noted.clear()
    monkeypatch.setattr(pg_ledger, "abandon", ledger_down)
    asyncio.run(
        recovery._abandon(
            row=row,  # type: ignore[arg-type]
            cfg=cfg,
            result=result,
            attempts=5,
            last_error="",
            completed={"memory.tombstone"},
            verify={},
        )
    )
    assert noted == ["erasure.ledger"]  # noted; the next pass's `backfill` copies it


def test_a_request_whose_first_checkpoint_fails_closes_its_ledger_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its caller is told it failed: left open, a reconcile after a ClickHouse restore
    would find it and run it; marked ended, a saga that resumes it could never close it."""
    from server.core import pg
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app import saga
    from server.erasure.store import pg_ledger

    calls: list[str] = []

    async def record(row: Any) -> None:
        calls.append("record")

    async def forget(org: str, erasure_id: str) -> None:
        calls.append("forget")

    async def checkpoint(**kw: Any) -> None:
        raise ConnectionError("ClickHouse refused the insert")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_ledger, "record", record)
    monkeypatch.setattr(pg_ledger, "forget", forget)
    monkeypatch.setattr(saga, "checkpoint", checkpoint)
    with pytest.raises(ConnectionError):
        asyncio.run(
            saga.begin_erasure(
                ErasureRequestInput(org="o", scope_type="user_id", scope_value="u", requester="r")
            )
        )
    # Deleted, not ended: the insert may have landed, and a resumed saga records the row
    # again (`run_saga`), which its completion then closes.
    assert calls == ["record", "forget"]


def test_a_replay_runs_nothing_for_an_erasure_the_restored_row_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every step ran before the point, or an earlier run of the reconcile replayed it:
    no pending row, no steps, no second audit row."""
    from server.erasure.store import ledger

    async def restored(org: str, erasure_id: str, **kw: Any) -> dict[str, Any]:
        return {"status": "completed", "completed_at": datetime(2026, 1, 2), "done": set()}

    async def nothing(**kw: Any) -> None:
        raise AssertionError("replayed an erasure the restored row completed")

    monkeypatch.setattr(ledger, "trusted_row", restored)
    monkeypatch.setattr(replay, "checkpoint", nothing)
    monkeypatch.setattr(replay.audit, "emit", nothing)
    assert asyncio.run(replay.replay(_completed(), "clickhouse", _now())) == "completed"  # type: ignore[arg-type]


def test_the_ledger_sweep_runs_after_a_copy_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg
    from server.erasure.store import pg_ledger
    from server.erasure.workers import recovery

    async def down(limit: int) -> int:
        raise ConnectionError("ClickHouse is unreachable")

    async def swept(limit: int) -> int:
        return 4

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_ledger, "backfill", down)
    monkeypatch.setattr(pg_ledger, "sweep", swept)
    monkeypatch.setattr(recovery.pass_context, "note_failure", lambda e, what: None)
    result = recovery.RecoveryResult()
    asyncio.run(recovery._keep_pg_ledger(result, recovery.RecoveryConfig()))
    assert result.ledger_swept == 4


def test_the_heads_clickhouse_lacks_or_holds_older_are_held_page_by_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory and knowledge alike: a head ClickHouse holds at an older version, or not at
    all, is held; one it holds at the same version is not; the next page starts after
    the last key of the one before."""
    from server.knowledge.store import pg_documents
    from server.memory.app import restore_reconcile
    from server.memory.store.rows import identity_key

    def head(key: str, version: int) -> dict[str, Any]:
        return {
            "org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "-",
            "memory_key": key, "version": version, "seq": version, "memory_type": "semantic",
            "sensitivity": "",
        }  # fmt: skip

    pages = [[head("k1", 2), head("k2", 3)], [head("k3", 4)], []]
    afters: list[Any] = []

    async def page(after: Any, limit: int) -> list[dict[str, Any]]:
        afters.append(after)
        return pages[len(afters) - 1]

    held: list[Any] = []

    async def hold(seqs: Any, targets: Any) -> None:
        held.extend(zip(seqs, targets, strict=True))

    class _Published:
        """ClickHouse holds k1 at the same version, k2 at an older one, k3 not at all."""

        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            got = {"k1": 2, "k2": 1}
            return [
                {**dict(zip(("org", "namespace", "agent", "user_id", "session_id", "memory_key"), k, strict=True)), "v": got[k[5]]}
                for k in params["keys"]
                if k[5] in got
            ]  # fmt: skip

    monkeypatch.setattr(pg_memory, "head_page", page)
    monkeypatch.setattr(pg_memory, "hold", hold)
    assert asyncio.run(restore_reconcile.hold_heads_behind(_Published(), page=2)) == 2
    assert [s for s, _ in held] == [("o", "n", 3), ("o", "n", 4)]
    assert all(t == pg_memory.republish_targets(head("k", 1), is_head=True) for _, t in held)
    assert afters == [None, identity_key(head("k2", 3)), identity_key(head("k3", 4))]

    docs = [[{"org": "o", "namespace": "n", "source_uri": u, "version": v}
             for u, v in (("kb://a", 2), ("kb://b", 3))], []]  # fmt: skip
    doc_afters: list[Any] = []

    async def doc_page(after: Any, limit: int) -> list[dict[str, Any]]:
        doc_afters.append(after)
        return docs[len(doc_afters) - 1]

    held_docs: list[Any] = []

    async def hold_heads(keys: Any) -> int:
        held_docs.extend(keys)
        return len(keys)

    class _Documents:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return [{"org": "o", "namespace": "n", "source_uri": "kb://a", "v": 2}]

    monkeypatch.setattr(pg_documents, "head_page", doc_page)
    monkeypatch.setattr(pg_documents, "hold_heads", hold_heads)
    assert asyncio.run(knowledge_reconcile.hold_documents_behind(_Documents(), page=2)) == 1
    assert held_docs == [("o", "n", "kb://b")]
    assert doc_afters == [None, ("o", "n", "kb://b")]
