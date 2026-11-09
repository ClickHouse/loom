"""The residue census runs whether or not the steps did, a
failed saga names its step, the recovery worker's terminal row keeps both,
and `loom.entity_vectors` joins the saga and the census. CH-free."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from typing import Any

import pytest

import server.erasure.app.saga as h
from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store import health as erasure_health
from server.erasure.store import ledger as erasure_ledger
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_graph as graph_steps


class _CH:
    """Records every statement; counts come from `count`, mutations poll done."""

    def __init__(self, count: int = 3, rows: list[dict[str, Any]] | None = None) -> None:
        self.count = count
        self.rows = rows or []  # every read but a system.mutations poll
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), dict(params or {})))
        return {"n": self.count}

    async def query_all(self, sql, params=None):
        if "startsWith(t.name, '_provider_rebuild_')" in sql or "'consolidated'" in sql:
            return []
        if "system.mutations" not in sql:
            return self.rows
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    async def execute(self, sql, params=None):
        self.commands.append((" ".join(sql.split()), dict(params or {})))


def _now():
    return h.datetime.now(h.UTC).replace(tzinfo=None)


async def _run(
    monkeypatch,
    *,
    saga,
    scope_type="namespace_list",
    scope_value='{"namespaces": ["lme-1", "lme-2"], "purge": true}',
    census=None,
    audits: list[dict[str, Any]] | None = None,
):
    """Drive `run_saga` with a fake namespace saga, recording the order of the
    census, the checkpoints and the audit rows (each row's arguments into `audits`)."""
    calls: list[str] = []
    checkpoints: list[dict[str, Any]] = []

    async def fake_checkpoint(**kw):
        calls.append(f"checkpoint:{kw.get('status')}")
        checkpoints.append(kw)

    async def fake_emit(**kw):
        calls.append(f"audit:{kw.get('event_type')}")
        if audits is not None:
            audits.append(kw)

    async def fake_census(org, st, sv, *, ch=None):
        calls.append(f"census:{st}:{'purge' if erasure_mutations.purge_mode() else 'live'}")
        if census is not None:
            return census(org, st, sv)
        return {"loom.memory": 0, "loom.entity_edges": 177999, "loom.entity_vectors": 12}

    async def fake_saga(org, namespace, *, completed, row, verify=None, external=True):
        calls.append("saga")
        return await saga(org, namespace, completed=completed, row=row, verify=verify)

    monkeypatch.setattr(h, "_saga_namespace", fake_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(h.audit, "emit", fake_emit)
    monkeypatch.setattr(erasure_census, "residue_census", fake_census)
    monkeypatch.setattr(erasure_census, "table_residue", fake_census)
    inp = ErasureRequestInput(
        org="o", scope_type=scope_type, scope_value=scope_value, requester="t"
    )
    out = await h.run_saga(
        input=inp,
        row={"id": "e1", "actor": "human:t"},
        erasure_id="e1",
        requested_at=_now(),
        previously_completed=set(),
    )
    return out, calls, checkpoints


async def _failing_external_step(org, namespace, *, completed, row, verify=None):
    """Two green steps, then the external step dies with an EMPTY message —
    the shape of the eight abandoned sagas."""
    results = []
    for name in ("memory.tombstone", "entity_graph.delete"):

        async def _ok() -> int:
            return 5

        results.append(await h._do_step(step_name=name, step_fn=_ok, completed=completed, row=row))

    async def _boom() -> int:
        raise RuntimeError("")

    results.append(
        await h._do_step(
            step_name="external.delete_namespace", step_fn=_boom, completed=completed, row=row
        )
    )
    return results


async def test_the_census_runs_after_a_step_failure_and_before_the_failed_checkpoint(
    monkeypatch,
) -> None:
    out, calls, checkpoints = await _run(monkeypatch, saga=_failing_external_step)
    assert out.status == "failed"
    # ordering: the step checkpoints, the failing step, THEN the census, THEN the terminal row
    assert calls == [
        "saga",
        "checkpoint:in_progress",
        "checkpoint:in_progress",
        "census:namespace:purge",  # a purge saga's tail counts every row, not only live ones
        "checkpoint:failed",
        "audit:erasure.failed",
    ]
    terminal = checkpoints[-1]
    # the reason is the step's, not the census's
    assert terminal["failure_reason"] == "external.delete_namespace: RuntimeError"
    assert out.failure_reason == "external.delete_namespace: RuntimeError"
    # …and the verify columns are on the same row
    assert terminal["extra"]["residue_count"] == 178011
    assert json.loads(terminal["extra"]["residue"]) == {
        "loom.entity_edges": 177999,
        "loom.entity_vectors": 12,
    }
    assert terminal["extra"]["verified_at"] is not None
    assert out.residue_count == 178011 and out.verified_at is not None
    # the green steps stay green for the resume; the failed one is not among them
    assert terminal["steps_completed"] == ["entity_graph.delete", "memory.tombstone"]
    assert out.steps_completed == ["entity_graph.delete", "memory.tombstone"]
    assert (
        "verify.residue" not in terminal["steps_completed"]
    )  # the tail is evidence, not a completed step


async def test_a_step_error_names_its_step_even_with_a_message(monkeypatch) -> None:
    async def saga(org, namespace, *, completed, row, verify=None):
        async def _boom() -> int:
            raise ValueError("Network Error: Timeout on reading data from socket")

        await h._do_step(step_name="memory.tombstone", step_fn=_boom, completed=completed, row=row)
        return []

    audits: list[dict[str, Any]] = []
    out, _calls, checkpoints = await _run(monkeypatch, saga=saga, audits=audits)
    # the driver's text (a statement, an address) stays in the log; the requester
    # reads the step and the error's kind
    assert checkpoints[-1]["failure_reason"] == "memory.tombstone: ValueError"
    (failed,) = [a for a in audits if a["event_type"] == "erasure.failed"]
    assert failed["subject"]["error"] == "memory.tombstone: ValueError"
    err = saga_steps.ErasureStepError("x.step", KeyError("k"))
    assert (
        err.step == "x.step" and isinstance(err.cause, KeyError) and str(err) == "x.step: KeyError"
    )


async def test_the_tail_does_not_rerun_a_census_the_verify_step_already_did(monkeypatch) -> None:
    async def saga(org, namespace, *, completed, row, verify=None):
        # the real verify step: fills the holder and raises on residue
        return [
            await h._do_step(
                step_name="verify.residue",
                step_fn=lambda: saga_steps._step_verify_residue(
                    org, "namespace", namespace, verify
                ),
                completed=completed,
                row=row,
            )
        ]

    out, calls, checkpoints = await _run(monkeypatch, saga=saga)
    assert (
        calls.count("census:namespace:purge") == 1
    )  # once, by the step; the tail saw a full holder
    assert checkpoints[-1]["failure_reason"].startswith(
        "verify.residue: ErasureResidueError: residue after erasure"
    )
    assert checkpoints[-1]["extra"]["residue_count"] == 178011


async def test_a_census_that_fails_in_the_tail_leaves_the_step_reason_alone(monkeypatch) -> None:
    def broken(org, st, sv):
        raise RuntimeError("Code: 241. MEMORY_LIMIT_EXCEEDED")

    out, calls, checkpoints = await _run(monkeypatch, saga=_failing_external_step, census=broken)
    assert out.status == "failed"
    assert checkpoints[-1]["failure_reason"] == "external.delete_namespace: RuntimeError"
    assert checkpoints[-1]["extra"] == {} and out.verified_at is None
    assert erasure_mutations.purge_mode() is False  # the tail's purge token was reset


async def test_a_completed_saga_reports_its_completed_steps(monkeypatch) -> None:
    async def saga(org, namespace, *, completed, row, verify=None):
        async def _ok() -> int:
            return 1

        await h._do_step(step_name="memory.tombstone", step_fn=_ok, completed=completed, row=row)
        return []

    out, calls, _ = await _run(monkeypatch, saga=saga)
    assert out.status == "completed" and out.steps_completed == ["memory.tombstone"]
    assert not any(c.startswith("census") for c in calls)  # no failure → no tail


async def test_a_user_scope_tail_counts_by_user(monkeypatch) -> None:
    async def saga(org, namespace, *, completed, row, verify=None):
        raise AssertionError("not the user saga")

    calls: list[str] = []

    async def fake_user_saga(org, user_id, *, completed, row, verify=None):
        async def _boom() -> int:
            raise RuntimeError("x")

        await h._do_step(step_name="memory.tombstone", step_fn=_boom, completed=completed, row=row)
        return []

    async def fake_census(org, st, sv, *, ch=None):
        calls.append(f"{st}:{sv}:{erasure_mutations.purge_mode()}")
        return {"loom.memory": 0}

    async def noop(**kw):
        return None

    monkeypatch.setattr(h, "_saga_user_id", fake_user_saga)
    monkeypatch.setattr(h, "checkpoint", noop)
    monkeypatch.setattr(h.audit, "emit", noop)
    monkeypatch.setattr(erasure_census, "residue_census", fake_census)
    monkeypatch.setattr(erasure_census, "table_residue", fake_census)
    inp = ErasureRequestInput(org="o", scope_type="user_id", scope_value="alice", requester="t")
    out = await h.run_saga(
        input=inp,
        row={"actor": "human:t"},
        erasure_id="e2",
        requested_at=_now(),
        previously_completed=set(),
    )
    assert out.status == "failed" and calls == ["user_id:alice:False"]


# ---------------------------------------------------------------------------
# the recovery worker's terminal row
# ---------------------------------------------------------------------------


_DRIVER_TEXT = "Code: 210. Connection refused (ch-internal-7:9440) ALTER TABLE loom.memory"


def _ledger_row(**over) -> dict[str, Any]:
    row = {
        "id": "e9",
        "org": "o",
        "scope_type": "namespace_list",
        "scope_value": '{"namespaces": ["lme-1"], "purge": true, "external": true}',
        "requester": "cli",
        "actor": "",
        "urgency": "normal",
        "attempts": 4,
        "steps_completed": ["memory.tombstone"],
        "requested_at": _now(),
        "failure_reason": "external.delete_namespace: ErasureBackendError: x transport error",
    }
    row.update(over)
    return row


async def test_the_terminal_row_keeps_the_last_step_reason_and_the_verify_columns(
    monkeypatch,
) -> None:
    from server.erasure.workers import recovery as w

    checkpoints: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    verified_at = _now()

    async def fake_run_saga(
        *, input, row, erasure_id, requested_at, previously_completed, residue=None
    ):
        # the attempt completed one more step, then died on the external step,
        # and its tail counted residue
        return SimpleNamespace(
            status="failed",
            steps=[],
            residue_count=41,
            residue={"loom.entity_edges": 41},
            verified_at=verified_at,
            failure_reason="external.delete_namespace: ErasureBackendError",
            steps_completed=["entity_graph.delete", "memory.tombstone"],
        )

    async def fake_checkpoint(**kw):
        checkpoints.append(kw)

    async def fake_emit(**kw):
        audits.append(kw)

    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(w.audit, "emit", fake_emit)
    res = w.RecoveryResult()
    ledger_row = _ledger_row()
    await w._recover_one(row=ledger_row, cfg=w.RecoveryConfig(max_attempts=5), result=res)
    assert res.failed_permanently == 1 and len(checkpoints) == 1
    t = checkpoints[0]
    assert t["status"] == "failed" and t["row"]["attempts"] == 5
    # the terminal row carries the columns every saga row carries
    assert t["row"] == h.request_row_template(
        erasure_id="e9",
        input=h.input_from_ledger_row(ledger_row),
        actor="",  # the ledger row names no actor, and the terminal row keeps none
        requested_at=ledger_row["requested_at"],
        attempts=5,
    )
    assert t["failure_reason"] == (
        "max_attempts (5) exceeded; last: external.delete_namespace: ErasureBackendError"
    )
    assert t["steps_completed"] == [
        "entity_graph.delete",
        "memory.tombstone",
    ]  # this attempt's, not the stale set
    assert t["extra"] == {
        "residue_count": 41,
        "verified_at": verified_at,
        "residue": '{"loom.entity_edges": 41}',
    }
    assert audits[-1]["event_type"] == "erasure.abandoned"
    assert audits[-1]["subject"]["last_error"] == "external.delete_namespace: ErasureBackendError"
    assert audits[-1]["subject"]["residue_count"] == 41


async def test_the_terminal_row_falls_back_to_the_ledgers_reason_when_the_saga_raises(
    monkeypatch,
) -> None:
    from server.erasure.workers import recovery as w

    checkpoints: list[dict[str, Any]] = []

    async def fake_run_saga(**kw):
        raise RuntimeError("client disconnected")

    async def fake_checkpoint(**kw):
        checkpoints.append(kw)

    async def fake_emit(**kw):
        return None

    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(w.audit, "emit", fake_emit)
    res = w.RecoveryResult()
    await w._recover_one(row=_ledger_row(), cfg=w.RecoveryConfig(max_attempts=5), result=res)
    t = checkpoints[0]
    # the requester reads the kind; the text is in the server log
    assert t["failure_reason"] == "max_attempts (5) exceeded; last: RuntimeError"
    assert t["extra"] == {} and t["steps_completed"] == ["memory.tombstone"]
    assert res.errors and res.errors[0]["error"] == "RuntimeError: client disconnected"


async def test_a_saga_that_completes_on_its_last_attempt_is_not_abandoned(monkeypatch) -> None:
    from server.erasure.workers import recovery as w

    checkpoints: list[dict[str, Any]] = []
    audits: list[str] = []

    async def fake_run_saga(**kw):
        return SimpleNamespace(
            status="completed",
            steps=[],
            residue_count=0,
            residue={},
            verified_at=_now(),
            failure_reason="",
            steps_completed=["a"],
        )

    async def fake_checkpoint(**kw):
        checkpoints.append(kw)

    async def fake_emit(**kw):
        audits.append(kw["event_type"])

    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(w.audit, "emit", fake_emit)
    res = w.RecoveryResult()
    await w._recover_one(row=_ledger_row(), cfg=w.RecoveryConfig(max_attempts=5), result=res)
    assert res.completed == 1 and res.failed_permanently == 0
    assert checkpoints == [] and audits == ["erasure.recovered"]


async def test_a_failed_abandonment_write_does_not_stop_the_next_sagas_recovery(
    monkeypatch,
) -> None:
    """The permanent-fail checkpoint and audit sit outside the saga's own
    try; before, one failed write there ended the pass and every saga after
    it in the batch waited for the next interval."""
    from server.erasure.store import ledger as erasure_ledger
    from server.erasure.workers import recovery as w
    from server.workers import pass_context

    async def fake_stuck(**kw):
        return [_ledger_row(id="e1"), _ledger_row(id="e2", attempts=0)]

    ran: list[str] = []

    async def fake_run_saga(*, erasure_id, **kw):
        ran.append(erasure_id)
        return SimpleNamespace(
            status="completed" if erasure_id == "e2" else "failed",
            steps=[],
            residue_count=0,
            residue={},
            verified_at=_now(),
            failure_reason="x",
            steps_completed=[],
        )

    async def failing_checkpoint(**kw):
        raise RuntimeError("insert timed out")

    async def fake_emit(**kw):
        return None

    async def none_exhausted(**kw):
        return []

    monkeypatch.setattr(erasure_ledger, "list_stuck_in_progress", fake_stuck)
    monkeypatch.setattr(erasure_ledger, "list_unabandoned_exhausted", none_exhausted)
    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    monkeypatch.setattr(h, "checkpoint", failing_checkpoint)
    monkeypatch.setattr(w.audit, "emit", fake_emit)
    ctx = pass_context.begin("erasure_recovery")
    try:
        res = await w.run_pass(w.RecoveryConfig(max_attempts=5))
    finally:
        pass_context.clear()
    assert ran == ["e1", "e2"] and res.completed == 1
    assert res.errors[-1]["erasure_id"] == "e1" and ctx.count == 1


async def test_a_saga_whose_abandonment_never_landed_is_abandoned_next_pass(
    monkeypatch,
) -> None:
    """Fail closed: the last attempt failed and its terminal write failed
    too. The row is past the attempts budget the stuck scan reads within;
    the next pass writes the audit row and then the terminal checkpoint,
    carrying the verify columns the saga already stored, without re-running
    the saga."""
    from server.erasure.store import ledger as erasure_ledger
    from server.erasure.workers import recovery as w

    verified_at = _now()
    # a saga that failed before the upgrade stored the driver's text after the kind
    stored = _ledger_row(
        attempts=5,
        residue_count=3,
        residue='{"loom.memory": 3}',
        verified_at=verified_at,
        failure_reason=f"external.delete_namespace: ErasureBackendError: {_DRIVER_TEXT}",
    )
    order: list[str] = []
    subjects: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []

    async def no_stuck(**kw):
        return []

    async def exhausted(**kw):
        assert kw["max_attempts"] == 5
        return [stored]

    async def no_saga(**kw):
        raise AssertionError("an exhausted saga is not re-run")

    async def fake_emit(**kw):
        order.append(kw["event_type"])
        subjects.append(kw["subject"])

    async def fake_checkpoint(**kw):
        order.append("checkpoint")
        checkpoints.append(kw)

    monkeypatch.setattr(erasure_ledger, "list_stuck_in_progress", no_stuck)
    monkeypatch.setattr(erasure_ledger, "list_unabandoned_exhausted", exhausted)
    monkeypatch.setattr(h, "run_saga", no_saga)
    monkeypatch.setattr(h, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(w.audit, "emit", fake_emit)
    res = await w.run_pass(w.RecoveryConfig(max_attempts=5))
    assert order == ["erasure.abandoned", "checkpoint"]  # the marker lands last
    (t,) = checkpoints
    assert t["failure_reason"].startswith(erasure_ledger.ABANDONED_REASON_PREFIX)
    assert t["failure_reason"].endswith("last: external.delete_namespace: ErasureBackendError")
    assert subjects[-1]["last_error"] == "external.delete_namespace: ErasureBackendError"
    assert t["row"]["attempts"] == 5 and t["steps_completed"] == ["memory.tombstone"]
    assert t["extra"] == {
        "residue_count": 3,
        "residue": '{"loom.memory": 3}',
        "verified_at": verified_at,
    }
    assert res.failed_permanently == 1


def test_an_exhausted_row_without_a_census_abandons_with_no_verify_columns() -> None:
    """A saga that never reached the census has no verify columns to carry:
    its terminal row writes none, rather than a `residue_count = 0` that
    would read as verified."""
    from server.erasure.workers import recovery as w

    unverified = _ledger_row(attempts=5, residue_count=0, residue="", verified_at=None)
    assert w._ledger_verify(unverified) == {}
    verified_at = _now()
    partial = _ledger_row(attempts=5, verified_at=verified_at)  # no residue columns read
    assert w._ledger_verify(partial) == {"verified_at": verified_at}


@pytest.mark.parametrize("ch_residue", [{"loom.memory": 3}, {}], ids=["residue", "clean"])
async def test_a_failed_saga_with_postgres_down_records_what_its_census_counted(
    monkeypatch, ch_residue: dict[str, int]
) -> None:
    """The failed checkpoint keeps the ClickHouse residue its tail found, unverified, and
    writes no census at all when it found nothing."""

    async def pg_down(*_: Any, **__: Any) -> dict[str, int]:
        raise ConnectionError("the authority is unreachable")

    monkeypatch.setattr(erasure_census, "pg_residue", pg_down)
    out, _, checkpoints = await _run(
        monkeypatch, saga=_failing_external_step, census=lambda *_: dict(ch_residue)
    )
    assert out.status == "failed" and out.verified_at is None
    kept = {"residue_count": 3, "verified_at": None, "residue": '{"loom.memory": 3}'}
    assert checkpoints[-1]["extra"] == (kept if ch_residue else {})


def test_a_census_that_left_a_store_uncounted_keeps_the_residue_it_found() -> None:
    """No `verified_at`, as a store could not be counted, but residue found: the terminal
    row keeps it, from the ledger and from a saga's output alike."""
    from server.erasure.api.erasure import ErasureRequestOutput
    from server.erasure.workers import recovery as w

    uncounted = _ledger_row(attempts=5, residue_count=3, residue='{"loom.memory": 3}')
    kept = {"residue_count": 3, "residue": '{"loom.memory": 3}', "verified_at": None}
    assert w._ledger_verify(uncounted | {"verified_at": None}) == kept
    out = ErasureRequestOutput.model_construct(
        residue_count=3, residue={"loom.memory": 3}, verified_at=None
    )
    assert h.verify_columns_from_output(out) == kept
    assert h.verify_columns_from_output(out.model_copy(update={"residue_count": 0})) == {}


def test_the_exhausted_scan_skips_abandoned_rows() -> None:
    import inspect

    from server.erasure.store import ledger as erasure_ledger

    src = inspect.getsource(erasure_ledger.list_unabandoned_exhausted)
    assert "attempts >= {ma:UInt16}" in src
    assert "NOT startsWith(failure_reason, {pfx:String})" in src
    assert erasure_ledger.ABANDONED_REASON_PREFIX == "max_attempts ("


def test_the_stuck_scan_reads_the_ledgers_reason() -> None:
    import inspect

    src = inspect.getsource(erasure_ledger.list_stuck_in_progress)
    assert "failure_reason" in src.split("FROM loom.erasure_requests")[0]


# ---------------------------------------------------------------------------
# entity_vectors joins the saga and the census
# ---------------------------------------------------------------------------


def test_entity_vectors_is_in_the_census_for_both_scopes() -> None:
    assert "loom.entity_vectors" in erasure_census.census_tables("namespace")
    assert "loom.entity_vectors" in erasure_census.census_tables("user_id")
    live = {s.table: s.live for s in erasure_census._CENSUS}["loom.entity_vectors"]
    assert live == "tombstoned_at IS NULL"  # the boost's own predicate; FINAL follows from it
    # every table the purge deletes lightweight and reads by namespace is counted
    for t in erasure_mutations.LIGHTWEIGHT_PURGE_TABLES - {
        "loom.chunks",
        "loom.code_chunks",
        "loom.code_summaries",
        "loom.conversation_embeddings",
        "loom.embedding_cache",  # deleted org-wide, uncounted: traffic refills it
    }:
        assert t in erasure_census.census_tables("namespace"), t


async def test_entity_vectors_delete_is_lightweight_in_every_mode() -> None:
    ch = _CH(count=7)
    n = await graph_steps.delete_entity_vectors_by_namespace("o", ["lme-1", "lme-2"], ch=ch)
    assert n == 7
    csql, cparams = ch.queries[0]
    assert (
        "FROM loom.entity_vectors WHERE org = {org:String} AND namespace IN {ns:Array(String)}"
        in csql
    )
    assert "FINAL" not in csql and cparams["ns"] == ["lme-1", "lme-2"]
    msql, _ = ch.commands[0]
    assert msql.startswith("DELETE FROM loom.entity_vectors")  # not a purge, still lightweight
    assert "namespace IN {ns:Array(String)}" in msql and "ALTER TABLE" not in msql

    ch = _CH(count=1)
    with erasure_mutations.purge_scope(True):
        await graph_steps.delete_entity_vectors_by_namespace("o", "lme-1", ch=ch)
    assert (
        ch.commands[0][0].startswith("DELETE FROM loom.entity_vectors")
        and "namespace = {ns:String}" in ch.commands[0][0]
    )

    ch = _CH(count=0)
    assert (
        await graph_steps.delete_entity_vectors_by_namespace("o", "lme-1", ch=ch) == 0
        and ch.commands == []
    )


async def test_entity_vectors_delete_by_user_reaches_both_identities() -> None:
    ch = _CH(count=2, rows=[{"namespace": "ns", "memory_key": "k"}])
    await graph_steps.delete_entity_vectors_by_user("o", "alice", ch=ch)
    msql, params = ch.commands[0]
    assert msql.startswith("DELETE FROM loom.entity_vectors")
    assert "user_id = {uid:String}" in msql and params["uid"] == "alice"
    assert "(namespace, memory_key) IN {keys:Array(Tuple(String, String))}" in msql
    assert params["keys"] == [("ns", "k")] and "SELECT" not in msql
    # a user with no memory keys still loses the vectors stamped with their id
    ch = _CH(count=2)
    await graph_steps.delete_entity_vectors_by_user("o", "alice", ch=ch)
    ((msql, params),) = ch.commands
    assert params["uid"] == "alice" and params["keys"] == []


async def test_other_deletes_stay_alter_delete_outside_a_purge() -> None:
    ch = _CH(count=2)
    await graph_steps.delete_entity_graph_by_namespace("o", "lme-1", ch=ch)
    assert all(c[0].startswith("ALTER TABLE") for c in ch.commands)


def _record_steps(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def fake_do_step(*, step_name, step_fn, completed, row, **kw):
        calls.append(step_name)
        return SimpleNamespace(step=step_name, rows_affected=0)

    monkeypatch.setattr(h, "_do_step", fake_do_step)
    return calls


async def _async_noop(*a, **kw):
    return None


async def test_both_sagas_run_entity_vectors_delete_after_the_graph_step(monkeypatch) -> None:
    calls = _record_steps(monkeypatch)
    await h._saga_namespace("o", ["lme-1"], completed=set(), row={}, external=False)
    assert calls.index("entity_vectors.delete") == calls.index("entity_graph.delete") + 1, calls
    assert calls[-1] == "verify.residue"

    import server.erasure.app.backends as eb

    monkeypatch.setattr(eb, "all_backends", list)
    monkeypatch.setattr("server.core.namespace_write_marks.bump", _async_noop)
    calls = _record_steps(monkeypatch)
    await h._saga_user_id("o", "alice", completed=set(), row={})
    # the user saga's graph rows and communities go in graph_links.delete, which reads
    # ownership from the links first
    assert "entity_graph.delete" not in calls and "entity_communities.delete" not in calls
    assert calls.index("entity_vectors.delete") == calls.index("graph_links.delete") - 1
    assert calls[-1] == "verify.residue"


# ---------------------------------------------------------------------------
# the operator CLI and the compliance bench
# ---------------------------------------------------------------------------


def test_re_verify_input_rewrites_external_only_for_a_namespace_list() -> None:
    from server.cli import hygiene

    row = _ledger_row()
    inp = hygiene._re_verify_input(row, skip_external=True)
    assert inp.external is False and inp.purge is True
    assert json.loads(inp.scope_value) == {
        "namespaces": ["lme-1"],
        "purge": True,
        "external": False,
    }
    kept = hygiene._re_verify_input(row, skip_external=False)
    assert kept.external is True and kept.scope_value == row["scope_value"]
    with pytest.raises(ValueError, match="namespace_list"):
        hygiene._re_verify_input(
            _ledger_row(scope_type="namespace", scope_value="ns"), skip_external=True
        )
    assert hygiene._re_verify_scope(_ledger_row(scope_type="user_id", scope_value="alice")) == (
        "user_id",
        "alice",
        False,
        True,
    )
    # a user saga's purge is its ledger row's column: a resumed purge keeps purging
    purged = _ledger_row(scope_type="user_id", scope_value="alice", purge=1)
    assert hygiene._re_verify_scope(purged) == ("user_id", "alice", True, True)
    assert hygiene._re_verify_input(purged, skip_external=False).purge is True
    one_ns = _ledger_row(scope_type="namespace", scope_value="ns", purge=1)
    assert hygiene._re_verify_scope(one_ns) == ("namespace", "ns", False, True)


async def test_re_verify_run_lists_censuses_and_resumes_from_the_checkpoint(monkeypatch) -> None:
    from server.cli import hygiene

    calls: list[Any] = []
    row = _ledger_row(attempts=5)

    async def fake_list(*, org=None, ch=None):
        calls.append(("list", org))
        return [row]

    async def fake_census(org, st, sv, *, ch=None):
        calls.append(("census", st, sv, erasure_mutations.purge_mode()))
        return {"loom.entity_edges": 3, "loom.memory": 0}

    async def fake_counts(*, ch=None):
        return {"orphan_entity_vectors": 0, "orphan_graph_namespaces": 0}

    async def fake_run_saga(
        *, input, row, erasure_id, requested_at, previously_completed, residue=None
    ):
        calls.append(
            ("resume", erasure_id, row["attempts"], sorted(previously_completed), input.external)
        )
        return SimpleNamespace(
            status="completed",
            steps=[],
            residue_count=0,
            residue={},
            verified_at=_now(),
            failure_reason="",
            steps_completed=[],
        )

    monkeypatch.setattr(erasure_ledger, "list_failed_requests", fake_list)
    monkeypatch.setattr(erasure_census, "residue_census", fake_census)
    monkeypatch.setattr(erasure_census, "table_residue", fake_census)
    monkeypatch.setattr(erasure_health, "integrity_counts", fake_counts)
    monkeypatch.setattr(h, "run_saga", fake_run_saga)
    args = argparse.Namespace(
        org="o",
        id=None,
        resume=False,
        skip_external=False,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="t",
        completed_since=None,
    )
    assert await hygiene._re_verify_run(object(), args) == 1  # residue found, nothing resumed
    assert calls == [("list", "o"), ("census", "namespace", ["lme-1"], True)]

    calls.clear()
    args.resume = True
    args.skip_external = True
    assert await hygiene._re_verify_run(object(), args) == 0
    assert calls[-1] == (
        "resume",
        "e9",
        6,
        ["memory.tombstone"],
        False,
    )  # past the worker's budget, loom-only
    assert erasure_mutations.purge_mode() is False


async def test_re_verify_run_fails_when_a_store_cannot_be_read(monkeypatch, capsys) -> None:
    """No ClickHouse residue is no verdict while Postgres cannot be counted: the run says
    so and exits 1."""
    from server.cli import hygiene

    async def fake_list(*, org=None, ch=None):
        return [_ledger_row(attempts=5)]

    async def no_tables(org, st, sv, *, ch=None):
        return {"loom.memory": 0}

    async def pg_down(*args: Any, **kw: Any) -> dict[str, int]:
        raise ConnectionError("the authority is unreachable")

    async def fake_counts(*, ch=None):
        return {"orphan_entity_vectors": 0, "orphan_graph_namespaces": 0}

    monkeypatch.setattr(erasure_ledger, "list_failed_requests", fake_list)
    monkeypatch.setattr(erasure_census, "table_residue", no_tables)
    monkeypatch.setattr(erasure_census, "pg_residue", pg_down)
    monkeypatch.setattr(erasure_health, "integrity_counts", fake_counts)
    args = argparse.Namespace(
        org="o",
        id=None,
        resume=False,
        skip_external=False,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="t",
        completed_since=None,
    )
    assert await hygiene._re_verify_run(object(), args) == 1
    assert "postgres: could not be read (ConnectionError" in capsys.readouterr().out


async def test_re_verify_orphan_graph_lists_and_erases_in_loom_only_purge_sagas(
    monkeypatch,
) -> None:
    from server.cli import hygiene

    calls: list[Any] = []

    async def fake_list(*, org=None, ch=None):
        return []

    async def fake_orphans(*, org=None, ch=None):
        return [{"org": org, "namespace": f"lme-{i}", "edges": 10 - i} for i in range(3)]

    async def fake_counts(*, ch=None):
        return {"orphan_entity_vectors": 5, "orphan_graph_namespaces": 3}

    async def fake_erase(inp):
        calls.append((inp.scope_type, json.loads(inp.scope_value), inp.purge, inp.external))
        return SimpleNamespace(erasure_id="x", status="completed", steps=[], residue_count=0)

    monkeypatch.setattr(erasure_ledger, "list_failed_requests", fake_list)
    monkeypatch.setattr(erasure_health, "orphan_graph_namespaces", fake_orphans)
    monkeypatch.setattr(erasure_health, "integrity_counts", fake_counts)
    monkeypatch.setattr("server.erasure.app.handlers.handle_erasure_request", fake_erase)
    args = argparse.Namespace(
        org="o",
        id=None,
        resume=False,
        skip_external=False,
        orphan_graph=True,
        erase=False,
        batch=2,
        show=20,
        requester="t",
        completed_since=None,
    )
    assert await hygiene._re_verify_run(object(), args) == 1  # dry run with orphans → non-zero
    assert calls == []
    args.erase = True
    assert await hygiene._re_verify_run(object(), args) == 0
    assert calls == [
        (
            "namespace_list",
            {"namespaces": ["lme-0", "lme-1"], "purge": True, "external": False},
            True,
            False,
        ),
        (
            "namespace_list",
            {"namespaces": ["lme-2"], "purge": True, "external": False},
            True,
            False,
        ),
    ]
    args.org = ""
    assert await hygiene._re_verify_run(object(), args) == 2


async def test_completed_since_deletes_only_orphan_row_links(monkeypatch) -> None:
    # A saga an N-1 replica completed during a rollout never erased the tables N added
    # (entity_triple_rows, entity_edge_rows). Everything else in its scope may have
    # been written since, so the saga is not run again: only the orphan links go.
    from datetime import datetime

    from server.cli import hygiene

    since = datetime(2026, 10, 17, 8, 0)
    done = ["memory.tombstone", "graph_links.delete", "verify.residue"]
    left = _ledger_row(id="c1", steps_completed=done, failure_reason="", verified_at=since)
    clean = _ledger_row(
        id="c2",
        verified_at=since,
        scope_value='{"namespaces": ["lme-2"], "purge": true, "external": true}',
        steps_completed=done,
        failure_reason="",
    )
    asked: list[Any] = []
    deleted: list[Any] = []

    async def fake_failed(*, org=None, ch=None):
        return []

    async def fake_completed(*, since, org=None, ch=None):
        asked.append((since, org))
        return [left, clean]

    async def fake_orphans(org, st, sv, *, verified_at, ch=None):
        n = 2 if sv == ["lme-1"] and not deleted else 0
        return {"loom.entity_triple_rows": n, "loom.entity_edge_rows": 0}

    async def fake_delete(org, st, sv, *, verified_at, ch=None):
        assert verified_at == since  # the saga's own census time is the cut
        deleted.append((org, st, sv))
        return 2

    async def no_census(*a, **k):
        raise AssertionError("a completed saga gets no full census")

    async def no_saga(**k):
        raise AssertionError("a completed saga is not run again")

    async def fake_counts(*, ch=None):
        return {"orphan_entity_vectors": 0, "orphan_graph_namespaces": 0}

    monkeypatch.setattr(erasure_ledger, "list_failed_requests", fake_failed)
    monkeypatch.setattr(erasure_ledger, "list_completed_requests", fake_completed)
    monkeypatch.setattr(graph_steps, "count_orphan_row_links", fake_orphans)
    monkeypatch.setattr(graph_steps, "delete_orphan_row_links", fake_delete)
    monkeypatch.setattr(erasure_census, "residue_census", no_census)
    monkeypatch.setattr(erasure_census, "table_residue", no_census)
    monkeypatch.setattr(erasure_health, "integrity_counts", fake_counts)
    monkeypatch.setattr(h, "run_saga", no_saga)
    args = argparse.Namespace(
        org="o",
        id=None,
        resume=False,
        skip_external=False,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="t",
        completed_since=since,
    )
    assert await hygiene._re_verify_run(object(), args) == 1  # orphans found, read-only
    assert asked == [(since, "o")] and deleted == []

    args.resume = True
    assert await hygiene._re_verify_run(object(), args) == 0
    # Only the saga with orphans deletes, and only in its own scope.
    assert deleted == [("o", "namespace", ["lme-1"])]


async def test_completed_since_takes_only_completed_sagas(monkeypatch, capsys) -> None:
    # A failed saga is the no-flag mode's: --completed-since must not resume it too.
    from datetime import datetime

    from server.cli import hygiene

    failed = _ledger_row(id="f1")
    done = _ledger_row(
        id="c1", steps_completed=["verify.residue"], failure_reason="", verified_at=_now()
    )

    async def fake_failed(*, org=None, ch=None):
        return [failed]

    async def fake_completed(*, since, org=None, ch=None):
        return [done]

    async def fake_orphans(org, st, sv, *, verified_at, ch=None):
        return {"loom.entity_triple_rows": 0, "loom.entity_edge_rows": 0}

    async def no_saga(**k):
        raise AssertionError("no saga runs: the failed one is not taken")

    async def fake_counts(*, ch=None):
        return {"orphan_entity_vectors": 0, "orphan_graph_namespaces": 0}

    monkeypatch.setattr(erasure_ledger, "list_failed_requests", fake_failed)
    monkeypatch.setattr(erasure_ledger, "list_completed_requests", fake_completed)
    monkeypatch.setattr(graph_steps, "count_orphan_row_links", fake_orphans)
    monkeypatch.setattr(erasure_health, "integrity_counts", fake_counts)
    monkeypatch.setattr(h, "run_saga", no_saga)
    args = argparse.Namespace(
        org="o",
        id=None,
        resume=True,
        skip_external=False,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="t",
        completed_since=datetime(2026, 10, 17, 8, 0),
    )
    assert await hygiene._re_verify_run(object(), args) == 0
    out = capsys.readouterr().out
    assert "c1" in out and "f1" not in out


async def test_completed_since_refuses_skip_external(monkeypatch, capsys) -> None:
    # --skip-external changes how a failed saga resumes; a completed one is not resumed,
    # so the flag would do nothing. Refuse it before reading the ledger.
    from datetime import datetime

    from server.cli import hygiene

    async def no_ledger(**k):
        raise AssertionError("the combination is refused before any read")

    monkeypatch.setattr(erasure_ledger, "list_completed_requests", no_ledger)
    monkeypatch.setattr(erasure_ledger, "list_failed_requests", no_ledger)
    args = argparse.Namespace(
        org="o",
        id=None,
        resume=True,
        skip_external=True,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="t",
        completed_since=datetime(2026, 10, 17, 8, 0),
    )
    assert await hygiene._re_verify_run(object(), args) == 2
    assert "--skip-external" in capsys.readouterr().err


def test_an_orphan_row_link_is_one_whose_memory_has_no_live_row_and_no_later_one() -> None:
    from datetime import datetime

    cut = datetime(2026, 10, 17, 8, 0)
    orphans = graph_steps._orphan_row_links("loom.entity_triple_rows", "namespace", ["a", "b"], cut)
    where, params = orphans.where, orphans.params
    assert where.startswith("namespace IN {ns:Array(String)} AND memory_id NOT IN (")
    assert "FROM loom.memory FINAL" in where and "tombstoned_at IS NULL" in where
    # A memory with any row written since the census keeps its link, tombstoned or not.
    assert "AND memory_id NOT IN (SELECT memory_id FROM loom.memory WHERE " in where
    assert "updated_at >= {verified_at:DateTime}" in where
    assert params == {"ns": ["a", "b"], "verified_at": cut}
    orphans = graph_steps._orphan_row_links("loom.entity_triple_rows", "user_id", "u1", cut)
    where, params = orphans.where, orphans.params
    assert where.startswith("user_id = {uid:String} AND memory_id NOT IN (")
    assert "updated_at >= {verified_at:DateTime}" in where
    assert params == {"uid": "u1", "verified_at": cut}


def test_a_user_scope_reads_only_the_memories_its_links_name() -> None:
    from datetime import datetime

    cut = datetime(2026, 10, 17, 8, 0)
    for table in graph_steps.ROW_LINK_TABLES:
        where = graph_steps._orphan_row_links(table, "user_id", "u1", cut).where
        bound = (
            f"AND memory_id IN (SELECT memory_id FROM {table} "
            "WHERE org = {org:String} AND user_id = {uid:String})"
        )
        # Both the live-row and the later-row reads, not the whole org.
        assert where.count(bound) == 2


async def test_list_completed_requests_takes_sagas_verified_since() -> None:
    from datetime import datetime

    seen: list[Any] = []

    class _CH:
        async def query_all(self, sql, params=None):
            seen.append((" ".join(sql.split()), params))
            return []

    since = datetime(2026, 10, 17, 8, 0)
    await erasure_ledger.list_completed_requests(since=since, org="o", ch=_CH())
    sql, params = seen[0]
    assert "status = 'completed' AND verified_at >= {since:DateTime}" in sql
    assert "org = {org:String}" in sql and params == {"since": since, "org": "o"}


def test_completed_since_reads_an_iso_instant_as_utc(monkeypatch) -> None:
    from datetime import datetime

    import server.cli as cli
    from server.cli import hygiene

    seen: list[argparse.Namespace] = []

    def fake_cmd(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(hygiene, "_cmd_erasure_re_verify", fake_cmd)
    assert cli.main(["erasure-re-verify", "--completed-since", "2026-10-17T10:00+02:00"]) == 0
    assert cli.main(["erasure-re-verify", "--completed-since", "2026-10-17 08:00"]) == 0
    assert [a.completed_since for a in seen] == [datetime(2026, 10, 17, 8, 0)] * 2


def test_the_cli_registers_the_command(monkeypatch) -> None:
    import server.cli as cli
    from server.cli import hygiene

    seen: list[argparse.Namespace] = []

    def fake_cmd(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(hygiene, "_cmd_erasure_re_verify", fake_cmd)
    assert cli.main(["erasure-re-verify", "--org", "o", "--resume", "--skip-external"]) == 0
    ns = seen[0]
    assert ns.org == "o" and ns.resume and ns.skip_external and not ns.orphan_graph
    assert ns.batch == 200 and ns.requester == "cli:erasure-re-verify"


def test_bench_census_coverage_names_the_census_tables_the_scan_misses() -> None:
    from bench.erasure_compliance import CanaryReport, census_coverage

    census = erasure_census.census_tables("namespace")
    assert "loom.entity_vectors" in census
    plan = {"memory", "memory_embeddings", "entity_vectors"}
    gaps = census_coverage(plan, census)
    assert "entity_vectors" not in gaps and "entity_edges" in gaps
    assert census_coverage({t.split(".", 1)[1] for t in census}, census) == []
    rep = CanaryReport(token="t", namespace="n", census_gaps=["entity_vectors"])
    assert rep.to_json()["census_gaps"] == ["entity_vectors"]


def test_the_probe_and_the_cli_read_the_same_orphan_residue() -> None:
    # The probe folds both orphan counts into its one-pass census; an
    # integration test holds it equal to these CLI statements on real rows.
    from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL
    from server.observability import otel_probes

    sql = otel_probes._invariants_sql()
    assert "FROM loom.entity_vectors FINAL" in sql and "FROM loom.entity_edges" in sql
    # knowledge-only namespaces are not orphans, in either statement
    assert (
        LIVE_CHUNK_SQL in sql
        and "doc_superseded_at IS NULL" in erasure_health.ORPHAN_GRAPH_NAMESPACES_SQL
    )
    assert (
        "LEFT ANTI JOIN" in erasure_health.ORPHAN_GRAPH_NAMESPACES_SQL
        and "LEFT ANTI JOIN" in erasure_health.ORPHAN_ENTITY_VECTORS_SQL
    )
    for col in ("agent", "user_id", "memory_key"):
        assert f"e.{col} = m.{col}" in erasure_health.ORPHAN_ENTITY_VECTORS_SQL


async def test_the_evidence_census_passes_a_refusal_through(monkeypatch) -> None:
    """A Postgres of another deployment is not a store that could not be read."""
    from server.core.pg import AuthorityMismatch

    async def _tables(*_a, **_k):
        return {}

    async def _refused(*_a, **_k):
        raise AuthorityMismatch("LOOM_PG_DSN names a database that records no authority")

    monkeypatch.setattr(erasure_census, "table_residue", _tables)
    monkeypatch.setattr(erasure_census, "pg_residue", _refused)
    with pytest.raises(AuthorityMismatch):
        await erasure_census.evidence_census("o", "namespace", "ns")


async def test_a_failure_outside_a_step_names_its_kind_only(monkeypatch) -> None:
    """A failure the step wrapper does not see (here, the saga itself) reaches the
    requester as its kind; the driver's text stays in the log."""

    async def saga(org, namespace, *, completed, row, verify=None):
        raise RuntimeError("Code: 210. Connection refused (ch-internal-7:9440)")

    audits: list[dict[str, Any]] = []
    out, _calls, checkpoints = await _run(monkeypatch, saga=saga, audits=audits)
    assert checkpoints[-1]["failure_reason"] == "RuntimeError"
    assert out.failure_reason == "RuntimeError"
    (failed,) = [a for a in audits if a["event_type"] == "erasure.failed"]
    assert failed["subject"]["error"] == "RuntimeError"


def test_a_stored_reason_keeps_its_step_and_kind_only() -> None:
    """A reason read back from the ledger (a saga from before the upgrade kept the
    driver's text) keeps the step and the kind, a Loom error's code and message, a
    residue failure's counts, and an abandoned saga's head with its cut last reason."""
    from server.erasure.domain.failure_reason import caller_reason

    residue = "verify.residue: ErasureResidueError: residue after erasure: {'loom.memory': 3}"
    typed = "memory.tombstone: BACKEND_UNAVAILABLE: ClickHouse is unavailable; retry"
    abandoned = "max_attempts (5) exceeded"
    assert {
        reason: caller_reason(reason)
        for reason in (
            "",
            "RuntimeError",
            "memory.tombstone: ValueError",
            f"memory.tombstone: ValueError: {_DRIVER_TEXT}",
            f"RuntimeError: {_DRIVER_TEXT}",
            "Connection refused (ch-internal-7:9440)",
            "Code: 241. DB::Exception: Memory limit (total) exceeded",
            "memory.tombstone: Code: 241. DB::Exception: Memory limit (total) exceeded",
            residue,
            typed,
            "OVERLOADED: the server is busy; retry",
            "external.delete_namespace: gaierror",
            "external.delete_namespace: ConnectTimeout",
            abandoned,
            f"{abandoned}; last: memory.tombstone: ValueError: {_DRIVER_TEXT}",
            f"{abandoned}; last: RuntimeError: {_DRIVER_TEXT}",
            f"{abandoned}; last: {typed}",
            f"{abandoned}; last: Connection refused (ch-internal-7:9440)",
        )
    } == {
        "": "",
        "RuntimeError": "RuntimeError",
        "memory.tombstone: ValueError": "memory.tombstone: ValueError",
        f"memory.tombstone: ValueError: {_DRIVER_TEXT}": "memory.tombstone: ValueError",
        f"RuntimeError: {_DRIVER_TEXT}": "RuntimeError",
        "Connection refused (ch-internal-7:9440)": "",
        "Code: 241. DB::Exception: Memory limit (total) exceeded": "",
        "memory.tombstone: Code: 241. DB::Exception: Memory limit (total) exceeded": (
            "memory.tombstone"
        ),
        residue: residue,
        typed: typed,
        "OVERLOADED: the server is busy; retry": "OVERLOADED: the server is busy; retry",
        # What this build writes for a kind that is not `...Error`: kept.
        "external.delete_namespace: gaierror": "external.delete_namespace: gaierror",
        "external.delete_namespace: ConnectTimeout": "external.delete_namespace: ConnectTimeout",
        abandoned: abandoned,
        f"{abandoned}; last: memory.tombstone: ValueError: {_DRIVER_TEXT}": (
            f"{abandoned}; last: memory.tombstone: ValueError"
        ),
        f"{abandoned}; last: RuntimeError: {_DRIVER_TEXT}": f"{abandoned}; last: RuntimeError",
        f"{abandoned}; last: {typed}": f"{abandoned}; last: {typed}",
        f"{abandoned}; last: Connection refused (ch-internal-7:9440)": abandoned,
    }


async def test_erasure_status_cuts_a_reason_stored_before_the_upgrade(monkeypatch) -> None:
    """A saga that failed under an older build and was never resumed: `erasure.status`
    shows the step and the kind, not the driver's text the ledger holds."""
    from server.auth.context import set_current_auth
    from server.erasure.api.erasure import ErasureStatusInput
    from server.erasure.app import handlers

    async def _row(erasure_id: str) -> dict[str, Any]:
        return {
            "id": erasure_id,
            "org": "o",
            "scope_type": "namespace",
            "scope_value": "ns",
            "requester": "r",
            "status": "failed",
            "current_step": 3,
            "requested_at": None,
            "completed_at": None,
            "failure_reason": (
                f"max_attempts (5) exceeded; last: memory.tombstone: ValueError: {_DRIVER_TEXT}"
            ),
        }

    set_current_auth(None)
    monkeypatch.setattr(erasure_ledger, "fetch_request_status", _row)
    out = await handlers.handle_erasure_status(ErasureStatusInput(erasure_id="e1"))
    assert out.failure_reason == "max_attempts (5) exceeded; last: memory.tombstone: ValueError"


async def test_a_reason_in_no_known_shape_reads_as_a_fixed_reason(monkeypatch) -> None:
    """A failed saga always says it failed: a stored reason the cut leaves nothing of
    reads as the fixed reason, never as `None` or `""`."""
    from server.auth.context import set_current_auth
    from server.erasure.api.erasure import ErasureListInput, ErasureStatusInput
    from server.erasure.app import handlers
    from server.erasure.domain.failure_reason import UNSHOWN_REASON

    # A driver's lead word (`Code`) is not a kind: nothing of this reason is shown.
    raw = "Code: 241. DB::Exception: Memory limit (total) exceeded (ch-internal-7:9440)"
    row = {
        "id": "e1",
        "org": "o",
        "scope_type": "namespace",
        "scope_value": "ns",
        "requester": "r",
        "status": "failed",
        "current_step": 3,
        "requested_at": _now(),
        "completed_at": None,
        "failure_reason": raw,
    }

    async def _row(erasure_id: str) -> dict[str, Any]:
        return row

    async def _rows(org, **_kw) -> list[dict[str, Any]]:
        return [row]

    set_current_auth(None)
    monkeypatch.setattr(erasure_ledger, "fetch_request_status", _row)
    monkeypatch.setattr(erasure_ledger, "list_requests", _rows)
    status = await handlers.handle_erasure_status(ErasureStatusInput(erasure_id="e1"))
    listed = await handlers.handle_erasure_list(ErasureListInput(org="o"))
    assert status.failure_reason == UNSHOWN_REASON
    assert [r.failure_reason for r in listed.rows] == [UNSHOWN_REASON]


async def test_erasure_list_cuts_a_reason_stored_before_the_upgrade(monkeypatch) -> None:
    from server.auth.context import set_current_auth
    from server.erasure.api.erasure import ErasureListInput
    from server.erasure.app import handlers

    async def _rows(org, **_kw) -> list[dict[str, Any]]:
        return [
            {
                "id": "e1",
                "scope_type": "namespace",
                "scope_value": "ns",
                "requester": "r",
                "status": "failed",
                "requested_at": _now(),
                "completed_at": None,
                "failure_reason": f"external.delete_namespace: ErasureBackendError: {_DRIVER_TEXT}",
            }
        ]

    set_current_auth(None)
    monkeypatch.setattr(erasure_ledger, "list_requests", _rows)
    out = await handlers.handle_erasure_list(ErasureListInput(org="o"))
    assert [r.failure_reason for r in out.rows] == [
        "external.delete_namespace: ErasureBackendError"
    ]


async def test_erasure_export_cuts_an_erasure_failure_written_before_the_upgrade(
    monkeypatch,
) -> None:
    """`erasure.export` returns the scope's audit rows: an `erasure.failed` subject an
    older build wrote with the driver's text shows the step and the kind."""
    from server.erasure.app import handlers
    from server.erasure.store import exports as erasure_exports

    failed = {"erasure_id": "e1", "error": f"memory.tombstone: ValueError: {_DRIVER_TEXT}"}
    other = {"key": "k", "note": _DRIVER_TEXT}

    async def _rows(org, namespace, *, limit, shown) -> list[dict[str, Any]]:
        return [
            {"event_type": "erasure.failed", "subject": json.dumps(failed)},
            {"event_type": "memory.set", "subject": json.dumps(other)},
        ]

    monkeypatch.setattr(erasure_exports, "export_audit_namespace", _rows)
    rows, _n, _more = await handlers._export_audit_log(
        "o", "namespace", "ns", limit=10, shown=erasure_exports._EVERY
    )
    assert json.loads(str(rows[0]["subject"]))["error"] == "memory.tombstone: ValueError"
    assert rows[1]["subject"] == json.dumps(other)  # another event as stored


def test_a_step_failing_with_a_loom_error_keeps_its_code_and_message() -> None:
    from server.core.errors import BackendUnavailableError

    err = saga_steps.ErasureStepError(
        "memory.tombstone", BackendUnavailableError("ClickHouse is unavailable; retry")
    )
    assert str(err) == "memory.tombstone: BACKEND_UNAVAILABLE: ClickHouse is unavailable; retry"
