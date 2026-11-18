"""The real mirrored turn applies the private boundary before any lease or file work."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import diagnostics, request_work
from server.knowledge.app import extractors, private_sync, source_sync
from server.observability import cost_accumulator, events, provider_health
from tests.unit.test_knowledge_source_sync import DOCX, FOLDER, PDF, PPTX, _file
from tests.unit.test_knowledge_source_sync import world as world
from tests.unit.test_knowledge_source_sync_mirror import MIRROR, READERS, _mirrored
from tests.unit.test_knowledge_source_sync_mirror import mirror as mirror
from tests.unit.test_private_diagnostics import isolated_context as isolated_context


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(("mime", "fmt"), [(PDF, "pdf"), (PPTX, "pptx"), (DOCX, "docx")])
def test_extractor_name_is_generic_only_for_private_work(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch, private: bool, mime: str, fmt: str
) -> None:
    from tests.unit.test_knowledge_source_sync import _source

    original_extract = extractors.extract
    names: list[str] = []

    async def extract(*args: Any, **kwargs: Any) -> Any:
        names.append(kwargs["name"])
        return await original_extract(*args, **kwargs)

    monkeypatch.setattr(extractors, "extract", extract)
    config = {"folder_ids": [FOLDER], "include": [fmt]}
    world.sources = [_mirrored(config=config) if private else _source(config=config)]
    world.files = [_file("private-file", mime, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    result = asyncio.run(source_sync.run_sync())
    assert names == [f"source.{fmt}" if private else "private-file title"]
    [(written, _)] = world.upserts
    assert written.title == "private-file title"
    assert (
        written.namespace == MIRROR if private else world.sources[0].namespace == written.namespace
    )
    assert result.private_work is private
    assert not diagnostics.codes_only()


def test_private_boundary_starts_before_lease_and_listing(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.asynccontextmanager
    async def lease(src: Any) -> Any:
        assert diagnostics.codes_only()
        raise RuntimeError("private lease failure")
        yield

    monkeypatch.setattr(source_sync, "_source_turn", lease)
    world.sources = [_mirrored()]
    result = asyncio.run(source_sync.run_sync())
    assert result.private_work
    assert result.errors == ["acme/code-acme-app/decks: SYNC_FAILED"]
    assert not diagnostics.codes_only()


def test_mirrored_embedding_spend_lands_only_in_its_mirror(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.knowledge.app import writes

    saved = writes.handle_knowledge_upsert
    rows: list[dict[str, Any]] = []

    async def upsert(*args: Any, **kwargs: Any) -> Any:
        assert diagnostics.codes_only()
        cost_accumulator.report_embed("fake-ngram-1024", 17)
        return await saved(*args, **kwargs)

    async def insert(table: str, batch: list[dict[str, Any]], **kwargs: Any) -> None:
        assert diagnostics.codes_only()
        assert table == "loom.agent_events_private"
        rows.extend(batch)

    monkeypatch.setattr(writes, "handle_knowledge_upsert", upsert)
    monkeypatch.setattr(events, "get_client", lambda: SimpleNamespace(insert=insert))
    world.sources = [_mirrored()]
    world.files = [_file("private-file", PDF, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    emitter = events.EventEmitter()
    monkeypatch.setattr(events, "get_emitter", lambda: emitter)
    outer = cost_accumulator.begin_op()
    bag = request_work.begin()
    before = events.get_emitter().stats()
    asyncio.run(source_sync.run_sync())
    [row] = rows
    assert row["org"] == "acme" and row["namespace"] == MIRROR
    assert row["embedding_tokens"] == 17
    assert outer.embed_tokens == 0 and bag.ch_statements == 0
    assert events.get_emitter().stats() == before


def test_private_provider_health_and_cost_contexts_restore_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_health, "_state", {})
    outer = cost_accumulator.begin_op()
    before = provider_health.status()
    with pytest.raises(RuntimeError, match="private fault"):
        with private_sync.scope():
            cost_accumulator.report_embed("fake-ngram-1024", 17)
            provider_health.record_failure("fake", RuntimeError("private fault"))
            raise RuntimeError("private fault")
    assert provider_health.status() == before
    assert cost_accumulator.current() is outer and outer.embed_tokens == 0
    provider_health.record_success("fake")
    assert provider_health.status()["providers"]["fake"]["failing"] is False


def test_mirror_document_bound_never_sets_public_pass_cut(
    world: Any, mirror: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings
    from tests.unit.test_knowledge_source_sync import DOCS, _source

    monkeypatch.setattr(settings, "knowledge_source_mirror_documents_per_pass", 1)
    world.sources = [_mirrored(name="first"), _mirrored(name="second"), _source(name="ordinary")]
    world.files = [_file("doc", DOCS, "2026-01-01T00:00:00.000Z", permissions=READERS)]
    result = asyncio.run(source_sync.run_sync())
    assert [written.namespace for written, _ in world.upserts] == [MIRROR, _source().namespace]
    assert result.changed == 1 and result.synced == 1
    assert not result.cut
