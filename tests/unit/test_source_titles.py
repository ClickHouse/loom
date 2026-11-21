"""Inspection titles retain private evidence without reading document diagnostics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import source_listing
from server.knowledge.app.source_access import Admitted
from server.knowledge.store import source_documents as store

SID = "11111111-1111-4111-8111-111111111111"
NS = "acl-" + "1" * 24
NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    items, rows = [], {}
    for index in range(2):
        uri = f"gdrive:file{index}#native#{SID}"
        document = {"version": 11, "revision": 0, "ingested_at": NOW}
        items.append(Admitted("team", {"namespace": NS, "source_uri": uri}, document))
        rows[uri] = {
            "org": "acme",
            "namespace": NS,
            "source_uri": uri,
            **document,
            "superseded_at": None,
            "title": f"Title {index}",
            "doc_format": "markdown",
            "source_scheme": "(none)",
            "source_revision": "r1",
        }
    plan = SimpleNamespace(org="acme", failed=False, unread=False)

    async def renew(_plan, selected):
        return selected

    async def finish(_plan, _input, selected, **_scope):
        return selected

    async def project(_org, _ns, uri, _version, _revision):
        return dict(rows[uri])

    monkeypatch.setattr(source_listing, "select", AsyncMock(return_value=items))
    monkeypatch.setattr(source_listing, "renew", AsyncMock(side_effect=renew))
    monkeypatch.setattr(source_listing, "finish", AsyncMock(side_effect=finish))
    monkeypatch.setattr(store, "title_projection", AsyncMock(side_effect=project))
    monkeypatch.setattr(store, "list_projection", AsyncMock(side_effect=AssertionError("preview")))
    monkeypatch.setattr(store, "list_chunk_count", AsyncMock(side_effect=AssertionError("chunks")))
    return plan, items, rows


async def collect(world, **kwargs):
    from server.knowledge.app import source_titles

    return await source_titles.collect(world[0], "team", limit=10, **kwargs)


async def test_title_leg_exposes_only_public_scalar_identity(world):
    titles = await collect(world)
    assert [title.source_uri for title in titles] == ["gdrive:file0#native", "gdrive:file1#native"]
    assert titles[0].title == "Title 0" and titles[0].format == "markdown"
    assert titles[0].source_scheme == "(none)" and titles[0].source_revision == "r1"
    assert (titles[0].version, titles[0].revision, titles[0].ingested_at) == (11, 0, NOW)
    assert titles[0].proof.plan is world[0]
    assert set(vars(titles[0])) == {
        "source_uri",
        "title",
        "format",
        "source_scheme",
        "source_revision",
        "version",
        "revision",
        "ingested_at",
        "proof",
    }
    store.list_projection.assert_not_awaited()
    store.list_chunk_count.assert_not_awaited()


async def test_source_identity_reaches_both_selection_and_last_barrier(world):
    await collect(world, source_id=SID)
    assert source_listing.select.await_args.kwargs["source_id"] == SID
    assert source_listing.finish.await_args.kwargs["source_id"] == SID


@pytest.mark.parametrize(
    "field", ["org", "namespace", "source_uri", "version", "revision", "ingested_at"]
)
async def test_wrong_projected_copy_discards_every_title(world, field):
    rows = world[2]
    row = rows[next(reversed(rows))]
    row[field] = "wrong" if field not in {"version", "revision"} else row[field] + 1
    assert await collect(world) == []
    assert not world[0].unread


async def test_missing_later_projection_discards_prior_title(world):
    first = next(iter(world[2].values()))
    store.title_projection.side_effect = [first, None]
    assert await collect(world) == []
    assert not world[0].unread


async def test_final_access_or_shadow_loss_discards_every_title(world):
    source_listing.finish.side_effect = None
    source_listing.finish.return_value = None
    assert await collect(world) == []
    assert store.title_projection.await_count == 2
    assert not world[0].unread


async def test_projection_failure_is_conditional_and_has_no_partial_result(world):
    first = next(iter(world[2].values()))
    store.title_projection.side_effect = [first, RuntimeError("private backend")]
    assert await collect(world) == []
    assert world[0].failed and not world[0].unread


async def test_cancellation_propagates_without_a_partial_title_list(world):
    store.title_projection.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await collect(world)
    assert not world[0].failed


async def test_zero_sample_reads_no_conditional_title_fields(world):
    from server.knowledge.app import source_titles

    assert await source_titles.collect(world[0], "team", limit=0) == []
    source_listing.select.assert_not_awaited()
    store.title_projection.assert_not_awaited()
