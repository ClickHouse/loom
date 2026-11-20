"""Update checks provisioning against the prepared cell's actual private stores."""

from __future__ import annotations

import sys
from argparse import Namespace

import httpx
import pytest

from server.auth import scim
from server.cli import cell_control, cell_update_pg
from server.cli.cell_update_run import Run
from server.core import cell, pg
from server.router import directory
from server.settings import settings
from tests.integration.test_cell_attach import ROUTER, _Network, _prepare
from tests.integration.test_cell_scim_census_live_ch import (
    cell_clickhouse_databases as cell_clickhouse_databases,
)
from tests.integration.test_cell_scim_census_live_ch import cell_stores as cell_stores
from tests.integration.test_cell_scim_census_live_ch import (
    genuine_cell_processes as genuine_cell_processes,
)
from tests.integration.test_cell_scim_census_live_ch import people as people
from tests.integration.test_scim_person_live_ch import People
from tests.unit.cell_control_owner import owner

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture(autouse=True)
async def _clean_people(people: People):
    try:
        yield people
    finally:
        first_error = sys.exception()
        for sql, params in (
            (
                "ALTER TABLE loom.namespaces DELETE WHERE org = {org:String} "
                "SETTINGS mutations_sync = 2",
                {"org": people.org},
            ),
            (
                "ALTER TABLE loom.scim_users DELETE WHERE id IN {ids:Array(String)} "
                "SETTINGS mutations_sync = 2",
                {"ids": [scim.user_id(email) for email in people.emails]},
            ),
        ):
            try:
                await people.client.execute(sql, params=params)
            except BaseException as error:  # noqa: BLE001 — attempt every owned cleanup.
                if first_error is None:
                    first_error = error
                else:
                    first_error.add_note(f"identity cleanup also failed: {type(error).__name__}")
        if first_error is not None:
            raise first_error


@pytest.mark.parametrize("case", ["provisioned", "missing-person", "changed-local-pair"])
async def test_update_scim_precondition_uses_actual_acceptance_and_people(
    cell_stores, people: People, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setattr(settings, "scim_token", "")
    monkeypatch.setattr(settings, "oauth_login_provider", "")
    monkeypatch.setattr(settings, "auth_required", False)
    async with _Network() as network:
        await _prepare(cell_stores, network)
        email = people.email("update-owner")
        await people.account(email, email)
        if case != "missing-person":
            await people.provision(email)
        monkeypatch.setattr(settings, "scim_token", "lsc_first_" + "a" * 64)
        monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
        monkeypatch.setattr(settings, "auth_required", True)
        args = Namespace(cell="first", revoke_unprovisioned=False)
        run = Run(people.client, args, httpx.URL(ROUTER), network)
        async with pg.dedicated() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
            run.owner = owner(conn)
            run.row = await run.call("")
            run.state = await cell_update_pg.read(conn, "first")
            assert run.state.current["fields"]["scim"] is False
            target = cell.as_value({**run.state.current["fields"], "scim": True})
            if case == "changed-local-pair":
                await conn.execute("UPDATE loom.cell_self SET next_fp=$1::jsonb", target)
            pool = await directory.pool()
            async with pool.acquire() as directory_conn:
                before = (
                    await cell_update_pg.read(conn, "first"),
                    await conn.fetchrow("SELECT * FROM loom.cell_self"),
                    await conn.fetch("SELECT * FROM loom.tenancy ORDER BY org"),
                    await directory_conn.fetch("SELECT * FROM loom_router.cells ORDER BY cell"),
                    await directory_conn.fetch("SELECT * FROM loom_router.placements ORDER BY org"),
                    await directory_conn.fetch("SELECT * FROM loom_router.audit ORDER BY id"),
                )
                if case == "provisioned":
                    await run.scim_precondition(target)
                else:
                    reason = (
                        "unprovisioned accounts subject"
                        if case == "missing-person"
                        else "accepted local fingerprint pair changed"
                    )
                    with pytest.raises(cell_control.Refused, match=reason):
                        await run.scim_precondition(target)
                after = (
                    await cell_update_pg.read(conn, "first"),
                    await conn.fetchrow("SELECT * FROM loom.cell_self"),
                    await conn.fetch("SELECT * FROM loom.tenancy ORDER BY org"),
                    await directory_conn.fetch("SELECT * FROM loom_router.cells ORDER BY cell"),
                    await directory_conn.fetch("SELECT * FROM loom_router.placements ORDER BY org"),
                    await directory_conn.fetch("SELECT * FROM loom_router.audit ORDER BY id"),
                )
                assert after == before
