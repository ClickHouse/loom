"""Actual staging failures retain bounded cancellation and fresh-plan evidence."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from bench import cell_trigger_gate as gate
from scripts.gates.testsupport.cell_trigger.fixture import native_failure_metadata
from server.core import pg
from server.memory.store import pg_memory
from tests.unit.test_cell_trigger_ci_creator import keeper_config_case as keeper_config_case
from tests.unit.test_cell_trigger_driver_failure_details import _main_case
from tests.unit.test_cell_trigger_gate_diagnostics import run_driver as run_driver

SECRET = "secret-stage-SQL-value-condition-credential"
MESSAGES = [
    ("canceling statement due to statement timeout", "statement_timeout"),
    ("canceling statement due to user request", "user_request"),
    (SECRET, "unknown"),
]
SUMMARY = {
    "latency_within_limit": True,
    "throughput_within_limit": True,
    "p99_delta_ms": 0.0,
    "throughput_loss": 0.0,
}


def canceled(message):
    try:
        raise asyncpg.PostgresError.new({"C": "57014", "M": message})
    except asyncpg.QueryCanceledError as cause:
        try:
            pg._raise_if_unavailable(cause)
        except pg.PostgresUnavailableError as error:
            return error
    raise AssertionError("the real PostgreSQL wrapper must refuse")


def plan_node(kind="Hash Join"):
    return {
        "Node Type": kind,
        "Join Type": "Inner",
        "Plan Rows": 0,
        "Startup Cost": 1.0,
        "Total Cost": 2.0,
        "Relation Name": SECRET,
        "Index Name": SECRET,
        "Hash Cond": SECRET,
        "Index Cond": SECRET,
        "Output": [SECRET],
        "SQL": SECRET,
    }


@pytest.fixture
def stage_case(monkeypatch, tmp_path, run_driver):
    def configure(*, fault=None, message=MESSAGES[0][0], plan_fault=None):
        artifact_error = OSError(SECRET) if fault == "artifact" else None
        cleanup_error = RuntimeError(SECRET) if fault == "explain" else None
        run, events = run_driver(
            prior=None, cleanup=cleanup_error, output=artifact_error, event="stage_error"
        )
        rows = [
            {
                "org": "owned",
                "namespace": "scratch",
                "agent": "bench",
                "user_id": "-",
                "session_id": "-",
                "memory_key": key,
                "memory_id": f"00000000-0000-0000-0000-{index:012d}",
            }
            for index, key in enumerate(("b", "a"), 1)
        ]
        block = gate.Block(rows=rows, audits=[{}, {}], documents=[])
        first = (
            None
            if fault == "healthy"
            else ValueError(SECRET)
            if fault == "noncancel"
            else KeyboardInterrupt(SECRET)
            if fault == "fatal"
            else canceled(message)
        )
        state = SimpleNamespace(first=first, explain=[], commits=[], block=block)
        conn = gate.asyncpg.connect.return_value
        monkeypatch.setattr(conn, "is_in_transaction", lambda: False, raising=False)

        async def fetchval(sql, *values):
            if sql.startswith("EXPLAIN"):
                state.explain.append((sql, values))
                assert sql == "EXPLAIN (FORMAT JSON) " + pg_memory._LOCK_HEADS
                assert values == (
                    ["owned", "owned"],
                    ["scratch", "scratch"],
                    ["bench", "bench"],
                    ["-", "-"],
                    ["-", "-"],
                    ["a", "b"],
                )
                if fault == "explain":
                    raise OSError(SECRET)
                if plan_fault == "oversized":
                    return " " * 65_537
                node = plan_node()
                if plan_fault == "deep":
                    for _ in range(9):
                        node = {**plan_node("Materialize"), "Plans": [node]}
                elif plan_fault == "nonfinite":
                    node["Total Cost"] = float("nan")
                else:
                    node["Plans"] = [plan_node("Index Scan"), plan_node("Function Scan")]
                return json.dumps([{"Plan": node, "Private": SECRET}])
            if "fresh" in sql:
                raise AssertionError("freshness is an invariant label, never SQL")
            if "memory_id=ANY" in sql:
                return 0
            assert "targets=7" in sql
            return len(rows)

        async def commit(actual_rows, actual_audits, **kwargs):
            state.commits.append((actual_rows, actual_audits, kwargs))
            assert actual_rows is block.rows and actual_audits is block.audits
            assert kwargs == {"targets": 7, "publisher_lease": False}
            if first is not None:
                raise first
            return SimpleNamespace(
                seqs=[("owned", "scratch", index) for index in (1, 2)],
                targets=[7, 7],
                rows=rows,
                audits=block.audits,
            )

        monkeypatch.setattr(conn, "fetchval", fetchval, raising=False)
        monkeypatch.setattr(gate, "_arm_order", lambda: (True,))
        monkeypatch.setattr(
            gate,
            "_memory_set",
            AsyncMock(return_value=[SimpleNamespace(measured=True, elapsed_ms=1.0)]),
        )
        monkeypatch.setattr(gate, "_settle_api", AsyncMock())
        monkeypatch.setattr(gate, "_build_block", AsyncMock(return_value=block))
        monkeypatch.setattr(gate, "_delivery", AsyncMock(return_value=100.0))
        monkeypatch.setattr(gate, "_verify", AsyncMock())
        monkeypatch.setattr(gate, "_physical", AsyncMock(return_value={}))
        monkeypatch.setattr(gate, "_debt", AsyncMock())
        monkeypatch.setattr(gate, "_absent", AsyncMock())
        monkeypatch.setattr(gate, "_statistics", lambda samples: SUMMARY.copy())
        monkeypatch.setattr(pg_memory, "commit_versions", commit)

        async def observe():
            try:
                result = await run()
            except BaseException as error:  # noqa: BLE001 — keep native fatals inside their owner.
                result = error
            records = [
                json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()
            ]
            assert SECRET not in json.dumps(records)
            return result, records

        return run, observe, events, state

    return configure


@pytest.mark.asyncio
@pytest.mark.parametrize("message,reason", MESSAGES)
async def test_actual_stage_records_cancellation_before_preserving_original(
    stage_case, message, reason
):
    _, observe, events, state = stage_case(message=message)
    result, records = await observe()
    stage = [record for record in records if record["kind"] == "stage_error"]
    assert len(stage) == 1  # Baseline fails here after the real _run -> _stage refusal.
    assert stage[0]["arm"] == 0 and stage[0]["key_count"] == 2
    assert type(stage[0]["elapsed_ms"]) in {int, float} and stage[0]["elapsed_ms"] >= 0
    assert result is state.first and len(state.commits) == len(state.explain) == 1
    metadata = native_failure_metadata(result)
    cause = metadata["driver"]["causes"][0]
    assert cause["cancel_reason"] == reason and cause["sqlstate"] == "57014"
    assert metadata["driver"]["outcome_unknown"] is False
    assert SECRET not in json.dumps(metadata)
    fresh = [record for record in records if record["kind"] == "freshplan_after_failure"]
    assert len(fresh) == 1
    assert fresh[0]["observer"] == "independent_connection" and fresh[0]["analyze"] is False
    assert fresh[0]["key_count"] == 2
    assert fresh[0]["nodes"] == [
        {
            "depth": depth,
            "node_type": kind,
            "join_type": "Inner",
            "plan_rows": 0,
            "startup_cost": 1.0,
            "total_cost": 2.0,
        }
        for depth, kind in ((0, "Hash Join"), (1, "Index Scan"), (1, "Function Scan"))
    ]
    assert events.index("stage_error") < events.index("restoration")
    assert "triggers_restored" in events and "public close" in events


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["noncancel", "fatal", "explain", "artifact"])
async def test_actual_stage_diagnostics_cannot_replace_work_or_cleanup(stage_case, fault):
    _, observe, events, state = stage_case(fault=fault)
    result, records = await observe()
    assert events.count("stage_error") == 1  # Causal baseline failure, not a new helper lookup.
    assert result is state.first and len(state.commits) == 1
    assert len(state.explain) == (0 if fault in {"noncancel", "fatal"} else 1)
    assert "restoration" in events and "public close" in events
    assert ("cleanup_error" in events) is (fault == "explain")
    assert any(record["kind"] == "freshplan_after_failure" for record in records) is (
        fault == "artifact"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_fault", ["oversized", "deep", "nonfinite"])
async def test_actual_stage_rejects_unsafe_plan_and_retains_cancellation(stage_case, plan_fault):
    _, observe, events, state = stage_case(plan_fault=plan_fault)
    result, records = await observe()
    assert any(record["kind"] == "stage_error" for record in records)
    assert result is state.first and len(state.explain) == 1
    assert not any(record["kind"] == "freshplan_after_failure" for record in records)
    assert "triggers_restored" in events and "public close" in events


@pytest.mark.parametrize("fault", ["healthy", None])
def test_actual_stage_reaches_main_verdict_and_retains_cleanup(
    monkeypatch, tmp_path, capsys, stage_case, fault
):
    from server.cli import _ch

    output, _ = _main_case(monkeypatch, tmp_path, None)
    run, _, events, state = stage_case(fault=fault)
    monkeypatch.setattr(_ch, "run_connected", lambda *_args, **_kwargs: asyncio.run(run()))
    assert gate.main() == (0 if fault == "healthy" else 1)
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == ("PASS" if fault == "healthy" else "INVALID")
    assert len(state.commits) == 1 and "triggers_restored" in events and "public close" in events
    if fault == "healthy":
        assert not state.explain and "settled" in events and "arm" in events
        assert not any(kind in events for kind in ("stage_error", "freshplan_after_failure"))
        assert json.loads(output.read_text().splitlines()[-1])["kind"] == "driver_cleanup"
    else:
        assert "stage_error" in events and len(state.explain) == 1
        assert verdict["error_type"] == "PostgresUnavailableError"
        assert verdict["failure"]["driver"]["outcome_unknown"] is False
        assert json.loads(output.read_text().splitlines()[-1])["kind"] == "invalid"
        assert SECRET not in json.dumps(verdict)


@pytest.mark.parametrize(
    "message,reason,fault",
    [(message, reason, None) for message, reason in MESSAGES]
    + [(MESSAGES[0][0], "statement_timeout", fault) for fault in ("enum", "sqlstate", "extra")],
)
def test_native_action_reads_only_closed_cancellation_evidence(
    keeper_config_case, message, reason, fault
):
    native, instance, owner, _, _ = keeper_config_case
    instance.s["python"] = "unused"
    metadata = native_failure_metadata(canceled(message))
    cause = metadata["driver"]["causes"][0]
    assert cause.get("cancel_reason") == reason  # Baseline misses the actual server reason.
    expected = deepcopy(metadata)
    if fault == "enum":
        cause["cancel_reason"] = SECRET
    elif fault == "sqlstate":
        cause["sqlstate"] = "08006"
    elif fault == "extra":
        cause["message"] = SECRET
    native.write(instance.w / "pg_cell-created.json.failure.json", metadata)
    original = native.Refused(SECRET)

    def refused(*_args, **_kwargs):
        raise original

    owner.command = refused
    with pytest.raises(native.Refused) as caught:
        instance.action("create", role="pg_cell")
    assert caught.value is original
    assert original.native == (expected if fault is None else None)
    assert SECRET not in json.dumps(original.native)
