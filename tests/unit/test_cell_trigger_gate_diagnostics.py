"""Artifact diagnostics retain the first driver failure at the actual owner."""

import argparse
import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock

import pytest

from bench import cell_trigger_gate as gate
from server.cli import cell_attach_pg as apg


@pytest.fixture
def run_driver(monkeypatch, tmp_path):
    from server.core import background

    def configure(*, prior=None, cleanup=None, output=None, event="cleanup_error"):
        events = []

        class Conn:
            async def close(self, **kwargs):
                events.append("public close")

            def terminate(self):
                events.append("abort")

        class Triggers:
            tables = ("memory_current",)
            invariant = org = "digest"

            async def set(self, *args):
                pass

            async def restore(self, *args):
                events.append("restoration")
                if cleanup is not None:
                    raise cleanup

        artifact = gate.Artifact(str(tmp_path / "raw.jsonl"))
        original = artifact.file

        class File:
            def write(self, line):
                kind = json.loads(line)["kind"]
                events.append(kind)
                if kind == event and output is not None:
                    raise output
                return original.write(line)

            def __getattr__(self, name):
                return getattr(original, name)

        artifact.file = File()
        monkeypatch.setattr(gate.asyncpg, "connect", AsyncMock(return_value=Conn()))
        monkeypatch.setattr(gate, "_proof", AsyncMock(return_value={}))
        monkeypatch.setattr(gate.Triggers, "capture", AsyncMock(return_value=Triggers()))
        monkeypatch.setattr(gate, "_memory_set", AsyncMock(side_effect=prior))
        if prior is None:
            monkeypatch.setattr(gate, "_arm_order", lambda: ())
        monkeypatch.setattr(gate, "_statistics", lambda samples: {})
        monkeypatch.setattr(background, "drain_task_sets", AsyncMock(return_value=True))
        monkeypatch.setattr(background, "flush_registered_buffers", AsyncMock(return_value=True))

        async def run():
            try:
                return await gate._run(
                    argparse.Namespace(pg_dsn="unused"), None, artifact, object()
                )
            finally:
                artifact.close()

        return run, events

    return configure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior_kind", [None, ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
@pytest.mark.parametrize(
    "output_kind",
    [None, OSError, RuntimeError, asyncio.CancelledError, SystemExit, KeyboardInterrupt],
)
async def test_outer_cleanup_diagnostic_preserves_selected_first_error(
    run_driver, prior_kind, output_kind
):
    prior = prior_kind("work failed first") if prior_kind is not None else None
    cleanup = ValueError("cleanup failed")
    output = output_kind("diagnostic failed") if output_kind is not None else None
    run, events = run_driver(prior=prior, cleanup=cleanup, output=output)

    async def owner():
        try:
            await run()
        except BaseException as error:  # noqa: BLE001 — assert the actual owner failure.
            assert error is (prior if prior is not None else cleanup)
            assert events.count("cleanup_error") == 1
            assert events.index("public close") < events.index("cleanup_error")
            return
        raise AssertionError("selected failure was swallowed")

    assert await apg._work_result(owner) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["provisional_statistics", "triggers_restored"])
@pytest.mark.parametrize(
    "output_kind", [OSError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
async def test_required_output_failure_after_successful_work_still_fails(
    run_driver, event, output_kind
):
    first = output_kind("required artifact failed")
    run, events = run_driver(output=first, event=event)

    async def owner():
        try:
            await run()
        except BaseException as error:  # noqa: BLE001 — native errors stay in their owner.
            assert error is first
            assert "restoration" in events and "public close" in events
            return
        raise AssertionError("required output failure was swallowed")

    assert await apg._work_result(owner) is None


@pytest.mark.asyncio
async def test_successful_driver_and_cleanup_output_returns_normally(run_driver):
    run, events = run_driver()
    assert await run() == {}
    assert events == ["provisional_statistics", "restoration", "triggers_restored", "public close"]


@pytest.mark.parametrize("phase", ["invalid", "close"])
@pytest.mark.parametrize(
    "prior_kind", [None, ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
@pytest.mark.parametrize(
    "output_kind",
    [None, OSError, RuntimeError, asyncio.CancelledError, SystemExit, KeyboardInterrupt],
)
def test_main_preserves_first_invalid_and_requires_successful_artifact_close(
    monkeypatch, tmp_path, capsys, phase, prior_kind, output_kind
):
    import sys

    import server.settings
    import server.wiring
    from server.cli import _ch
    from tests.unit.test_bench_cell_trigger_gate import args, fixture_config, launch_files

    fixture, config = fixture_config()
    fixture, launch_path, _, _, _ = launch_files(tmp_path, fixture, config)
    manifest_path = tmp_path / "fixture.json"
    manifest_path.write_text(json.dumps(dataclasses.asdict(fixture)))
    prior = prior_kind("first native work failure") if prior_kind is not None else None
    output = output_kind("secondary artifact failure") if output_kind is not None else None
    events = []
    real_artifact = gate.Artifact

    class Artifact(real_artifact):
        def __init__(self, path):
            super().__init__(path)
            original = self.file

            class File:
                def write(self, line):
                    kind = json.loads(line)["kind"]
                    events.append(kind)
                    if phase == "invalid" and kind == "invalid" and output is not None:
                        raise output
                    return original.write(line)

                def close(self):
                    events.append("file closed")
                    return original.close()

                def __getattr__(self, name):
                    return getattr(original, name)

            self.file = File()

    argv = ["driver"]
    for key, value in vars(
        args(
            fixture_manifest=str(manifest_path),
            launch_manifest=str(launch_path),
            output=str(tmp_path / "raw.jsonl"),
        )
    ).items():
        argv += ["--" + key.replace("_", "-"), str(value)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server.settings, "settings", config)
    monkeypatch.setattr(server.wiring, "wire", lambda: None)
    monkeypatch.setattr(gate, "_source", lambda _: {})
    monkeypatch.setattr(gate, "_owned_server", lambda _: None)
    monkeypatch.setattr(gate, "_preflight", AsyncMock())
    monkeypatch.setattr(gate, "Artifact", Artifact)

    def native(*_args, **_kwargs):
        if prior is not None:
            raise prior
        return {
            "latency_within_limit": True,
            "throughput_within_limit": True,
            "p99_delta_ms": 0.0,
            "throughput_loss": 0.0,
        }

    def sync(_fd):
        assert capsys.readouterr().out == ""
        events.append("file sync")
        if phase == "close" and output is not None:
            raise output

    monkeypatch.setattr(_ch, "run_connected", native)
    monkeypatch.setattr(gate.os, "fsync", sync)
    invalid = prior is not None or (phase == "close" and output is not None)
    try:
        result = gate.main()
    except BaseException as error:  # noqa: BLE001 — an escaped fatal must fail this case only.
        pytest.fail(f"main escaped {type(error).__name__}")
    assert result == int(invalid)
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == ("INVALID" if invalid else "PASS")
    if prior is not None:
        assert verdict["error_type"] == prior_kind.__name__
        assert verdict["phase"] == "native_driver"
    elif invalid:
        assert verdict["error_type"] == output_kind.__name__
        assert verdict["phase"] == "artifact_close"
    assert events[-2:] == ["file sync", "file closed"]
    assert not any(
        json.loads(line).get("verdict") == "PASS"
        for line in (tmp_path / "raw.jsonl").read_text().splitlines()
    )
