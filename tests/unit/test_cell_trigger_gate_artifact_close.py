"""Artifact close retains its first flush, sync or file-close failure."""

import asyncio
import dataclasses
import json

import pytest

from bench import cell_trigger_gate as gate
from tests.unit.test_bench_cell_trigger_gate import fixture_config, receipt_posture


@pytest.mark.parametrize("phase", ["flush", "fsync"])
@pytest.mark.parametrize(
    "first_kind", [None, OSError, ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt]
)
@pytest.mark.parametrize(
    "second_kind",
    [None, OSError, ValueError, asyncio.CancelledError, SystemExit, KeyboardInterrupt],
)
def test_actual_artifact_close_retains_first_flush_sync_or_close_failure(
    monkeypatch, tmp_path, phase, first_kind, second_kind
):
    first = first_kind("flush or sync failed first") if first_kind is not None else None
    second = second_kind("file close failed second") if second_kind is not None else None
    artifact = gate.Artifact(str(tmp_path / "raw.jsonl"))
    fixture, config = fixture_config()
    artifact.emit(
        "configuration",
        manifest=dataclasses.asdict(fixture),
        shared_configuration_excludes=["cell_router_token"],
        credential_posture=receipt_posture(),
        api_configuration_attested=True,
    )
    original = artifact.file
    events = []

    class File:
        def flush(self):
            events.append("flush")
            if phase == "flush" and first is not None:
                raise first
            return original.flush()

        def close(self):
            events.append("file close")
            original.close()
            if second is not None:
                raise second

        def __getattr__(self, name):
            return getattr(original, name)

    def sync(_fd):
        events.append("fsync")
        if phase == "fsync" and first is not None:
            raise first

    artifact.file = File()
    monkeypatch.setattr(gate.os, "fsync", sync)
    selected = first if first is not None else second
    if selected is None:
        artifact.close()
    else:
        with pytest.raises(BaseException) as observed:
            artifact.close()
        assert observed.value is selected
    assert original.closed and events[-1] == "file close"
    assert events.count("file close") == 1
    assert ("fsync" in events) == (phase != "flush" or first is None)
    record = json.loads((tmp_path / "raw.jsonl").read_text())
    assert record["manifest"]["schema"] == 2
    assert record["manifest"]["cli_configuration_sha256"] == gate._digest(config.model_dump())
    assert "configuration_sha256" not in record["manifest"]
    assert record["shared_configuration_excludes"] == ["cell_router_token"]
    assert record["credential_posture"] == receipt_posture()
    assert config.pg_dsn not in json.dumps(record)
