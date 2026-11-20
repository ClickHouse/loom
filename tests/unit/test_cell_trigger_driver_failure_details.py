"""The native driver retains finite Postgres failure evidence without payloads."""

import asyncio
import dataclasses
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

import bench.cell_trigger_gate as gate
from tests.unit.test_bench_cell_trigger_gate import args, fixture_config
from tests.unit.test_cell_trigger_ci_creator import keeper_config_case as keeper_config_case
from tests.unit.test_cell_trigger_native_failure_details import detail


@pytest.mark.parametrize("fault", ["query_cancel", "connection", "unknown_commit"])
def test_actual_stage_failure_retains_pg_cause_in_invalid_result(
    monkeypatch, tmp_path, capsys, fault
):
    output, calls = _main_case(monkeypatch, tmp_path, fault)
    assert gate.main() == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == "INVALID"
    assert verdict["phase"] == "native_driver"
    assert verdict["reason"] == "operation refused"
    assert verdict["error_type"] == "PostgresUnavailableError"
    assert calls == ["wire", "stage", "native_returned"]
    assert "secret-trigger-diagnostic-payload" not in json.dumps(verdict)
    assert "failure" in verdict
    metadata = verdict["failure"]
    assert metadata["error_type"] == "PostgresUnavailableError"
    assert metadata["site"]["file"] == "pg.py"
    assert metadata["site"]["function"] == "_raise_if_unavailable"
    assert metadata["site"]["line"] > 0
    assert metadata["driver"]["outcome_unknown"] is (fault == "unknown_commit")
    expected = (
        {"error_type": "ConnectionResetError", "errno": 104}
        if fault == "connection"
        else {
            "error_type": "QueryCanceledError",
            "errno": None,
            "sqlstate": "57014",
            "cancel_reason": "unknown",
        }
    )
    assert metadata["driver"]["causes"] == [expected]
    invalid = json.loads(output.read_text().splitlines()[-1])
    assert invalid["kind"] == "invalid" and invalid["failure"] == metadata


def test_healthy_native_driver_keeps_pass_without_failure_metadata(monkeypatch, tmp_path, capsys):
    output, calls = _main_case(monkeypatch, tmp_path, None)
    assert gate.main() == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == "PASS" and "failure" not in verdict
    assert calls == ["wire", "native_returned"]
    assert json.loads(output.read_text().splitlines()[-1])["kind"] == "driver_cleanup"


def _main_case(monkeypatch, tmp_path, fault):
    import server.settings
    import server.wiring
    from server.cli import _ch
    from server.core import pg
    from server.memory.store import pg_memory

    fixture, config = fixture_config()
    manifest = tmp_path / "manifest"
    manifest.write_text(json.dumps(dataclasses.asdict(fixture)))
    output = tmp_path / "driver.jsonl"
    argv = ["driver"]
    for name, value in {
        **vars(args()),
        "fixture_manifest": str(manifest),
        "output": str(output),
    }.items():
        argv += ["--" + name.replace("_", "-"), str(value)]
    calls = []
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(server.wiring, "wire", lambda: calls.append("wire"))
    monkeypatch.setattr(gate, "_preflight", AsyncMock())
    monkeypatch.setattr(gate, "_source", lambda _: {"clean": True})
    monkeypatch.setattr(gate, "_launch", lambda *_: {})
    monkeypatch.setattr(gate, "_debt", AsyncMock())
    monkeypatch.setattr(gate, "_absent", AsyncMock())

    async def commit(*_args, **_kwargs):
        calls.append("stage")
        cause = (
            ConnectionResetError(104, "secret-trigger-diagnostic-payload")
            if fault == "connection"
            else asyncpg.QueryCanceledError("secret-trigger-diagnostic-payload")
        )
        pg._raise_if_unavailable(cause, outcome_unknown=fault == "unknown_commit")
        raise AssertionError("the actual PG wrapper must refuse")

    monkeypatch.setattr(pg_memory, "commit_versions", commit)

    def native(*_args, **_kwargs):
        try:
            if fault:
                block = gate.Block(
                    rows=[
                        {
                            "org": "owned",
                            "namespace": "owned",
                            "memory_id": "00000000-0000-0000-0000-000000000011",
                            "memory_key": "owned",
                        }
                    ],
                    audits=[],
                    documents=[],
                )
                asyncio.run(
                    gate._stage(
                        SimpleNamespace(fetchval=AsyncMock(return_value=0)), object(), block
                    )
                )
            return {
                "latency_within_limit": True,
                "throughput_within_limit": True,
                "p99_delta_ms": 0.0,
                "throughput_loss": 0.0,
            }
        finally:
            calls.append("native_returned")

    monkeypatch.setattr(_ch, "run_connected", native)
    return output, calls


