"""A source without release_health_snapshot answers the
release blockers alone; the read does not name the missing table."""

from __future__ import annotations

import types

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.tracker.store.source import contract_reports as reports
from tests.unit.test_tracker_source import ORG, PUB, REPO, _cfg, _Upstream  # noqa: F401


def _report(optional: list[str], mode: str = "views") -> types.SimpleNamespace:
    return types.SimpleNamespace(optional_present=optional, mode=mode, rewrite=lambda sql, db: sql)


@pytest.mark.asyncio
async def test_missing_health_table_drops_only_the_health_row(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    monkeypatch.setattr(reports, "cached", lambda cred: _report(["release_items"]))
    rel = await src.TrackerSource(ORG, PUB).release_items(ORG, PUB, "26.3")
    sql, params, _ = up.calls[-1]
    assert rel == [] and "release_health_snapshot" not in sql and "release_items FINAL" in sql
    assert params == {"v": "26.3", "repos": [REPO]}


@pytest.mark.asyncio
@pytest.mark.parametrize("rep", [_report([], mode="mapping"), _report(["release_health_snapshot"])])
async def test_present_or_unchecked_health_table_is_still_read(monkeypatch, rep) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    monkeypatch.setattr(reports, "cached", lambda cred: rep)
    await src.TrackerSource(ORG, PUB).release_items(ORG, PUB, "26.3")
    assert "release_health_snapshot" in up.calls[-1][0]


@pytest.mark.asyncio
async def test_unchecked_source_answers_without_the_health_row(monkeypatch) -> None:
    """The first read after a boot has no report yet; it must not ask for an
    optional table."""
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    monkeypatch.setattr(reports, "cached", lambda cred: None)
    await src.TrackerSource(ORG, PUB).release_items(ORG, PUB, "26.3")
    assert "release_health_snapshot" not in up.calls[-1][0]
