"""The keys a process's cell heartbeat carries beside its fingerprint (`serves`, `marked`,
`router`, `router_why`), the marker read in the cell refresh, the `cell_marker` readiness
check that reads it, the derived tenancy mode, and the boot refusal of a router URL that
cannot be requested (docs/architecture/cells.md, "The heartbeat's keys")."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server import boot
from server.core import cell, cell_triggers, instances, pg, readiness
from server.settings import settings

_OWN = {"v": 1, "fields": {}}


def _view(**fields: Any) -> cell._View:
    return cell._View(read_at=time.monotonic(), **fields)


@pytest.fixture(autouse=True)
def _cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "eu-1")
    monkeypatch.setattr(settings, "serves_traffic", True)
    monkeypatch.setattr(cell, "_view", cell._View())


def _reported() -> dict[str, Any]:
    return json.loads(instances.cell_row()["fingerprint"])


def test_until_the_first_read_lands_marked_and_router_are_null() -> None:
    """As on a boot with skipped Postgres migrations whose first read failed: the keys are
    present, which proves the build, and null."""
    reported = _reported()
    assert reported["serves"] is True
    assert reported["marked"] is None and reported["router"] is None
    assert "router_why" in reported
    assert instances.cell_row()["tenancy_mode"] == ""


def test_the_keys_sit_outside_the_fields_and_never_in_the_stored_value() -> None:
    reported = _reported()
    assert reported["fields"] == cell.mine()
    assert set(cell.as_value(cell.mine())) == {"v", "fields"}
    assert not {"serves", "marked", "router", "router_why"} & set(reported["fields"])
    # A fingerprint comparison ignores them: the heartbeat's value matches its own fields.
    assert cell.matches(cell.mine(), reported)


@pytest.mark.parametrize("marked", [True, False])
def test_marked_reports_what_the_refresh_read(
    monkeypatch: pytest.MonkeyPatch, marked: bool
) -> None:
    async def _row(sql: str) -> dict[str, Any]:
        assert f"current_setting('{pg.TENANCY_MARKER}', true)" in sql
        return {
            "first": {"is_first": True, "cell": "eu-1"},
            "mode": "recording",
            "own": {"current_fp": _OWN, "next_fp": None},
            "tenancy": False,
            "marked": marked,
        }

    monkeypatch.setattr(pg, "fetchrow_reset", _row)
    assert asyncio.run(cell.refresh()) is True
    reported = _reported()
    # An unmarked session (a pooler stripped the startup parameter) reports false, never
    # null: attach refuses it at once.
    assert reported["marked"] is marked
    assert reported["router"] is None  # a first cell whose reader has not answered yet
    assert instances.cell_row()["tenancy_mode"] == "recording"


def test_a_later_cell_reports_no_router_key_and_its_mode_as_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cell, "_view", _view(first=(False, "eu-1"), mode="recording", current=_OWN, marked=True)
    )
    reported = _reported()
    assert "router" not in reported and "router_why" not in reported
    assert reported["marked"] is True
    assert instances.cell_row()["tenancy_mode"] == "enforced"


def test_with_loom_cell_unset_no_router_key_and_the_derived_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(cell, "_view", _view(marked=False))
    reported = _reported()
    assert "router" not in reported and reported["marked"] is False
    assert instances.cell_row()["tenancy_mode"] == "open"
    monkeypatch.setattr(cell, "_view", _view(has_tenancy=True, marked=False))
    assert instances.cell_row()["tenancy_mode"] == "partial"


def test_serves_reports_loom_serves_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "serves_traffic", False)
    assert _reported()["serves"] is False


@pytest.mark.parametrize(
    "state,marked,ok,reason",
    [
        ("enforced", False, False, "refuse its writes"),
        ("partial", False, False, "refuse its writes"),
        ("recording", False, True, "once attach raises the mode"),
        ("open", False, True, ""),
        ("enforced", True, True, ""),
        ("unread", None, True, ""),
    ],
)
def test_the_cell_marker_check_reads_the_cached_marker(
    monkeypatch: pytest.MonkeyPatch, state: str, marked: bool | None, ok: bool, reason: str
) -> None:
    rows = {
        "enforced": {"first": (True, "eu-1"), "mode": "enforced", "current": _OWN},
        "partial": {"has_tenancy": True},
        "recording": {"first": (True, "eu-1"), "mode": "recording", "current": _OWN},
        "open": {},
    }
    view = cell._View() if state == "unread" else _view(marked=marked, **rows[state])
    monkeypatch.setattr(cell, "_view", view)
    found = cell_triggers.readiness_check()
    assert found.ok is ok
    assert (reason in found.reason) if reason else found.reason == ""


def test_the_cell_marker_check_runs_only_with_loom_cell_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(readiness, "_CHECKS", {})
    boot.register_readiness_checks(object())
    assert "cell_marker" in readiness.names()
    monkeypatch.setattr(readiness, "_CHECKS", {})
    monkeypatch.setattr(settings, "cell", "")
    boot.register_readiness_checks(object())
    assert "cell_marker" not in readiness.names() and "cell" in readiness.names()


@pytest.mark.parametrize(
    "url,refused",
    [
        ("", False),
        ("https://router.example", False),
        ("http://router:8080/base", False),
        ("router.example", True),
        ("https://router.example:70000", True),
        ("https://router.example/?x=1", True),
        ("https://router.example/#top", True),
        ("ftp://router.example", True),
    ],
)
def test_a_cell_refuses_to_boot_with_a_router_url_that_cannot_be_requested(
    monkeypatch: pytest.MonkeyPatch, url: str, refused: bool
) -> None:
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "public_base_url", "https://eu-1.example.test")
    monkeypatch.setattr(settings, "cell_router_url", url)
    problem = cell.settings_problem()
    assert ("LOOM_CELL_ROUTER_URL" in problem) is refused
    # A deployment that is not a cell is untouched.
    monkeypatch.setattr(settings, "cell", "")
    assert cell.settings_problem() == ""


@pytest.mark.parametrize("failure", [LookupError, asyncio.CancelledError])
def test_a_failed_reset_read_rolls_back_before_the_release(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    """The read fails, or is cancelled (a shutdown mid-refresh), after `BEGIN; RESET ALL`:
    the transaction is rolled back on the connection, so the pool gets it back idle, and
    the read's error or the cancel is raised."""
    sent: list[str] = []

    class _Conn:
        in_transaction = False

        def is_closed(self) -> bool:
            return False

        def is_in_transaction(self) -> bool:
            return self.in_transaction

        async def execute(self, sql: str) -> str:
            sent.append(sql)
            self.in_transaction = sql.startswith("BEGIN")
            return ""

        async def fetchrow(self, sql: str) -> Any:
            raise failure("relation does not exist")

    conn = _Conn()

    @contextlib.asynccontextmanager
    async def _connection() -> AsyncIterator[Any]:
        yield conn

    monkeypatch.setattr(pg, "connection", _connection)
    with pytest.raises(failure):
        asyncio.run(pg.fetchrow_reset(cell._READ))
    assert sent == ["BEGIN; RESET ALL", "ROLLBACK"] and not conn.in_transaction
