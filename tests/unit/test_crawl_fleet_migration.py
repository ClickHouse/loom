"""Crawl admission waits for the migration that supplies its authorization state."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.core.errors import OverloadedError
from server.knowledge.app import crawl_sources
from server.knowledge.domain.sources import KnowledgeSource


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [112, 113, 114])
async def test_registration_requires_the_crawl_migration_on_every_process(
    monkeypatch: pytest.MonkeyPatch, supported: int
) -> None:
    client = object()
    permit = object()
    scope = object()
    required_versions: list[int] = []
    validation = AsyncMock()

    async def fleet_at(ch: object, required: int) -> bool:
        assert ch is client
        required_versions.append(required)
        return supported >= required

    monkeypatch.setattr(crawl_sources, "get_client", lambda: client)
    monkeypatch.setattr(crawl_sources.crawl_authority, "scope", lambda _source: scope)
    monkeypatch.setattr(crawl_sources._Draft, "validate", validation)
    monkeypatch.setattr(
        crawl_sources.source_registration, "current_permit", AsyncMock(return_value=permit)
    )
    monkeypatch.setattr(crawl_sources.machines, "fleet_at", fleet_at)
    source = KnowledgeSource("org", "namespace", "name", "gdrive", "source")

    if supported < 114:
        with pytest.raises(OverloadedError, match="not every process enforces crawl authority"):
            await crawl_sources._draft(source, None)
        assert validation.await_count == 1
    else:
        draft = await crawl_sources._draft(source, None)
        assert draft.source is source and draft.permit is permit
        assert validation.await_count == 2
    assert required_versions == [114]


@pytest.mark.asyncio
async def test_composed_fixture_advertises_the_crawl_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import instances
    from tests.unit.test_crawl_composed_flow import _World

    world = _World.__new__(_World)
    monkeypatch.setattr(instances, "read_live", world.read_live)

    assert await crawl_sources.machines.fleet_at(world, 114)
