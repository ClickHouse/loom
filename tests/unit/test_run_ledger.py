"""run.begin / run.complete / run.get / run.list — the agent run ledger.

Handlers run against a scripted fake ClickHouse; usage events are captured
at `memory_repo.record_usage_event`. Pins the run ledger's contract:
one row per run, `started_at` preserved across begin→complete, `used`
events for memories_used (plus `evidence` only on a positive outcome), and
the derived views (written / observed / recalled / cost) read live.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from server.core.errors import NotFoundError
from server.tasks.api.models_run import RunBeginInput, RunCompleteInput, RunGetInput, RunListInput
from server.tasks.store import runs as runs_store

T0 = datetime(2026, 9, 11, 12, 0, 0)


class _FakeCH:
    """Scripted CH: `one` and `all` are FIFO answers; every call is recorded."""

    def __init__(self) -> None:
        self.one: list[dict[str, Any] | None] = []
        self.all: list[list[dict[str, Any]]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql, params=None):
        self.calls.append(("one", sql, params or {}))
        return self.one.pop(0) if self.one else None

    async def query_all(self, sql, params=None):
        self.calls.append(("all", sql, params or {}))
        return self.all.pop(0) if self.all else []

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows))

    async def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params or {}))
        if self.execute_error is not None:
            raise self.execute_error

    execute_error: Exception | None = None


def _run_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "agent": "claude",
        "user_id": "-",
        "session_id": "s1",
        "run_id": "r1",
        "goal": "fix CI",
        "user_request": "CI is red",
        "expected_outcome": "",
        "environment": "",
        "model": "",
        "consumer": "",
        "status": "active",
        "started_at": T0,
        "completed_at": None,
        "duration_ms": 0,
        "final_response": "",
        "confidence": 0.0,
        "memories_used": [],
        "memories_written": [],
        "metadata": '{"k": 1}',
    }
    row.update(over)
    return row


@pytest.fixture
def env(monkeypatch):
    from server.memory import store as memory_repo
    from server.tasks.app import run as h

    fake = _FakeCH()
    events: list[dict[str, Any]] = []

    async def record(**kw):
        events.append(kw)

    monkeypatch.setattr(memory_repo, "record_usage_event", record)
    monkeypatch.setattr(h, "_now", lambda: datetime(2026, 9, 11, 12, 0, 42, 500000))
    with patch.object(h, "get_client", return_value=fake):
        yield fake, events, h


def test_begin_mints_a_run_id_and_inserts_an_active_row(env) -> None:
    fake, _, h = env
    fake.one.append(None)  # no existing row
    out = asyncio.run(
        h.handle_run_begin(
            RunBeginInput(
                org="o",
                namespace="n",
                agent="claude",
                session_id="s1",
                goal="fix CI",
                user_request="CI is red",
                metadata={"pr": 7},
            )
        )
    )
    assert len(out.run_id) == 32 and out.status == "active"
    assert out.run_id in out.next and "run.complete" in out.next and "memories_used" in out.next
    table, rows = fake.inserted[0]
    assert table == "loom.agent_runs" and len(rows) == 1
    r = rows[0]
    assert r["status"] == "active" and r["session_id"] == "s1" and r["goal"] == "fix CI"
    assert r["metadata"] == '{"pr":7}' and r["completed_at"] is None
    assert set(r) == set(runs_store.RUN_COLUMNS)


def test_begin_with_an_existing_run_id_keeps_the_original_start(env) -> None:
    fake, _, h = env
    fake.one.append(_run_row(started_at=T0))
    out = asyncio.run(
        h.handle_run_begin(
            RunBeginInput(
                org="o", namespace="n", agent="claude", run_id="r1", goal="fix CI (retry)"
            )
        )
    )
    assert out.run_id == "r1"
    assert fake.inserted[0][1][0]["started_at"] == T0  # preserved, not now()


def test_complete_unknown_run_is_not_found(env) -> None:
    fake, _, h = env
    fake.one.append(None)
    with pytest.raises(NotFoundError):
        asyncio.run(
            h.handle_run_complete(
                RunCompleteInput(org="o", namespace="n", run_id="nope", status="completed")
            )
        )
    assert fake.inserted == []


def _complete_r1(h) -> Any:
    return asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(org="o", namespace="n", run_id="r1", status="completed")
        )
    )


def test_complete_drops_the_runs_claim_after_closing_it(env, monkeypatch) -> None:
    fake, _, h = env
    monkeypatch.setattr(runs_store, "_claims_atomic", True)
    fake.one.append(_run_row())
    _complete_r1(h)
    drops = [c for c in fake.calls if c[0] == "execute" and "agent_run_claims DELETE" in c[1]]
    assert [c[2] for c in drops] == [{"k": '["o", "n", "r1"]'}]
    assert fake.inserted, "the closed row lands before the claim goes"


def test_a_failed_claim_drop_does_not_fail_the_close(env, monkeypatch) -> None:
    fake, _, h = env
    monkeypatch.setattr(runs_store, "_claims_atomic", True)
    fake.execute_error = RuntimeError("Keeper unavailable")
    fake.one.append(_run_row())
    assert _complete_r1(h).status == "completed"


def test_complete_credits_used_memories_and_positive_evidence(env, monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "search_trace_log_enabled", True)
    fake, events, h = env
    fake.one.append(_run_row())  # existing run
    fake.all.append(
        [  # key -> id (dup id)
            {"memory_id": "id-a", "memory_key": "ci:flaky"},
            {"memory_id": "id-a", "memory_key": "ci:flaky"},
            {"memory_id": "id-b", "memory_key": "runner:image"},
        ]
    )
    fake.all.append([{"memory_key": "ci:fix:2026-09-11"}])  # written under run
    fake.one.append({"n": 4})  # observations
    fake.all.append([{"memory_key": "ci:flaky"}, {"memory_key": "x"}])  # recalled
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o",
                namespace="n",
                run_id="r1",
                status="completed",
                final_response="Pinned the runner image.",
                confidence=0.9,
                memories_used=["ci:flaky", "runner:image", "ci:flaky", " "],
            )
        )
    )
    assert out.status == "completed" and out.duration_ms == 42_500  # from started_at
    assert out.used_applied == 2 and out.evidence_applied == 2
    assert out.memories_written == ["ci:fix:2026-09-11"]
    assert out.observations == 4 and out.recalled == 2
    kinds = sorted((e["memory_id"], e["event_type"]) for e in events)
    assert kinds == [("id-a", "evidence"), ("id-a", "used"), ("id-b", "evidence"), ("id-b", "used")]
    assert all(e["agent"] == "claude" for e in events)
    # the terminal row replaces the active one: same run_id + started_at.
    table, rows = fake.inserted[-1]
    r = rows[0]
    assert (r["run_id"], r["started_at"], r["status"]) == ("r1", T0, "completed")
    assert r["memories_used"] == ["ci:flaky", "runner:image"]
    assert r["memories_written"] == ["ci:fix:2026-09-11"]
    assert r["final_response"] == "Pinned the runner image." and r["confidence"] == 0.9
    assert r["completed_at"] is not None and r["metadata"] == '{"k":1}'


def test_failed_run_records_used_and_harm_but_no_evidence(env) -> None:
    """A failed run costs the memories it used: `used` still
    says the agent relied on the row, `harm` says the run ended badly, and no
    `evidence` is earned. Only memories_used are charged — the recalled set
    is never touched."""
    fake, events, h = env
    fake.one.append(_run_row())
    fake.all.append([{"memory_id": "id-a", "memory_key": "ci:flaky"}])
    fake.all.append([])  # written
    fake.one.append({"n": 0})  # observations
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o", namespace="n", run_id="r1", status="failed", memories_used=["ci:flaky"]
            )
        )
    )
    assert out.used_applied == 1 and out.evidence_applied == 0 and out.harm_applied == 1
    assert [e["event_type"] for e in events] == ["used", "harm"]
    assert all(e["memory_id"] == "id-a" and e["agent"] == "claude" for e in events)
    assert out.recalled == -1  # trace log off: absence of telemetry is not zero


def test_complete_labels_the_session_with_the_client_outcome(env) -> None:
    """The client's outcome is the session's label too — written
    as source=client so it outranks a later judged reading. `none` and a run
    without a session write nothing."""
    fake, events, h = env
    fake.one.append(_run_row(confidence=0.0))
    fake.all.append([])  # used keys (none)
    fake.all.append([])  # written
    fake.one.append({"n": 0})  # observations
    asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(org="o", namespace="n", run_id="r1", status="failed")
        )
    )
    labels = [(t, rows[0]) for t, rows in fake.inserted if t == "loom.session_outcomes_by_user"]
    assert len(labels) == 1
    row = labels[0][1]
    assert (row["session_id"], row["outcome"], row["source"], row["run_id"]) == (
        "s1",
        "negative",
        "client",
        "r1",
    )
    assert row["confidence"] == 1.0 and row["agent"] == "claude"

    fake.inserted.clear()
    fake.one.append(_run_row(session_id="-"))
    fake.all.append([])
    fake.all.append([])
    fake.one.append({"n": 0})
    asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(org="o", namespace="n", run_id="r1", status="completed")
        )
    )
    assert all(t != "loom.session_outcomes_by_user" for t, _ in fake.inserted)


def test_cancelled_run_records_used_only(env) -> None:
    """outcome none (cancelled, or the plugin's SessionEnd close): neither
    evidence nor harm — the hook knows the run ended, not how."""
    fake, events, h = env
    fake.one.append(_run_row())
    fake.all.append([{"memory_id": "id-a", "memory_key": "ci:flaky"}])
    fake.all.append([])
    fake.one.append({"n": 0})
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o", namespace="n", run_id="r1", status="cancelled", memories_used=["ci:flaky"]
            )
        )
    )
    assert (out.used_applied, out.evidence_applied, out.harm_applied) == (1, 0, 0)
    assert [e["event_type"] for e in events] == ["used"]
    assert all(
        t != "loom.session_outcomes_by_user" for t, _ in fake.inserted
    )  # `none` labels nothing


def test_outcome_override_and_explicit_duration(env) -> None:
    fake, events, h = env
    fake.one.append(_run_row())
    fake.all.append([{"memory_id": "id-a", "memory_key": "k"}])
    fake.all.append([])
    fake.one.append({"n": 0})
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o",
                namespace="n",
                run_id="r1",
                status="completed",
                memories_used=["k"],
                outcome="none",
                duration_ms=1234,
                weight=3,
            )
        )
    )
    assert out.duration_ms == 1234
    assert [(e["event_type"], e["event_count"]) for e in events] == [("used", 3)]


def test_get_derives_written_observed_recalled_and_session_cost(env) -> None:
    fake, _, h = env
    fake.one.append(
        _run_row(status="completed", completed_at=T0, duration_ms=5, memories_used=["ci:flaky"])
    )
    fake.all.append([{"memory_key": "w1"}, {"memory_key": "w2"}])
    fake.one.append({"n": 3})
    # trace log off -> recalled query is skipped entirely
    fake.one.append({"turns": 12, "cost": 0.4321})
    out = asyncio.run(h.handle_run_get(RunGetInput(org="o", namespace="n", run_id="r1")))
    assert out.found and out.run is not None
    assert out.run.status == "completed" and out.run.memories_used == ["ci:flaky"]
    assert out.run.started_at == "2026-09-11T12:00:00.000Z" and out.run.metadata == {"k": 1}
    assert out.memories_written == ["w1", "w2"] and out.observations == 3
    assert out.recalled == -1 and out.turns == 12 and out.cost_usd == 0.4321
    cost_call = [c for c in fake.calls if "agent_turn_usage" in c[1]][0]
    assert cost_call[2]["sid"] == "s1"


def test_get_missing_run(env) -> None:
    fake, _, h = env
    fake.one.append(None)
    out = asyncio.run(h.handle_run_get(RunGetInput(org="o", namespace="n", run_id="zz")))
    assert out.found is False and out.run is None


def test_list_filters_and_orders_newest_first(env) -> None:
    fake, _, h = env
    fake.all.append([_run_row(run_id="r2"), _run_row(run_id="r1")])
    out = asyncio.run(
        h.handle_run_list(
            RunListInput(org="o", namespace="n", agent="claude", status="active", limit=2)
        )
    )
    assert [r.run_id for r in out.runs] == ["r2", "r1"]
    kind, sql, params = fake.calls[0]
    assert "FROM loom.agent_runs FINAL" in sql and "ORDER BY started_at DESC" in sql
    assert "agent = {agent:String}" in sql and "status = {status:String}" in sql
    assert "session_id" not in sql.split("WHERE", 1)[1]
    assert params == {"org": "o", "ns": "n", "lim": 2, "agent": "claude", "status": "active"}
    # default: no memory-activity clause
    assert "memories_used" not in sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]


def test_list_memory_activity_is_a_where_clause_not_a_client_slice(env) -> None:
    """The Runs page's fold runs inside the query so LIMIT counts visible
    runs — otherwise the newest 100 of a chatty consumer can all be hollow."""
    fake, _, h = env
    fake.all.append([_run_row(run_id="r9", memories_used=["k"])])
    out = asyncio.run(
        h.handle_run_list(RunListInput(org="o", namespace="n", memory_activity="with", limit=100))
    )
    assert [r.run_id for r in out.runs] == ["r9"]
    _, sql, params = fake.calls[0]
    clause = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "(notEmpty(memories_used) OR notEmpty(memories_written))" in clause
    assert params == {"org": "o", "ns": "n", "lim": 100}

    fake.all.append([])
    asyncio.run(h.handle_run_list(RunListInput(org="o", namespace="n", memory_activity="without")))
    _, sql, _ = fake.calls[1]
    assert "(empty(memories_used) AND empty(memories_written))" in sql.split("WHERE", 1)[1]

    with pytest.raises(ValidationError):
        RunListInput(org="o", namespace="n", memory_activity="hollow")  # type: ignore[arg-type]


def test_run_ops_are_registered_and_run_list_is_mcp_hidden() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import _MCP_HIDDEN_OPS

    assert OPERATIONS_BY_NAME["run.begin"].required_role == Role.WRITER
    assert OPERATIONS_BY_NAME["run.complete"].idempotent is False
    assert OPERATIONS_BY_NAME["run.get"].required_role == Role.VIEWER
    assert "run.list" in _MCP_HIDDEN_OPS and "run.get" not in _MCP_HIDDEN_OPS


def test_next_hint_only_under_a_run() -> None:
    from server.memory.api.models_common import run_next_hint

    assert run_next_hint(None) == "" and run_next_hint("-") == "" and run_next_hint("") == ""
    hint = run_next_hint("r9")
    assert hint.startswith("run_id=r9") and "run.complete" in hint and "memories_used" in hint


def test_procedural_memories_used_land_a_procedure_run(env, monkeypatch) -> None:
    """A procedure the agent relied on is a procedure the agent ran: the same
    report lands one procedure.record_run row (empty path — loom never guesses
    the steps; ok = the run's outcome) so the evolve worker has a ledger.
    Non-procedural keys and `none` outcomes record nothing."""
    fake, events, h = env
    recorded: list[Any] = []

    async def _record(inp: Any) -> Any:
        recorded.append(inp)
        return type("Out", (), {"memory_key": inp.memory_key, "version": 0, "run_stats": {}})()

    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_record_run", _record)

    fake.one.append(_run_row())
    fake.all.append(
        [
            {
                "mid": "id-a",
                "memory_key": "ci:flaky",
                "memory_type": "semantic",
                "agent": "claude",
                "user_id": "-",
                "session_id": "-",
            },
            {
                "mid": "id-p",
                "memory_key": "proc:deploy",
                "memory_type": "procedural",
                "agent": "ops-bot",
                "user_id": "-",
                "session_id": "-",
            },
        ]
    )
    fake.all.append([])
    fake.one.append({"n": 0})
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o",
                namespace="n",
                run_id="r1",
                status="failed",
                final_response="rollback",
                confidence=0.4,
                memories_used=["ci:flaky", "proc:deploy"],
            )
        )
    )
    assert out.procedure_runs_recorded == 1
    assert out.used_applied == 2 and out.harm_applied == 2
    (inp,) = recorded
    assert inp.memory_key == "proc:deploy" and inp.ok is False and inp.path == []
    assert inp.agent == "ops-bot" and inp.org == "o" and inp.namespace == "n"
    assert inp.note.startswith("run.complete r1")

    # outcome "none": the run says nothing about the procedure → no ledger row.
    recorded.clear()
    fake.one.append(_run_row())
    fake.all.append(
        [
            {
                "mid": "id-p",
                "memory_key": "proc:deploy",
                "memory_type": "procedural",
                "agent": "ops-bot",
                "user_id": "-",
                "session_id": "-",
            }
        ]
    )
    fake.all.append([])
    fake.one.append({"n": 0})
    out = asyncio.run(
        h.handle_run_complete(
            RunCompleteInput(
                org="o",
                namespace="n",
                run_id="r1",
                status="completed",
                outcome="none",
                memories_used=["proc:deploy"],
            )
        )
    )
    assert out.procedure_runs_recorded == 0 and recorded == []


def test_a_narrowed_runs_written_keys_and_observations_stay_in_its_scope() -> None:
    import asyncio

    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
    from server.tasks.store import runs as runs_store

    class _CH:
        sql: list[str] = []

        async def query_all(self, sql, params=None):
            self.sql.append(sql)
            return []

        async def query_one(self, sql, params=None):
            self.sql.append(sql)
            return {"n": 0}

    only_u = ReadFilters(scope="own_user", user_id="u", session_id=None)
    for scope, bounded in ((only_u, True), (WHOLE_NAMESPACE, False)):
        ch = _CH()
        ch.sql = []
        asyncio.run(runs_store.written_keys(ch, "o", "n", "r", scope=scope))
        asyncio.run(runs_store.observation_count(ch, "o", "n", "r", scope=scope))
        assert all(("scope_uid" in s) is bounded for s in ch.sql), ch.sql
