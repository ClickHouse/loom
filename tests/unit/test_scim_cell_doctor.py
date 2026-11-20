"""Doctor grades the actual bounded identity read, including unread failures."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from server.cli import doctor, doctor_cell
from server.core import cell, cell_triggers, pg
from server.settings import settings

OWN = "lsc_first_" + "a" * 64
LEGACY = "legacy-token-" + "x" * 40


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "scim_token", OWN)
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(doctor_cell, "_deployment_check", AsyncMock(return_value=""))
    monkeypatch.setattr(doctor_cell, "_unmarked_session", AsyncMock(return_value=False))
    # The global view is deliberately unrelated; doctor must use its own bounded read.
    monkeypatch.setattr(cell, "_view", cell._View(first=(True, "another")))


@pytest.mark.parametrize(
    "first,raw,grade",
    [
        ((True, "first"), LEGACY, "PASS"),
        ((False, "first"), OWN, "PASS"),
        ((False, "first"), LEGACY, "FAIL"),
        ((True, "another"), OWN, "FAIL"),
        (None, OWN, "FAIL"),
        (None, "", "PASS"),
        ((True, "first"), ",".join([OWN] * 3), "FAIL"),
    ],
)
async def test_doctor_uses_full_first_tuple_from_its_existing_single_read(
    configured, monkeypatch, first, raw, grade
):
    monkeypatch.setattr(settings, "scim_token", raw)
    reads = []

    class Conn:
        async def fetchrow(self, sql):
            reads.append(sql)
            return {
                "is_first": None if first is None else first[0],
                "cell": None if first is None else first[1],
                "mode": "enforced",
                "has_own": True,
                "has_tenancy": True,
            }

        async def execute(self, _sql):
            pass

        async def fetchval(self, _sql, *_args):
            return "on"

    @asynccontextmanager
    async def dedicated(**_kwargs):
        yield Conn()

    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(cell_triggers, "missing", AsyncMock(return_value=[]))
    rows = await doctor_cell._checks(object())
    result = next((r for r in rows if r[0] == "scim_token"), None)
    assert result is not None and result[1] == grade
    assert len(reads) == 1
    assert raw not in result[2] if raw else True


@pytest.mark.parametrize("failure", ["before-boot", "deployment", "state"])
async def test_unread_doctor_identity_never_becomes_a_passing_policy(
    configured, monkeypatch, failure
):
    if failure == "before-boot":
        monkeypatch.setattr(
            doctor_cell, "_deployment_check", AsyncMock(return_value="before the first boot")
        )
    elif failure == "deployment":
        monkeypatch.setattr(
            doctor_cell, "_deployment_check", AsyncMock(side_effect=doctor_cell._Refused("refused"))
        )
    monkeypatch.setattr(
        doctor_cell, "_marked_session", AsyncMock(side_effect=OSError("state unreadable"))
    )
    rows = await doctor_cell._checks(object())
    assert any(name == "scim_token" and grade == "FAIL" for name, grade, _ in rows)


@pytest.mark.parametrize("raw", [OWN, LEGACY, ""])
def test_standalone_doctor_keeps_generic_no_store_policy(configured, monkeypatch, raw):
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "scim_token", raw)
    assert doctor._doctor_scim_token_check(settings)[1] == "PASS"
