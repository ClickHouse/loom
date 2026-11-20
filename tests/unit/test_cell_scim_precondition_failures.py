"""A cutoff that landed remains reported when its confirmation fails."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from server.auth import revocation
from server.auth.revocation import scim_revoke as actual_scim_revoke
from server.core.cell_ch import Refused
from tests.unit.test_cell_attach_serving_identity import serving_run as serving_run
from tests.unit.test_cell_scim_precondition import provisioned_run as provisioned_run


@pytest.mark.parametrize("confirmation", ["unavailable", "missing", "weaker"])
async def test_cutoff_insert_is_reported_before_failed_confirmation(
    provisioned_run, monkeypatch, confirmation
):
    run, people, _native = provisioned_run
    run.args.revoke_unprovisioned = True
    people.accounts = [("o", "person@example.test", "person@example.test", True)]
    read = people.query_all

    async def insert(table, rows):
        assert table == "loom.revoked_tokens"
        for row in rows:
            people.revocations[row["jti"]] = row
            people.writes.append(row)

    async def query_all(sql, params=None):
        if "loom.revoked_tokens" in sql and people.writes:
            if confirmation == "unavailable":
                raise ConnectionError("accepted insert; confirmation unavailable")
            if confirmation == "missing":
                return []
            return [{**people.writes[0], "issued_before": datetime.now(UTC) - timedelta(days=1)}]
        return await read(sql, params)

    monkeypatch.setattr(people, "insert", insert, raising=False)
    monkeypatch.setattr(people, "query_all", query_all)
    monkeypatch.setattr(revocation, "scim_revoke", actual_scim_revoke)
    monkeypatch.setattr(revocation, "_next_version", AsyncMock(return_value=7))
    remember = Mock()
    monkeypatch.setattr(revocation, "remember_row", remember)
    refresh = AsyncMock(side_effect=ConnectionError("full snapshot unavailable"))
    monkeypatch.setattr(revocation, "refresh", refresh)
    messages = []
    run.log = messages.append

    with pytest.raises(Refused):
        await run.check_beats("refuse")

    assert len(people.writes) == 1
    assert people.writes[0]["jti"] == revocation.SCIM_PREFIX + "person@example.test"
    assert people.writes[0]["version"] == 7
    assert people.writes[0]["issued_before"] is not None
    assert run.report["scim"]["revoked"] == 1
    assert run.report["scim"]["complete"] is False
    assert len(messages) == 1
    assert "SCIM cutoff accepted" in messages[0]
    assert "remains if a later check fails" in messages[0]
    assert remember.call_count == 1
    assert refresh.await_count == 0
    assert people.users == {}


@pytest.mark.parametrize("invalid", ["missing-current", "empty-current", "empty-pending"])
async def test_serving_rerun_refuses_an_unreadable_actual_local_pair(serving_run, invalid):
    from tests.unit.test_cell_attach import CURRENT

    run, conn, _router, sessions, mutations = serving_run()
    conn.routes.insert(
        0,
        (
            "SELECT current_fp, next_fp FROM loom.cell_self",
            {
                "current_fp": None
                if invalid == "missing-current"
                else {}
                if invalid == "empty-current"
                else CURRENT,
                "next_fp": {} if invalid == "empty-pending" else None,
            },
        ),
    )
    with pytest.raises(Refused, match="the accepted local pair is unreadable"):
        await run.run()
    assert "done" not in run.report
    assert sessions == ["opened", "closed"]
    assert conn.executed == []
    assert all(call.await_count == 0 for call in mutations)
