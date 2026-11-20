"""A serving shortcut validates the registration without claiming or writing."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.cli import cell_attach, cell_control
from server.core import pg
from server.core.cell_ch import Refused
from server.settings import settings
from tests.unit.test_cell_attach import FakeConn, _args


@pytest.fixture
def serving_run(monkeypatch):
    def make(
        *,
        deployment="registered",
        configured_cell="first",
        bucket="bucket",
        prefix="docs/",
        first=True,
        mode="enforced",
    ):
        row = {
            "cell": "first",
            "is_first": first,
            "state": "serving",
            "host": "first.example",
            "deployment_id": "registered",
            "bucket": "bucket",
            "bucket_prefix": "docs/",
            "probe_url": "",
            "upstream_url": "",
            "current_fp": None,
            "next_fp": None,
            "mutation_generation": str(UUID(int=1)),
        }
        monkeypatch.setattr(settings, "cell", configured_cell)
        monkeypatch.setattr(settings, "document_s3_bucket", bucket)
        monkeypatch.setattr(settings, "document_s3_prefix", prefix)
        conn = FakeConn(
            [
                ("SELECT deployment_id FROM loom.authority", deployment),
                (
                    "SELECT (SELECT is_first FROM loom.cell_first)",
                    {
                        "is_first": first,
                        "cell": "first",
                        "mode": mode,
                        "has_own": True,
                        "has_tenancy": True,
                    },
                ),
                (
                    "SELECT is_first, cell FROM loom.cell_first",
                    {"is_first": first, "cell": "first"},
                ),
            ]
        )
        sessions = []

        @asynccontextmanager
        async def dedicated(**kwargs):
            sessions.append("opened")
            try:
                yield conn
            finally:
                sessions.append("closed")

        monkeypatch.setattr(pg, "dedicated", dedicated)
        owner = AsyncMock(side_effect=AssertionError("serving must not start an owner"))
        claim = AsyncMock(side_effect=AssertionError("serving must not claim"))
        mutate = AsyncMock(side_effect=AssertionError("serving must not mutate"))
        monkeypatch.setattr(cell_control, "run", owner)
        monkeypatch.setattr(cell_control, "claim", claim)
        monkeypatch.setattr(cell_control, "mutate", mutate)
        router = SimpleNamespace(call=AsyncMock(return_value=row))
        run = cell_attach.Run(None, _args(), router, lambda text: None)
        return run, conn, router, sessions, (owner, claim, mutate)

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"deployment": "another-deployment"}, id="deployment"),
        pytest.param({"deployment": None}, id="missing-authority"),
        pytest.param({"configured_cell": "other"}, id="configured-cell"),
        pytest.param({"bucket": "another-bucket"}, id="bucket"),
        pytest.param({"prefix": "another-prefix/"}, id="bucket-prefix"),
    ],
)
async def test_serving_run_refuses_another_registration_without_writes(serving_run, overrides):
    run, conn, router, sessions, mutations = serving_run(**overrides)
    with pytest.raises(Refused, match="do not match the directory registration"):
        await run.run()
    assert "done" not in run.report
    assert sessions == ["opened", "closed"]
    assert router.call.await_args_list[0].args == ("",)
    assert router.call.await_count == 1
    assert conn.executed == []
    assert all(call.await_count == 0 for call in mutations)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [True, False], ids=["first", "later"])
async def test_matching_serving_registration_is_done_without_writes(serving_run, first):
    run, conn, router, sessions, mutations = serving_run(first=first)
    assert await run.run() == 0
    assert run.report == {"cell": "first", "state": "serving", "done": True}
    assert sessions == ["opened", "closed"]
    assert router.call.await_count == 1
    assert conn.executed == []
    assert all(call.await_count == 0 for call in mutations)


@pytest.mark.asyncio
async def test_matching_serving_registration_still_refuses_recording(serving_run):
    run, conn, router, sessions, mutations = serving_run(mode="recording")
    with pytest.raises(Refused, match="attach lowers and repairs neither"):
        await run.run()
    assert "done" not in run.report
    assert sessions == ["opened", "closed"]
    assert router.call.await_count == 1
    assert conn.executed == []
    assert all(call.await_count == 0 for call in mutations)
