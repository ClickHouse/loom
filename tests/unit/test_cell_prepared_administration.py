"""A complete prepared later cell may prove its public bot pair before attach."""

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

from server.cli import cell_automation, cell_bot_proof, cell_control
from server.router import app as router_app
from server.router import cell_admin, directory
from tests.unit.test_cell_automation import pair, post
from tests.unit.test_router_attach import CURRENT, OPERATOR, _cell_row, _Conn, _Store


def prepared(**changes: Any) -> dict[str, Any]:
    return _cell_row(
        is_first=False, mode="enforced", current_fp=copy.deepcopy(CURRENT), placed_orgs=0, **changes
    )


@pytest.fixture
def prepared_store(monkeypatch):
    import hashlib

    from server.settings import settings

    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    state = _Store(prepared(), _cell_row(cell="first", state="serving", mode="enforced"))

    class Conn(_Conn):
        async def fetchrow(self, sql, *args):
            if "WHERE is_first" in sql:
                self._count()
                return next((row for row in self.s.cells.values() if row["is_first"] is True), None)
            return await super().fetchrow(sql, *args)

        async def execute(self, sql, *args):
            if "SET break_glass = $2" in sql:
                self._count()
                self.s.cells[args[0]]["break_glass"] = copy.deepcopy(args[1])
                return "UPDATE 1"
            return await super().execute(sql, *args)

    @contextlib.asynccontextmanager
    async def connection(**_options):
        yield Conn(state)

    monkeypatch.setattr(directory, "_connection", connection)
    return state


def test_prepared_later_pair_uses_existing_fenced_metadata_write(prepared_store):
    before = copy.deepcopy(prepared_store.cells)
    client = TestClient(router_app.build_app(router_app.Router()))
    answer = post(client, prepared_store, pair())
    assert answer.status_code == 200
    assert answer.json()["automation"]["name"] == "automation"
    assert prepared_store.cells == {**before, "eu-1": {**before["eu-1"], "break_glass": pair()}}
    assert prepared_store.audit == [
        ("operator", "administration", {"cell": "eu-1", "bots": pair()})
    ]
    assert prepared_store.statements <= 10


@pytest.mark.parametrize(
    "fault",
    [
        "first",
        "begun",
        "upstream",
        "probe",
        "fingerprint",
        "recording",
        "no-first",
        "first-attaching",
        "generation",
    ],
)
def test_prepared_wrong_target_never_writes(prepared_store, fault):
    row = prepared_store.cells["eu-1"]
    if fault == "first":
        row["is_first"] = True
    elif fault == "begun":
        row["attach_started_at"] = "begun"
    elif fault == "upstream":
        row["upstream_url"] = ""
    elif fault == "probe":
        row["probe_url"] = row["upstream_url"]
    elif fault == "fingerprint":
        row["current_fp"]["fields"]["deployment_id"] = "foreign"
    elif fault == "recording":
        row["mode"] = "recording"
    elif fault == "no-first":
        prepared_store.cells.pop("first")
    elif fault == "first-attaching":
        prepared_store.cells["first"]["state"] = "attaching"
    before = copy.deepcopy(prepared_store.cells)
    client = TestClient(router_app.build_app(router_app.Router()))
    answer = post(
        client,
        prepared_store,
        pair(),
        **({"generation": UUID(int=99)} if fault == "generation" else {}),
    )
    assert answer.status_code in {400, 409}
    assert prepared_store.cells == before and prepared_store.audit == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["prepared", "serving", "wrong-local", "proof-fails"])
async def test_native_prepared_pair_preserves_local_claim_and_both_key_proofs(monkeypatch, kind):
    row = prepared()
    row["mutation_generation"] = str(row["mutation_generation"])
    if kind == "serving":
        row["state"] = "serving"
    events = []

    class Conn:
        async def fetchrow(self, sql):
            if "cell_first" in sql:
                return {"is_first": False, "cell": "foreign" if kind == "wrong-local" else "eu-1"}
            assert "cell_self" in sql
            return {"current_fp": row["current_fp"], "next_fp": row["next_fp"]}

        async def fetchval(self, sql):
            assert "tenancy_mode" in sql
            return "enforced"

    class Owner:
        conn = Conn()

        async def call(self, work, *, total_s):
            return await work()

    async def request(_url, _name, _transport, path, body=None, **_kwargs):
        events.append(path or "read")
        if path == "/claim":
            return {**row, "mutation_generation": str(UUID(int=2))}
        if path == "/administration":
            assert body["expected"]["mutation_generation"] == str(UUID(int=2))
            assert body["bots"] == pair()
            return {
                "version": 1,
                "cell": "eu-1",
                "deployment_id": "dep-1",
                **{
                    entry["purpose"]: {key: entry[key] for key in ("name", "thumbprint")}
                    for entry in pair()
                },
            }
        return row

    async def prove(**kwargs):
        events.append("proof")
        assert kwargs["origin"] == "https://loom.example"
        assert kwargs["deployment_id"] == "dep-1"
        assert kwargs["automation"].name == "automation" and kwargs["recovery"].name == "recovery"
        if kind == "proof-fails":
            raise ValueError("proof refused")
        return pair()

    monkeypatch.setattr(cell_control, "request", request)
    monkeypatch.setattr(cell_control, "paired", AsyncMock())
    monkeypatch.setattr(cell_control, "check_lock", AsyncMock())
    monkeypatch.setattr(cell_bot_proof, "prove_pair", prove)
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
    work = cell_automation.provision(Owner(), httpx.URL("https://router.example"), args)
    if kind == "wrong-local":
        with pytest.raises(cell_control.Refused):
            await work
        assert events == ["read"]
    elif kind == "proof-fails":
        with pytest.raises(ValueError, match="proof refused"):
            await work
        assert events == ["read", "/claim", "proof"]
    else:
        assert (await work)["automation"]["name"] == "automation"
        assert events == ["read", "/claim", "proof", "/administration"]


def test_prepared_target_is_explicit_and_does_not_open_each_census():
    from server.cli import cell_each_http

    row = prepared()
    row.update(
        administration=json.dumps(pair()),
        current_digest="a" * 64,
        next_digest=None,
    )
    with pytest.raises(cell_admin.InvalidAdministration):
        cell_admin.census([row])
    target = cell_admin.target(row, prepared=True)
    assert target["state"] == "attaching" and target["mode"] == "enforced"
    assert target["origin"] == "https://loom.example"
    with pytest.raises(ValueError):
        cell_each_http._target(target)
