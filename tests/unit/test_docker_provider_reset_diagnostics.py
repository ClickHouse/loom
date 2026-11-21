"""Failure evidence preserves the provider smoke's original reset refusal."""

import io
import json
import subprocess
import urllib.error
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import scripts.gates.docker_provider_data as data
import scripts.gates.docker_provider_smoke as smoke

TRANSITION = str(UUID(int=17))


@pytest.mark.parametrize("diagnostic_failure", [None, RuntimeError, KeyboardInterrupt])
def test_reset_failure_collects_diagnostic_and_preserves_first_error(
    monkeypatch, tmp_path, diagnostic_failure
):
    events, failure = _provider_flow(monkeypatch, tmp_path, diagnostic_failure=diagnostic_failure)
    with pytest.raises(AssertionError) as caught:
        smoke.smoke_providers("http://fixture", events["compose"])
    assert caught.value is failure
    assert events["checks"][-1] == ("diagnostic", TRANSITION)
    assert events["waits"] == 4
    assert events["resets"] == 1
    assert events["checks"].count(("crash-worker",)) == 1
    assert (
        "retired",
        json.dumps([{"participant_id": "first"}, {"participant_id": "second"}]),
    ) in events["checks"]
    assert not any(args[0] == "environment" for args in events["checks"])


def test_healthy_reset_does_not_collect_diagnostic(monkeypatch, tmp_path):
    events, _ = _provider_flow(monkeypatch, tmp_path, healthy=True)
    smoke.smoke_providers("http://fixture", events["compose"])
    assert not any(args[0] == "diagnostic" for args in events["checks"])
    assert events["checks"][-1] == ("environment", "8")
    assert events["waits"] == 4
    assert events["resets"] == 1


def _provider_flow(monkeypatch, tmp_path, *, diagnostic_failure=None, healthy=False):
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"services": {"loom": {}}}))
    events = {
        "compose": ["docker", "compose", "-f", str(override)],
        "checks": [],
        "waits": 0,
        "resets": 0,
    }
    failure = AssertionError("Timed out waiting for provider activation")
    environment = {
        "source": "environment",
        "embedding_model": "environment",
        "embedding_dimensions": 8,
        "llm_model": "fake",
        "pending": None,
    }
    active = {
        "source": "dashboard",
        "pending": None,
        "embedding_model": "docker-smoke-16",
        "embedding_dimensions": 16,
    }
    statuses = iter(
        [
            {
                **active,
                "embedding_model": "docker-smoke-8",
                "configured_keys": {"compatible": True},
            },
            {
                "embedding_model": "docker-smoke-8",
                "pending": {"state": "failed", "error_code": "rebuild_rebuild_failed"},
            },
            active,
            environment,
        ]
    )
    model_reads = iter([environment, active])
    applications = iter(["queued", "active", "queued"])

    def request(_base, path, _payload=None):
        if path == "/v1/admin/models":
            return next(model_reads)
        raise urllib.error.HTTPError("http://fixture", 503, "closed", {}, None)

    def configure(*_args):
        raise urllib.error.HTTPError(
            "http://fixture", 422, "width", {}, io.BytesIO(b"unsupported_dimensions")
        )

    def dashboard(_base, action, *_args):
        events["resets"] += action == "reset"
        return {
            "state": next(applications) if action == "apply" else "queued",
            "config_id": TRANSITION,
        }

    def check(_compose, *args):
        events["checks"].append(args)
        if args[0] == "diagnostic" and diagnostic_failure is not None:
            raise diagnostic_failure("diagnostic secret must not replace the original")
        return (
            [{"participant_id": "first"}, {"participant_id": "second"}]
            if args[0] == "workers"
            else None
        )

    def status(_base, predicate):
        events["waits"] += 1
        value = next(statuses)
        assert predicate(value)
        if events["waits"] == 4 and not healthy:
            raise failure
        return value

    monkeypatch.setattr(smoke, "request", request)
    monkeypatch.setattr(smoke, "configure", configure)
    monkeypatch.setattr(smoke, "dashboard_action", dashboard)
    monkeypatch.setattr(smoke, "data_check", check)
    monkeypatch.setattr(smoke, "wait_status", status)
    monkeypatch.setattr(smoke, "wait_admission", lambda *_: None)
    monkeypatch.setattr(smoke, "control", lambda *_: None)
    monkeypatch.setattr(smoke, "run", lambda *_: None)
    return events, failure


def test_diagnostic_exec_is_bounded_and_discards_stderr(monkeypatch):
    calls = []
    monkeypatch.setattr(
        smoke.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    smoke.data_check(["docker", "compose", "-f", "owned.json"], "diagnostic", TRANSITION)
    args, kwargs = calls[0]
    assert args[0][-2:] == ["diagnostic", TRANSITION]
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is True
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert "capture_output" not in kwargs


@pytest.mark.asyncio
async def test_diagnostic_emits_only_closed_bounded_counts(monkeypatch, capsys):
    from types import SimpleNamespace

    row = _counts()
    query = AsyncMock(return_value=row)
    monkeypatch.setattr(
        data.provider_process, "host_proof", lambda: "host|boot|container|namespace|55|90"
    )
    await data.diagnostic(SimpleNamespace(query_one=query), TRANSITION)
    sql, params = query.await_args.args
    assert sql.lstrip().startswith("WITH retained AS (SELECT")
    assert "ANY LEFT JOIN" in sql and "LIMIT 2" in sql
    for guard in (
        "max_execution_time = 3",
        "max_rows_to_read = 10000",
        "max_bytes_to_read = 10485760",
        "max_result_rows = 1",
        "max_result_bytes = 4096",
        "max_memory_usage = 134217728",
    ):
        assert guard in sql
    assert params == {
        "name": "provider_configuration",
        "transition": TRANSITION,
        "scope": ["host", "boot", "container", "namespace"],
    }
    output = capsys.readouterr().out
    assert json.loads(output.split(": ", 1)[1]) == row
    assert len(output) < 1024
    assert "host|boot" not in output and TRANSITION not in output
    assert query.await_count == 1


@pytest.mark.parametrize("fault", ["extra", "bool", "large", "negative", "missing", "none"])
@pytest.mark.asyncio
async def test_diagnostic_refuses_invalid_aggregate_without_output(monkeypatch, capsys, fault):
    from types import SimpleNamespace

    row = _counts()
    if fault == "extra":
        row["payload"] = "secret"
    elif fault == "bool":
        row["retained"] = True
    elif fault == "large":
        row["retained"] = 10001
    elif fault == "negative":
        row["retained"] = -1
    elif fault == "missing":
        del row["retained"]
    else:
        row = None
    monkeypatch.setattr(
        data.provider_process, "host_proof", lambda: "host|boot|container|namespace|55|90"
    )
    with pytest.raises(ValueError, match="Invalid provider diagnostic aggregate"):
        await data.diagnostic(SimpleNamespace(query_one=AsyncMock(return_value=row)), TRANSITION)
    assert capsys.readouterr().out == ""


def _counts():
    return {
        "retained": 4,
        "running": 1,
        "matching_drained": 3,
        "other_drained": 0,
        "retained_retired": 0,
        "expired": 1,
        "same_scope": 3,
        "lease_rows": 1,
        "expired_leases": 0,
        "retained_holders": 1,
        "matching_drained_holders": 1,
    }
