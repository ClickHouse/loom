"""Inspection adds checked titles after ordinary diagnostics have finished."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.identity.api.models_findings import ReportCoverage
from server.knowledge.api.models_inspect import (
    ChunkTotals,
    DecisionTotals,
    DocumentTotals,
    ExtractionTotals,
    IngestionTotals,
    InspectDocument,
    KnowledgeInspectInput,
    KnowledgeInspectOutput,
)
from server.knowledge.app import inspect as app
from server.knowledge.app import source_access, source_titles

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def title(uri="gdrive:file#native", when=NOW):
    proof = SimpleNamespace(mirrored=True, check=AsyncMock(), fresh=lambda: True)
    return source_titles.Title(uri, "Mirror title", "markdown", "gdrive", "r1", 17, 0, when, proof)


def ordinary(uri="https://example.com/ordinary", when=NOW):
    return InspectDocument(
        source_uri=uri,
        title="Ordinary title",
        format="html",
        source_scheme="https",
        version=19,
        revision=1,
        ingested_at=when,
        retired_versions=3,
    )


@pytest.fixture
def world(monkeypatch):
    events = []
    plan = SimpleNamespace(marked=True, failed=False, unread=False)
    report = KnowledgeInspectOutput(
        org="acme",
        namespace="team",
        computed_at=NOW,
        documents=DocumentTotals(live=1, retired_versions=3),
        chunks=ChunkTotals(),
        decisions=DecisionTotals(definition="Ordinary decisions"),
        coverage=ReportCoverage(complete=True),
        ingestions=IngestionTotals(worker_enabled=False),
        extraction=ExtractionTotals(documents=0),
        listed=[ordinary()],
        listed_complete=True,
        skipped=["ordinary section unavailable"],
    )

    async def prepare(org, namespaces):
        assert (org, namespaces) == ("acme", ["team"])
        events.append("prepare")
        return plan

    async def inspect(_input):
        events.append("ordinary")
        return report

    async def collect(checked, namespace, *, limit):
        assert checked is plan and namespace == "team" and limit > 0
        events.append("titles")
        return [title()]

    monkeypatch.setattr(source_access, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(app, "_ordinary_inspect", AsyncMock(side_effect=inspect))
    monkeypatch.setattr(source_titles, "collect", AsyncMock(side_effect=collect))
    return plan, report, events


async def inspect(limit=20):
    return await app.handle_knowledge_inspect(
        KnowledgeInspectInput(org="acme", namespace="team", sample_limit=limit)
    )


async def test_checked_titles_are_last_and_do_not_change_ordinary_diagnostics(world):
    _plan, report, events = world
    before = report.model_dump(exclude={"listed", "listed_complete"})
    output = await inspect()
    assert events == ["prepare", "ordinary", "titles"]
    assert output.model_dump(exclude={"listed", "listed_complete"}) == before
    assert not output.listed_complete
    checked = next(doc for doc in output.listed if doc.title == "Mirror title")
    assert checked.source_uri == "gdrive:file#native"
    assert (checked.version, checked.revision) == (17, 0)
    assert checked.ingestion_id == ""
    for field in ("retired_versions", "chunks", "decisions", "extraction", "jobs"):
        assert getattr(checked, field) is None


async def test_public_sample_uses_utc_timestamp_then_uri_before_limit(world):
    world[1].listed = [ordinary("https://example.com/z", NOW.replace(tzinfo=None))]
    source_titles.collect.side_effect = None
    source_titles.collect.return_value = [
        title("gdrive:b", NOW + timedelta(seconds=1)),
        title("gdrive:z", NOW + timedelta(seconds=1)),
    ]
    output = await inspect(2)
    assert [doc.source_uri for doc in output.listed] == ["gdrive:z", "gdrive:b"]


async def test_unmarked_inspection_keeps_the_entire_ordinary_result(world):
    world[0].marked = False
    before = world[1].model_dump()
    output = await inspect()
    assert output.model_dump() == before
    source_titles.collect.assert_not_awaited()
    assert world[2] == ["prepare", "ordinary"]


async def test_denied_mirror_titles_disclose_no_new_counts_or_skip_reason(world):
    world[0].failed = True
    source_titles.collect.side_effect = None
    source_titles.collect.return_value = []
    before = world[1].model_dump(exclude={"listed_complete"})
    output = await inspect()
    assert output.model_dump(exclude={"listed_complete"}) == before
    assert not output.listed_complete


async def test_zero_sample_still_prepares_but_never_reads_mirror_titles(world):
    world[1].listed = []
    output = await inspect(0)
    assert output.listed == [] and not output.listed_complete
    assert world[2] == ["prepare", "ordinary"]
    source_titles.collect.assert_not_awaited()


async def test_title_cancellation_propagates(world):
    source_titles.collect.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await inspect()
