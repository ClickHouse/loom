"""Renewal pages advance past failed sources and reset exhausted scans durably."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.domain.source_push import PushRegistration
from server.knowledge.workers import source_push as worker
from server.workers import pass_context, registry


def registration(source_id: str) -> PushRegistration:
    return PushRegistration(
        org="org",
        namespace="docs",
        name="source",
        source_id=source_id,
        provider="gdrive",
        generation="gen",
        fingerprint="fp",
    )


@pytest.mark.asyncio
async def test_failed_source_does_not_starve_following_row(monkeypatch: pytest.MonkeyPatch) -> None:
    context = pass_context.begin("knowledge_source_push", resume_cursor='["before", "docs", "id"]')
    page = AsyncMock(return_value=[registration("one"), registration("two")])
    renew = AsyncMock(side_effect=[ConnectionError("temporary"), None])
    monkeypatch.setattr(worker, "require_ready", AsyncMock())
    monkeypatch.setattr(worker, "drive_page", page)
    monkeypatch.setattr(worker, "renew_drive", renew)
    result = await worker.run_pass()
    page.assert_awaited_once_with(("before", "docs", "id"))
    assert result == {"items_planned": 2, "items_done": 1, "failed": 1}
    assert context.count == 1 and json.loads(context.cursor) == ["", "", ""]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_pass,expected", [(120.0, 1), (180.0, 2)])
async def test_page_cut_persists_last_attempted_row(
    monkeypatch: pytest.MonkeyPatch, max_pass: float, expected: int
) -> None:
    context = pass_context.begin("knowledge_source_push")
    monkeypatch.setattr(worker, "require_ready", AsyncMock())
    monkeypatch.setattr(
        worker, "drive_page", AsyncMock(return_value=[registration("one"), registration("two")])
    )
    renew = AsyncMock()
    monkeypatch.setattr(worker, "renew_drive", renew)
    monkeypatch.setattr(worker.settings, "knowledge_source_push_max_pass_seconds", max_pass)
    clock = iter([0.0, 0.0, 80.0])
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: next(clock, 80.0)))
    result = await worker.run_pass()
    assert result["items_done"] == expected
    assert json.loads(context.cursor) == (["org", "docs", "one"] if expected == 1 else ["", "", ""])
    assert renew.await_count == expected


def test_registry_reads_configured_push_cadence_and_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker.settings, "knowledge_source_push_enabled", True)
    monkeypatch.setattr(worker.settings, "knowledge_source_push_interval_seconds", 733)
    monkeypatch.setattr(worker.settings, "knowledge_source_push_max_pass_seconds", 180.0)
    spec = next(spec for spec in registry.enabled_workers() if spec.name == "knowledge_source_push")
    assert spec.interval_seconds == 733
    assert spec.deadline_seconds() == 180.0


@pytest.mark.asyncio
async def test_page_cursor_advances_between_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    context = pass_context.begin("knowledge_source_push", resume_cursor='["org", "docs", "before"]')
    rows = [
        registration("same-id"),
        dataclasses.replace(registration("same-id"), namespace="other-docs"),
    ]
    page = AsyncMock(return_value=rows)
    renew = AsyncMock()
    cursors: list[list[str]] = []
    set_cursor = pass_context.set_cursor

    def capture_cursor(cursor: str) -> None:
        cursors.append(json.loads(cursor))
        set_cursor(cursor)

    monkeypatch.setattr(worker, "require_ready", AsyncMock())
    monkeypatch.setattr(worker, "drive_page", page)
    monkeypatch.setattr(worker, "renew_drive", renew)
    monkeypatch.setattr(pass_context, "set_cursor", capture_cursor)
    assert await worker.run_pass() == {"items_planned": 2, "items_done": 2, "failed": 0}
    page.assert_awaited_once_with(("org", "docs", "before"))
    assert [call.args[0] for call in renew.await_args_list] == rows
    assert cursors == [["org", "docs", "same-id"], ["org", "other-docs", "same-id"], ["", "", ""]]
    assert json.loads(context.cursor) == ["", "", ""]
