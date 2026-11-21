"""Cross-store update ordering and rerun choices use only recorded fingerprints."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from server.cli import cell_control, cell_update_participants, cell_update_pg, cell_update_run
from server.core import cell


def value(issuer="old", *, scim=False, orgs=()):
    return cell.as_value(
        {
            "deployment_id": "dep-first",
            "public_host": "first.example.test",
            "oauth_issuer_host": "first.example.test",
            "bearer_issuers": [issuer],
            "configured_orgs": list(orgs),
            "scim": scim,
        }
    )


def row(current=None, pending=None):
    return {
        "cell": "first",
        "is_first": True,
        "host": "first.example.test",
        "deployment_id": "dep-first",
        "bucket": "",
        "bucket_prefix": "",
        "probe_url": "http://probe",
        "upstream_url": "http://main",
        "current_fp": current,
        "next_fp": pending,
        "mutation_generation": str(UUID(int=1)),
        "state": "serving",
        "mode": "enforced",
    }


@pytest.fixture
def coordinator(monkeypatch):
    events = []
    run = cell_update_run.Run(
        None,
        Namespace(cell="first", config=None, finish=False, revert=False, probe_url=None, wait=0),
        httpx.URL("http://router"),
        None,
    )
    run.owner = SimpleNamespace(conn=object())
    run.row = row(value(), value("new", orgs=("held",)))
    run.state = cell_update_pg.State("dep-first", True, "first", "enforced", value(), None)
    run.target = None

    async def held(current, pending):
        events.append("placement-proof")
        return ["held"]

    async def write(owner, expected, registered, current, pending, placed):
        events.append("local-write")
        assert registered is run.row
        assert placed == ["held"] or registered["current_fp"] is None
        run.state = replace(expected, current=current, pending=pending)
        return run.state

    async def durable():
        events.append("durable")
        return run.state

    async def publish(target, action):
        events.append("directory-" + action)
        current, pending = cell_update_run.fingerprints.configuration_pair(
            run.row["current_fp"], run.row["next_fp"], target, action
        )
        return run.row | {"current_fp": current, "next_fp": pending}

    async def settle(selected, target):
        events.append("turnover")
        assert selected is run
        assert run.state.current == target and run.state.pending is None
        assert run.row["next_fp"] is not None

    async def call(path):
        events.append("directory-read")
        assert path == ""
        return run.row

    monkeypatch.setattr(run, "held_orgs", held)
    monkeypatch.setattr(run, "durable", durable)
    monkeypatch.setattr(run, "publish", publish)
    monkeypatch.setattr(run, "call", call)
    monkeypatch.setattr(cell_update_pg, "write", write)
    monkeypatch.setattr(cell_update_participants, "settle", settle)
    return run, events


@pytest.mark.asyncio
async def test_config_reserves_directory_before_any_local_acceptance(coordinator):
    run, events = coordinator
    run.args.config, run.target = Path("target"), value("new", orgs=("held",))
    await run.configuration()
    assert events == ["directory-config", "placement-proof", "local-write", "durable"]
    assert run.state.pending == run.target


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["finish", "revert"])
@pytest.mark.parametrize("already_narrowed", [False, True])
async def test_finalization_repairs_then_cuts_off_then_turns_over_then_releases(
    coordinator, action, already_narrowed
):
    run, events = coordinator
    setattr(run.args, action, True)
    target = run.row["next_fp"] if action == "finish" else run.row["current_fp"]
    if already_narrowed:
        run.state = replace(run.state, current=target, pending=None)
    await run.configuration()
    assert events == [
        "placement-proof",
        "local-write",
        "durable",
        "turnover",
        "durable",
        "directory-read",
        "directory-" + action,
    ]
    assert run.state.current == target and run.state.pending is None
    assert run.row["current_fp"] == target and run.row["next_fp"] is None


@pytest.mark.asyncio
async def test_cutoff_timeout_keeps_directory_dual_without_compensating_local_rewiden(
    coordinator, monkeypatch
):
    run, events = coordinator
    run.args.finish = True
    monkeypatch.setattr(
        cell_update_participants, "settle", AsyncMock(side_effect=TimeoutError("busy"))
    )
    before = run.row.copy()
    with pytest.raises(TimeoutError, match="busy"):
        await run.configuration()
    assert run.row == before
    assert run.state.current == before["next_fp"] and run.state.pending is None
    assert events == ["placement-proof", "local-write", "durable"]


@pytest.mark.asyncio
async def test_prebegin_never_reads_or_places_configured_orgs(coordinator, monkeypatch):
    run, events = coordinator
    run.row = row()
    run.args.config, run.target = Path("target"), value("new", orgs=("unplaced",))
    forbidden = AsyncMock(side_effect=AssertionError("directory reservation before begin"))
    monkeypatch.setattr(run, "held_orgs", forbidden)
    monkeypatch.setattr(run, "publish", forbidden)
    await run.configuration()
    assert events == ["local-write", "durable"]
    assert run.state.pending == run.target


@pytest.mark.asyncio
async def test_lost_generation_after_barrier_never_releases_reservation(coordinator, monkeypatch):
    run, events = coordinator
    run.args.finish = True
    monkeypatch.setattr(
        run, "call", AsyncMock(return_value=run.row | {"mutation_generation": str(UUID(int=2))})
    )
    with pytest.raises(cell_control.Refused, match="ownership or reservation changed"):
        await run.configuration()
    assert "directory-finish" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["config", "finish", "revert"])
async def test_singleton_noop_proves_local_durability_without_turnover(coordinator, action):
    run, events = coordinator
    run.row["next_fp"] = None
    run.state = replace(run.state, current=run.row["current_fp"], pending=None)
    if action == "config":
        run.args.config, run.target = Path("target"), run.row["current_fp"]
    else:
        setattr(run.args, action, True)
    await run.configuration()
    assert events.count("durable") == 1
    assert "turnover" not in events
    assert "directory-" + action in events


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["config", "finish"])
async def test_scim_on_refuses_before_generation_claim_or_acceptance(
    coordinator, monkeypatch, action
):
    run, _ = coordinator
    unsafe = value("new", scim=True)
    run.row["next_fp"] = unsafe
    if action == "config":
        run.args.config = Path("target")
        monkeypatch.setattr(cell_update_run.cell_update_config, "read_target", lambda *_: object())
        monkeypatch.setattr(
            cell_update_run.cell_update_config, "target_fingerprint", lambda *_: unsafe
        )
    else:
        run.args.finish = True
    claim = AsyncMock(side_effect=AssertionError("must not claim"))
    monkeypatch.setattr(cell_control, "claim", claim)
    with pytest.raises(cell_control.Refused, match="census client differs"):
        await run.start(run.owner)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_revert_repairs_tenancy_for_pending_placements_before_dropping_fingerprint(
    coordinator, monkeypatch
):
    run, _ = coordinator
    run.args.revert = True
    held = AsyncMock(return_value=["held"])
    monkeypatch.setattr(run, "held_orgs", held)
    before = run.row.copy()
    await run.configuration()
    held.assert_awaited_once_with(before["current_fp"], before["next_fp"])