@pytest.mark.parametrize("cause_state", ["57014", "08006", "secret-query-state", True])
def test_shared_sanitizer_keeps_only_closed_pg_values(cause_state):
    from scripts.gates.testsupport.cell_trigger.fixture import native_failure_metadata
    from server.core.pg import PostgresUnavailableError

    cause = OSError(104, "secret-trigger-diagnostic-payload")
    cause.sqlstate = cause_state
    error = PostgresUnavailableError("secret-trigger-diagnostic-payload", outcome_unknown=True)
    error.__cause__ = cause
    metadata = native_failure_metadata(error)
    assert metadata["driver"]["outcome_unknown"] is True
    entry = metadata["driver"]["causes"][0]
    assert entry["error_type"] == "ConnectionResetError" and entry["errno"] == 104
    if type(cause_state) is str and cause_state in {"57014", "08006"}:
        assert entry["sqlstate"] == cause_state
    else:
        assert "sqlstate" not in entry
    assert "secret-trigger-diagnostic-payload" not in json.dumps(metadata)
    assert len(json.dumps(metadata)) < 4096


def test_shared_sanitizer_preserves_existing_transport_shape_and_cycle_bound():
    from clickhouse_connect.driver.exceptions import OperationalError

    from scripts.gates.testsupport.cell_trigger.fixture import native_failure_metadata

    error = OperationalError("secret-trigger-diagnostic-payload", code=159)
    cause = ConnectionRefusedError(111, "secret-trigger-diagnostic-payload")
    error.__cause__ = cause
    cause.__cause__ = error
    metadata = native_failure_metadata(error)
    assert metadata["driver"] == {
        "code": 159,
        "causes": [{"error_type": "ConnectionRefusedError", "errno": 111}],
    }
    assert "secret-trigger-diagnostic-payload" not in json.dumps(metadata)


def test_diagnostic_error_cannot_replace_actual_stage_invalid(monkeypatch, tmp_path, capsys):
    output, calls = _main_case(monkeypatch, tmp_path, "query_cancel")

    def failed(*_args, **_kwargs):
        raise RuntimeError("secret-trigger-diagnostic-payload")

    monkeypatch.setattr(gate, "native_failure_metadata", failed)
    assert gate.main() == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == "INVALID" and verdict["error_type"] == "PostgresUnavailableError"
    assert verdict["phase"] == "native_driver" and verdict["reason"] == "operation refused"
    assert "failure" not in verdict
    assert calls == ["wire", "stage", "native_returned"]
    invalid = json.loads(output.read_text().splitlines()[-1])
    assert invalid["kind"] == "invalid" and "failure" not in invalid
    assert "secret-trigger-diagnostic-payload" not in json.dumps(verdict)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "root_sqlstate",
        "cause_sqlstate",
        "boolean_sqlstate",
        "outcome_unknown",
        "site_file",
        "site_line",
        "site_function",
        "site_extra",
        "driver_extra",
    ],
)
def test_native_reader_validates_new_pg_fields_without_replacing_first_error(
    keeper_config_case, fault
):
    native, instance, owner, _, _ = keeper_config_case
    instance.s["python"] = "unused"
    metadata = detail()
    driver = metadata["driver"]
    driver.update(sqlstate="57014", outcome_unknown=True)
    cause = driver["causes"][0]
    cause.update(
        sqlstate="08006", site={"file": "native.py", "line": 702, "function": "connections"}
    )
    if fault == "root_sqlstate":
        driver["sqlstate"] = "secret-query"
    elif fault == "cause_sqlstate":
        cause["sqlstate"] = "secret-query"
    elif fault == "boolean_sqlstate":
        driver["sqlstate"] = True
    elif fault == "outcome_unknown":
        driver["outcome_unknown"] = 1
    elif fault == "site_file":
        cause["site"]["file"] = "/secret/path/native.py"
    elif fault == "site_line":
        cause["site"]["line"] = True
    elif fault == "site_function":
        cause["site"]["function"] = "secret-payload"
    elif fault == "site_extra":
        cause["site"]["message"] = "secret-payload"
    elif fault == "driver_extra":
        driver["message"] = "secret-payload"
    native.write(instance.w / "pg_cell-created.json.failure.json", metadata)
    original = native.Refused("original-command-failure")

    def refused(*_args, **_kwargs):
        raise original

    owner.command = refused
    with pytest.raises(native.Refused) as caught:
        instance.action("create", role="pg_cell")
    assert caught.value is original
    assert original.native == (metadata if fault == "none" else None)
