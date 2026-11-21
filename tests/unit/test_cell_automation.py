"""Only a proved public bot pair may reach fenced directory provisioning."""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from starlette.testclient import TestClient

from server.cli import cell_bot_proof, cell_control
from server.router import app as router_app
from server.router import directory
from server.settings import settings
from tests.unit.test_router_attach import OPERATOR, _cell_row, _Conn, _Store


def pair() -> list[dict[str, str]]:
    return [
        {"purpose": purpose, "name": purpose, "thumbprint": thumbprint * 43}
        for purpose, thumbprint in (("automation", "A"), ("recovery", "B"))
    ]


def test_cell_cli_exposes_explicit_distinct_bot_key_inputs():
    from server.cli.cell import _add_cell_parser

    parser = argparse.ArgumentParser()
    _add_cell_parser(parser.add_subparsers())
    commands = (
        parser._subparsers._group_actions[0].choices["cell"]._subparsers._group_actions[0].choices
    )
    assert "automation" in commands
    args = parser.parse_args(
        [
            "cell",
            "automation",
            "--cell",
            "eu-1",
            "--automation-bot",
            "automation",
            "--automation-thumbprint",
            "A" * 43,
            "--automation-key",
            "/automation",
            "--recovery-bot",
            "recovery",
            "--recovery-thumbprint",
            "B" * 43,
            "--recovery-key",
            "/recovery",
        ]
    )
    assert (args.automation_bot, args.recovery_bot) == ("automation", "recovery")
    assert (args.automation_key, args.recovery_key) == (Path("/automation"), Path("/recovery"))


@pytest.fixture
def store(monkeypatch):
    import hashlib

    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    state = _Store(_cell_row(state="serving", mode="enforced", break_glass=[]))

    class Conn(_Conn):
        async def execute(self, sql: str, *args: Any) -> str:
            if "SET break_glass = $2" in sql:
                self._count()
                self.s.cells[args[0]]["break_glass"] = copy.deepcopy(args[1])
                return "UPDATE 1"
            return await super().execute(sql, *args)

    @contextlib.asynccontextmanager
    async def connection(**_kwargs):
        yield Conn(state)

    monkeypatch.setattr(directory, "_connection", connection)
    return state


def post(client, state, entries, *, generation=None, token=OPERATOR):
    row = state.cells["eu-1"]
    expected = {name: row[name] for name in directory.CONTROL_FIELDS}
    expected["mutation_generation"] = str(generation or row["mutation_generation"])
    return client.post(
        "/cells/v1/operator/cells/eu-1/administration",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        content=json.dumps({"expected": expected, "bots": entries}, ensure_ascii=True),
    )


def test_metadata_write_records_only_public_pair_and_audits(store):
    before = copy.deepcopy(store.cells["eu-1"])
    client = TestClient(router_app.build_app(router_app.Router()))
    answer = post(client, store, pair())
    assert answer.status_code == 200
    assert answer.json() == {
        "version": 1,
        "cell": "eu-1",
        "deployment_id": "dep-1",
        **{
            entry["purpose"]: {key: entry[key] for key in ("name", "thumbprint")}
            for entry in pair()
        },
    }
    assert store.cells["eu-1"] == {**before, "break_glass": pair()}
    assert store.audit == [("operator", "administration", {"cell": "eu-1", "bots": pair()})]


@pytest.mark.parametrize(
    "fault", ["credential", "generation", "same-key", "secret", "missing", "surrogate"]
)
def test_invalid_provisioning_never_changes_metadata_or_audit(store, fault):
    entries = pair()
    kwargs = {}
    if fault == "credential":
        kwargs["token"] = "wrong"
    elif fault == "generation":
        kwargs["generation"] = UUID(int=99)
    elif fault == "same-key":
        entries[1]["thumbprint"] = entries[0]["thumbprint"]
    elif fault == "secret":
        entries[0]["key_file"] = "/private/key"
    elif fault == "surrogate":
        entries[0]["name"] = "\ud800"
    else:
        entries.pop()
    before = copy.deepcopy(store.cells)
    client = TestClient(router_app.build_app(router_app.Router()))
    answer = post(client, store, entries, **kwargs)
    assert answer.status_code in {401, 400, 409}
    assert store.cells == before and store.audit == []


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_fails", [False, True])
async def test_live_pair_proof_precedes_the_only_metadata_mutation(monkeypatch, proof_fails):
    from server.cli import cell_automation

    row = _cell_row(state="serving", mode="enforced")
    row["mutation_generation"] = str(row["mutation_generation"])
    args = argparse.Namespace(
        cell="eu-1",
        automation_bot="automation",
        automation_thumbprint="A" * 43,
        automation_key=Path("/automation"),
        recovery_bot="recovery",
        recovery_thumbprint="B" * 43,
        recovery_key=Path("/recovery"),
        ca_file=None,
    )
    events = []

    async def request(_url, _name, _transport, path, body=None, **kwargs):
        events.append(path or "read")
        if path == "/administration":
            assert body["bots"] == pair()
            assert body["expected"]["mutation_generation"] == str(UUID(int=2))
            return {
                "version": 1,
                "cell": "eu-1",
                "deployment_id": "dep-1",
                **{
                    entry["purpose"]: {key: entry[key] for key in ("name", "thumbprint")}
                    for entry in pair()
                },
            }
        if path == "/claim":
            return {**row, "mutation_generation": str(UUID(int=2))}
        return row

    async def prove(**kwargs):
        events.append("proof")
        assert kwargs["origin"] == "https://loom.example"
        assert isinstance(kwargs["automation"], cell_bot_proof.BotKey)
        if proof_fails:
            raise ValueError("proof refused")
        return pair()

    class Owner:
        conn = object()

        async def call(self, work, *, total_s):
            return await work()

    monkeypatch.setattr(cell_control, "request", request)
    monkeypatch.setattr(cell_control, "paired", AsyncMock())
    monkeypatch.setattr(cell_control, "check_lock", AsyncMock())
    monkeypatch.setattr(cell_bot_proof, "prove_pair", prove)
    work = cell_automation.provision(Owner(), httpx.URL("https://router.example"), args)
    if proof_fails:
        with pytest.raises(ValueError, match="proof refused"):
            await work
        assert events == ["read", "/claim", "proof"]
    else:
        answer = await work
        assert answer["automation"]["name"] == "automation"
        assert events == ["read", "/claim", "proof", "/administration"]


@pytest.mark.asyncio
async def test_configured_authority_mismatch_prevents_claim_and_key_proof(monkeypatch):
    from server.cli import cell_automation

    row = _cell_row(state="serving", mode="enforced")
    row["mutation_generation"] = str(row["mutation_generation"])
    request = AsyncMock(return_value=row)
    proof = AsyncMock(side_effect=AssertionError("proof before authority pairing"))
    monkeypatch.setattr(cell_control, "request", request)
    monkeypatch.setattr(cell_control, "check_lock", AsyncMock())
    monkeypatch.setattr(cell_bot_proof, "prove_pair", proof)

    class Owner:
        class Connection:
            async def fetchval(self, sql):
                assert sql == "SELECT deployment_id FROM loom.authority"
                return "different-deployment"

        conn = Connection()

    with pytest.raises(cell_control.Refused, match="do not match"):
        await cell_automation.provision(
            Owner(), httpx.URL("https://router.example"), argparse.Namespace(cell="eu-1")
        )
    assert request.await_count == 1
    assert request.call_args.kwargs == {"method": "GET"}
    proof.assert_not_awaited()
