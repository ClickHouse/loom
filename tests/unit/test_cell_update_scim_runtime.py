"""Update checks the selected SCIM policy before claiming and releasing a reservation."""

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from server.auth import scim
from server.cli import cell_control, cell_update_participants, cell_update_probe, cell_update_run
from server.cli.cell_ch import Refused
from server.cli.cell_update_pg import State
from server.core import cell, pg_profile
from server.settings import settings
from tests.unit.cell_control_owner import owner
from tests.unit.test_cell_scim_precondition import provisioned_run as provisioned_run

EMAIL = "person@example.test"


@pytest.fixture
def update_run(provisioned_run, monkeypatch, tmp_path):
    attached, people, native = provisioned_run
    native["mode"] = "enforced"
    monkeypatch.setattr(cell, "mode", lambda: "enforced")
    monkeypatch.setattr(cell, "deployment_id", lambda: "dep-1")
    for name, value in {
        "public_base_url": "https://first.example.test",
        "oauth_issuer": "https://first.example.test",
        "document_s3_prefix": "originals",
        "code_repos_json": "[]",
        "code_org": "",
        "tracker_org": "",
        "oidc_jit_org": "",
        "self_signup": "off",
        "oidc_client_id": "",
        "grant_cache_ttl_seconds": 240,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(pg_profile, "standby_now", AsyncMock(return_value="synchronous"))
    fields = cell.fingerprint_for(settings, "dep-1", set())
    current = cell.as_value({**fields, "scim": False})
    target = cell.as_value({**fields, "oidc_client_id": "pending"})
    config = tmp_path / "target.yaml"
    config.write_text("version: 1\nsettings:\n  oidc_client_id: pending\n")
    args = Namespace(
        cell="first",
        config=None,
        finish=False,
        revert=False,
        probe_url=None,
        wait=0,
        revoke_unprovisioned=False,
    )
    run = cell_update_run.Run(people, args, httpx.URL("http://router"), None)
    run.owner = owner(attached.lock_conn)
    run.target = None
    run.row = {
        **attached.row,
        "host": "first.example.test",
        "probe_url": "http://probe",
        "upstream_url": "http://main",
        "mutation_generation": str(UUID(int=1)),
        "state": "serving",
    }
    events = []
    control = SimpleNamespace(config=config, reads=[], after_turnover=lambda: None)

    def pair(active, pending):
        attached.own_fp = (active, pending)
        run.row.update(current_fp=active, next_fp=pending)
        run.state = State("dep-1", True, "first", "enforced", active, pending)

    control.pair = pair
    pair(current, target)
    answer = run.owner.conn._answer

    def read(sql, params):
        control.reads.append(sql)
        return answer(sql, params)

    execute = run.owner.conn.execute

    async def write(sql, *params):
        result = await execute(sql, *params)
        if sql.startswith("UPDATE loom.cell_self SET current_fp="):
            events.append("local-write")
            attached.own_fp = params
            return "UPDATE 1"
        return result

    async def call(path, body=None):
        events.append(path or "directory-read")
        if path == "":
            return dict(run.row)
        if path == "/claim":
            assert body == cell_control.preconditions(run.row, claim=True)
            return {**run.row, "mutation_generation": str(UUID(int=2))}
        assert path == "/configuration/placements"
        assert body["orgs"] == []
        return {"orgs": []}

    async def post(selected, path, body):
        assert selected is run and path == "/configuration"
        action = body["action"]
        events.append("publish-" + action)
        active, pending = cell_update_run.fingerprints.configuration_pair(
            run.row["current_fp"], run.row["next_fp"], body["target_fp"], action
        )
        return {**run.row, "current_fp": active, "next_fp": pending}

    async def settle(selected, target):
        assert selected is run
        assert (run.state.current, run.state.pending) == (target, None)
        events.append("turnover")
        control.after_turnover()

    monkeypatch.setattr(run.owner.conn, "_answer", read)
    monkeypatch.setattr(run.owner.conn, "execute", write)
    monkeypatch.setattr(run, "call", call)
    monkeypatch.setattr(cell_update_probe, "post", post)
    monkeypatch.setattr(cell_update_participants, "settle", settle)
    return run, people, events, control


async def test_config_provisioned_target_passes_actual_census_on_both_local_pairs(update_run):
    run, people, events, control = update_run
    current, target = run.state.current, run.state.pending
    control.pair(current, None)
    run.args.config = control.config
    people.accounts = [("o", EMAIL, EMAIL, True)]
    people.provision(EMAIL)

    result = await run.start(run.owner)

    assert result["action"] == "config" and result["next"] == cell.digest(target["fields"])
    assert run.target == target
    assert (run.state.current, run.state.pending) == (current, target)
    assert (run.row["current_fp"], run.row["next_fp"]) == (current, target)
    assert len(people.census_reads) == 6
    assert events.index("/claim") < events.index("publish-config") < events.index("local-write")
    assert "turnover" not in events and people.writes == []


@pytest.mark.parametrize("action", ["config", "finish"])
async def test_unverified_contact_refuses_before_generation_claim_or_acceptance(update_run, action):
    run, people, events, control = update_run
    if action == "config":
        control.pair(run.state.current, None)
        run.args.config = control.config
    else:
        run.args.finish = True
    people.accounts = [("o", "handle", EMAIL, False)]
    people.provision(EMAIL)
    before = (run.state.current, run.state.pending, run.row["mutation_generation"])

    with pytest.raises(Refused, match="unprovisioned accounts subject 'handle'"):
        await run.start(run.owner)

    assert (run.state.current, run.state.pending, run.row["mutation_generation"]) == before
    assert events == ["directory-read"]
    assert len(people.census_reads) == 1
    assert not any(
        sql.startswith("UPDATE ") or sql.startswith("INSERT ") for sql in run.owner.conn.executed
    )
    assert people.writes == []


async def test_finish_provisioned_target_rechecks_and_releases_after_turnover(update_run):
    run, people, events, _control = update_run
    run.args.finish = True
    target = run.state.pending
    people.accounts = [("o", EMAIL, EMAIL, True)]
    people.provision(EMAIL)

    result = await run.start(run.owner)

    assert result["action"] == "finish" and result["next"] is None
    assert (run.state.current, run.state.pending) == (target, None)
    assert (run.row["current_fp"], run.row["next_fp"]) == (target, None)
    assert len(people.census_reads) == 9
    assert events.index("local-write") < events.index("turnover") < events.index("publish-finish")
    assert people.writes == []


async def test_finish_fresh_census_refuses_remote_deprovisioning_before_release(update_run):
    run, people, events, control = update_run
    run.args.finish = True
    current, target = run.state.current, run.state.pending
    people.accounts = [("o", EMAIL, EMAIL, True)]
    people.provision(EMAIL)
    assert not await scim.refuses_subject(EMAIL, org="o")
    control.after_turnover = people.users.clear

    with pytest.raises(Refused, match="unprovisioned accounts subject"):
        await run.start(run.owner)

    assert not await scim.refuses_subject(EMAIL, org="o"), "the ordinary permit is still warm"
    assert (run.state.current, run.state.pending) == (target, None)
    assert (run.row["current_fp"], run.row["next_fp"]) == (current, target)
    assert len(people.census_reads) == 7
    assert "turnover" in events and "publish-finish" not in events
    assert people.writes == []


async def test_revert_selects_scim_off_current_without_reading_provisioning(update_run):
    run, people, events, _control = update_run
    run.args.revert = True
    current = run.state.current
    people.unavailable = True

    result = await run.start(run.owner)

    assert result["action"] == "revert" and result["next"] is None
    assert (run.state.current, run.state.pending) == (current, None)
    assert (run.row["current_fp"], run.row["next_fp"]) == (current, None)
    assert events.index("turnover") < events.index("publish-revert")
    assert people.census_reads == people.reads == people.writes == []


async def test_scim_off_precondition_needs_no_store_io(update_run):
    run, people, events, control = update_run
    people.unavailable = True

    await run.scim_precondition(run.state.current)

    assert control.reads == run.owner.conn.executed == events == []
    assert people.census_reads == people.reads == people.writes == []


async def test_foreign_target_refuses_before_identity_reads_or_claim(update_run):
    run, people, events, _control = update_run
    target = cell.as_value({**run.state.pending["fields"], "deployment_id": "foreign"})

    with pytest.raises(Refused, match="intended configuration names another deployment"):
        await run.scim_precondition(target)

    assert events == []
    assert people.census_reads == []
    assert people.writes == []
