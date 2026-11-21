"""Cutover diagnostics identify a proof phase without exposing its evidence."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest

from server.insights.domain.analysis import SettingNames
from server.insights.store import analysis, event_cutover
from tests.unit.test_private_event_cutover import world as world


async def test_discovery_failure_logs_only_phase_type_and_numeric_code(world, monkeypatch, caplog):
    async def refused(sql, params=None):
        raise RuntimeError(
            "Code: 497. private-policy SELECT private-key https://example.invalid/private-session"
        )

    monkeypatch.setattr(world, "query_all", refused)
    with (
        caplog.at_level("WARNING", logger=event_cutover.__name__),
        pytest.raises(
            event_cutover.EventCutoverUnavailable,
            match="^ordinary event storage is not confirmed private-free on every writer$",
        ),
    ):
        await event_cutover.confirm_ordinary_events(ch=world)

    records = [r for r in caplog.records if r.name == event_cutover.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.msg == "Event storage proof refused: stage=%s exception=%s code=%s"
    assert record.args == ("writers", "RuntimeError", 497)
    assert record.exc_info is None and record.stack_info is None
    assert record.__dict__.keys() <= logging.makeLogRecord({}).__dict__.keys() | {
        "message",
        "asctime",
    }
    assert all(
        text not in caplog.text
        for text in (
            "private-policy",
            "SELECT",
            "private-key",
            "example.invalid",
            "private-session",
        )
    )
    assert world.process_reads == world.census_reads == 0 and world.sent == []


async def test_private_census_refusal_logs_its_phase_and_still_blocks_grants(world, caplog):
    world.private_after_drain = True
    with (
        caplog.at_level("WARNING", logger=event_cutover.__name__),
        pytest.raises(event_cutover.EventCutoverUnavailable),
    ):
        await analysis.write_entities(
            ["GRANT SELECT ON loom.agent_events TO loom_analyst"],
            names=SettingNames("SQL_"),
            ch=world,
        )

    records = [r for r in caplog.records if r.name == event_cutover.__name__]
    assert len(records) == 1
    assert records[0].args == ("census", "EventCutoverUnavailable", None)
    assert records[0].exc_info is None and records[0].stack_info is None
    assert world.process_reads == 2 and world.census_reads == 1
    assert world.private_after_drain and world.sent == []


async def test_diagnostic_failure_preserves_refusal_cancellation_and_observer_cleanup(
    world, monkeypatch
):
    original_query, original_observers = world.query_all, world.policy_observers
    closed, warnings = [], []
    cancelled = False

    @asynccontextmanager
    async def observed(servers):
        try:
            async with original_observers(servers) as observers:
                yield observers
        finally:
            closed.append(True)

    async def query(sql, params=None):
        if "FROM system.processes" in sql:
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("Code: 497. private-policy")
        return await original_query(sql, params)

    def broken_warning(self, *args, **kwargs):
        warnings.append(True)
        raise ValueError("diagnostic sink failed")

    monkeypatch.setattr(world, "query_all", query)
    monkeypatch.setattr(world, "policy_observers", observed)
    monkeypatch.setattr(logging.Logger, "warning", broken_warning)
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world)
    cancelled = True
    with pytest.raises(asyncio.CancelledError):
        await event_cutover.confirm_ordinary_events(ch=world)
    assert warnings == [True] and closed == [True, True]
    assert world.census_reads == 0 and world.sent == []
