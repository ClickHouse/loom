"""Registration refuses conflicting or incomplete bounded reservation carriers."""

from __future__ import annotations

import contextlib
import copy
from dataclasses import replace

import pytest

from server.router import directory
from server.router.limits import MAX_BODY_BYTES, MAX_CELLS, MAX_CENSUS_BYTES
from tests.unit.test_cell_registration_pairs import _DirectoryConnection, _registration


class _ReservationConnection(_DirectoryConnection):
    def __init__(self, reg, reservations):
        super().__init__()
        self.reg = reg
        self.reservations = reservations
        self.reservation_reads = 0

    async def fetchrow(self, sql, *args):
        if sql == "SELECT cell, state FROM loom_router.cells WHERE is_first":
            assert args == ()
            return {"cell": "first", "state": "serving"}
        if sql in (
            f"{directory._CELL_REGISTERED} WHERE cell = $1",
            f"{directory._CELL_REGISTERED} WHERE cell = $1 FOR UPDATE",
        ):
            assert args == (self.reg.cell,)
            return None
        raise AssertionError("unexpected registration row read")

    async def fetchval(self, sql, *args):
        known = {
            "SELECT cell FROM loom_router.cells "
            "WHERE credential_sha256 = $1 OR credential_next_sha256 = $1": (
                self.reg.credential_sha256,
            ),
            "SELECT cell FROM loom_router.cells WHERE deployment_id = $1": (
                self.reg.deployment_id,
            ),
            "SELECT cell FROM loom_router.cells WHERE host = $1 "
            "UNION ALL SELECT cell FROM loom_router.hosts WHERE host = $1 LIMIT 1": (
                self.reg.host,
            ),
        }
        assert sql in known, "unexpected registration scalar read"
        assert args == known[sql]
        return None

    async def fetch(self, sql, *args):
        if sql == "SELECT cell, bucket_prefix FROM loom_router.cells WHERE bucket = $1":
            assert args == (self.reg.bucket,)
            return []
        assert sql == directory._REGISTRATION_RESERVATIONS, "unexpected reservation read"
        assert args == (
            MAX_CELLS + 1,
            MAX_CELLS,
            MAX_CENSUS_BYTES,
            MAX_BODY_BYTES,
        )
        self.reservation_reads += 1
        return copy.deepcopy(self.reservations)


def _scenario(kind):
    reg = _registration()
    reg.current_fp["fields"].update(
        bearer_issuers=["https://target.example"], end_user_orgs=["target-*"]
    )
    other = copy.deepcopy(reg.current_fp)
    other["fields"].update(bearer_issuers=["https://other.example"], end_user_orgs=["other-*"])
    rows = [{"cell": "other", "current_fp": other, "next_fp": None, "fits": True}]
    if kind == "current-current":
        other["fields"]["bearer_issuers"] = ["https://target.example"]
        reason = "shared bearer issuer reservation"
    elif kind == "next-current":
        pending = copy.deepcopy(reg.current_fp)
        pending["fields"]["bearer_issuers"] = ["https://pending.example"]
        reg = replace(reg, next_fp=pending)
        other["fields"]["bearer_issuers"] = ["https://pending.example"]
        reason = "shared bearer issuer reservation"
    elif kind == "current-next":
        pending = copy.deepcopy(other)
        pending["fields"]["end_user_orgs"] = ["target-one*"]
        rows[0]["next_fp"] = pending
        reason = "overlapping end-user org pattern reservation"
    elif kind == "unreadable":
        rows[0]["next_fp"] = {"v": True, "fields": {}}
        reason = "unreadable fingerprint reservation"
    elif kind == "not-fits":
        rows[0].update(current_fp=None, next_fp=None, fits=False)
        reason = "count or byte bounds"
    elif kind == "no-rows":
        rows = []
        reason = "count or byte bounds"
    elif kind == "null-pair":
        rows[0].update(current_fp=None, next_fp=None)
        reason = ""
    else:
        pending = copy.deepcopy(other)
        pending["fields"]["bearer_issuers"] = ["https://other-next.example"]
        rows[0]["next_fp"] = pending
        reason = ""
    return reg, rows, reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "current-current",
        "next-current",
        "current-next",
        "unreadable",
        "not-fits",
        "no-rows",
        "disjoint",
        "null-pair",
    ],
)
async def test_registration_checks_the_complete_bounded_reservation_carrier(monkeypatch, kind):
    reg, rows, reason = _scenario(kind)
    before = copy.deepcopy((reg, rows))
    conn = _ReservationConnection(reg, rows)

    @contextlib.asynccontextmanager
    async def connection(**_kwargs):
        yield conn

    monkeypatch.setattr(directory, "_connection", connection)
    refusal = await directory.refusal_before_probe(reg)
    if reason:
        assert reason in refusal
        with pytest.raises(directory.OperatorRefused, match=reason):
            await directory.register(reg)
        assert conn.reservation_reads == 2
    else:
        assert refusal == ""
        assert conn.reservation_reads == 1
    assert conn.writes == []
    assert (reg, rows) == before
