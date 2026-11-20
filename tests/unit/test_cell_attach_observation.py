"""Cell observations report this process without changing comparable fingerprints."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.core import cell, deployment_config, instances, provider_process, retirement
from server.core.ch import current_query_tag
from server.settings import settings
from tests.unit.fixtures.cell_matches_previous import matches as previous_matches

IDENTITY = UUID("00000000-0000-0000-0000-000000000019")
PROOF = "host|boot|container|77|123|901|||cell-retirement-v1"


@pytest.fixture
def own_process(monkeypatch):
    monkeypatch.setattr(provider_process, "process_identity", lambda: IDENTITY)
    monkeypatch.setattr(
        provider_process, "host_proof", lambda: PROOF.removesuffix("|cell-retirement-v1")
    )
    monkeypatch.setattr(settings, "instance_heartbeat_seconds", 1.001)
    monkeypatch.setattr(settings, "serves_traffic", True)
    monkeypatch.setattr(cell, "gate_reason", lambda: "")
    return retirement.get_gate()


@pytest.mark.parametrize("role", ["server", "worker"])
def test_the_observation_binds_the_actual_guarded_process(own_process, role):
    own_process.install(role)
    reported = json.loads(instances.cell_row()["fingerprint"])
    assert reported.get("attach_observation") == {
        "protocol": 1,
        "role": role,
        "participant_id": str(IDENTITY),
        "host_proof": PROOF,
        "keys_fp": deployment_config.fingerprints(settings)["keys_fp"],
        "accepting": True,
        "period_ms": 1001,
    }
    assert reported["participant_id"] == str(IDENTITY)
    assert reported["v"] == cell.FINGERPRINT_VERSION
    assert reported["fields"] == cell.mine()
    assert reported["serves"] is (role == "server")
    assert settings.serves_traffic is True
    assert previous_matches(cell.mine(), reported)
    assert "attach_observation" not in reported["fields"]


def test_observation_acceptance_is_the_actual_cell_gate(own_process, monkeypatch):
    own_process.install("worker")
    monkeypatch.setattr(cell, "gate_reason", lambda: "cell state not read")
    reported = json.loads(instances.cell_row()["fingerprint"])
    assert reported["attach_observation"]["accepting"] is False


def test_a_long_integral_interval_remains_a_truthful_normal_observation(own_process, monkeypatch):
    own_process.install("server")
    monkeypatch.setattr(settings, "instance_heartbeat_seconds", 75.125)
    reported = json.loads(instances.cell_row()["fingerprint"])
    assert reported["attach_observation"]["period_ms"] == 75125


@pytest.mark.parametrize(
    "fault",
    [
        "uninstalled",
        "command",
        "birth",
        "malformed-birth",
        "fraction",
        "zero",
        "negative",
        "nan",
        "inf",
    ],
)
def test_unsupported_process_observation_never_invents_capability(own_process, monkeypatch, fault):
    if fault != "uninstalled":
        own_process.install("command" if fault == "command" else "server")
    if fault in {"birth", "malformed-birth"}:
        monkeypatch.setattr(
            provider_process, "host_proof", lambda: "" if fault == "birth" else "unsupported"
        )
    if fault in {"fraction", "zero", "negative", "nan", "inf"}:
        monkeypatch.setattr(
            settings,
            "instance_heartbeat_seconds",
            {
                "fraction": 1.0005,
                "zero": 0.0,
                "negative": -1.0,
                "nan": float("nan"),
                "inf": float("inf"),
            }[fault],
        )
    reported = json.loads(instances.cell_row()["fingerprint"])
    assert "attach_observation" in reported and reported["attach_observation"] is None
    assert reported["participant_id"] == str(IDENTITY)
    assert reported["fields"] == cell.mine()
    assert previous_matches(cell.mine(), reported)


@pytest.mark.asyncio
async def test_cell_only_loop_retries_a_missed_beat_and_stops_its_owned_task(monkeypatch):
    ch = SimpleNamespace(insert=AsyncMock())
    expected = RuntimeError("missed heartbeat")
    ch.insert.side_effect = [None, expected, None]
    entered = asyncio.Event()
    resume = asyncio.Event()
    period = settings.instance_heartbeat_seconds
    sleeps = []
    tags = []

    async def sleep(delay):
        sleeps.append(delay)
        tags.append(current_query_tag())
        if len(sleeps) == 3:
            entered.set()
            await resume.wait()  # The test owns this blocked timer and releases it below.

    monkeypatch.setattr(instances.asyncio, "sleep", sleep)
    task = None
    try:
        task = await instances.start_cell_heartbeat(ch)
        async with asyncio.timeout(0.5):
            await entered.wait()
        assert sleeps == [period, period, period]
        assert tags == [instances.QUERY_TAG] * 3
        assert ch.insert.await_count == 3
        assert all(call.args[0] == instances.CELL_TABLE for call in ch.insert.await_args_list)
        assert current_query_tag() == ""
    finally:
        if task is not None:
            task.cancel()
        resume.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
