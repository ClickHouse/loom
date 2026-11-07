"""A tracker read the source cannot admit names the failing check and the error's
kind; the driver's text, which can carry a statement or a server address, stays in
the log. CH-free."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.settings import settings
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source import base

_DRIVER_TEXT = "Code: 210. Connection refused (ch-internal-7:9440) SELECT * FROM loom.namespaces"


class _Gate:
    def __init__(self, *, contract: Exception | None = None, restricted: Exception | None = None):
        self.contract = contract
        self.restricted = restricted

    async def refresh_sources(self) -> None:
        return None

    def credential_configured(self, credential: str) -> bool:
        return True

    async def ensure_contract(self, cfg) -> None:
        if self.contract is not None:
            raise self.contract

    async def is_restricted(self, org: str, namespace: str) -> bool:
        if self.restricted is not None:
            raise self.restricted
        return True


def _source() -> base.SourceBase:
    src = base.SourceBase("o", "tracker-x")
    src.cfg = SimpleNamespace(credential="c", restricted=True, database="d")  # type: ignore[assignment]
    return src


@pytest.mark.parametrize(
    ("gate", "reason"),
    [
        (_Gate(contract=RuntimeError(_DRIVER_TEXT)), "contract check: RuntimeError"),
        (_Gate(restricted=RuntimeError(_DRIVER_TEXT)), "classification unavailable: RuntimeError"),
    ],
)
async def test_an_unreadable_check_names_its_kind_only(monkeypatch, gate, reason) -> None:
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    monkeypatch.setattr(base, "_gate", gate)
    with pytest.raises(TrackerUnavailable) as ei:
        await _source()._admit()
    assert str(ei.value) == reason


class _QueryGate(_Gate):
    async def query_rows(self, sql, params, **_kw):
        raise RuntimeError(_DRIVER_TEXT)


async def test_a_failed_read_names_its_tag_and_kind_only(monkeypatch) -> None:
    from server.tracker.store.source import contract_reports

    monkeypatch.setattr(settings, "tracker_live_serve", True)
    monkeypatch.setattr(base, "_gate", _QueryGate())
    monkeypatch.setattr(contract_reports, "cached", lambda credential: None)
    with pytest.raises(TrackerUnavailable) as ei:
        await _source()._rows("issue_by_number", "SELECT 1", {}, cache_key=["x"])
    assert str(ei.value) == "issue_by_number: RuntimeError"


async def test_a_source_check_that_cannot_reach_names_the_kind(monkeypatch) -> None:
    """`tracker.source.check` (viewer) reports an unreachable source: the error's kind;
    the driver's text is logged."""
    from types import SimpleNamespace as NS

    from server.tracker.api.models import TrackerSourceCheckInput
    from server.tracker.app import registry_sources
    from server.tracker.app import sources as tsources
    from server.tracker.domain import sources as dsources

    async def _refresh() -> None:
        return None

    async def _check(name: str, database: str) -> None:
        raise RuntimeError(_DRIVER_TEXT)

    monkeypatch.setattr(tsources, "_require_same_org", lambda org: None)
    monkeypatch.setattr(tsources, "_serves_trackers", lambda org: True)
    monkeypatch.setattr(registry_sources, "refresh_db_sources", _refresh)
    monkeypatch.setattr(dsources, "endpoint", lambda name: NS(database="qa"))
    monkeypatch.setattr(dsources, "credential_configured", lambda name: True)
    monkeypatch.setattr(tsources.contract, "check", _check)
    out = await tsources.handle_tracker_source_check(TrackerSourceCheckInput(org="o", name="qa"))
    assert out.reachable is False and out.contract["summary"] == "unreachable: RuntimeError"
