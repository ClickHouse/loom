"""Inspection reads admitted title scalars without document bodies or diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from server.knowledge.store import source_documents as store
from tests.unit.ch_fake import RecordingCH


@pytest.fixture
def world(monkeypatch):
    row = {
        "org": "acme",
        "namespace": "acl-" + "1" * 24,
        "source_uri": "gdrive://file#11111111-1111-4111-8111-111111111111",
        "version": 11,
        "revision": 0,
        "title": "Title",
        "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
        "superseded_at": None,
        "doc_format": "markdown",
        "source_scheme": "gdrive",
        "source_revision": "r1",
    }
    monkeypatch.setattr(store.pg, "enabled", lambda: False)
    ch = RecordingCH().on("FROM loom.documents FINAL", [row])
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    return row, ch


async def test_clickhouse_title_projection_binds_one_admitted_live_identity(world):
    row, ch = world
    assert await store.title_projection("acme", row["namespace"], row["source_uri"], 11, 0) == row
    statement = ch.calls[-1]
    assert statement.params == {
        "org": "acme",
        "ns": row["namespace"],
        "uri": row["source_uri"],
        "version": 11,
        "revision": 0,
    }
    assert "version = {version:UInt64}" in statement.sql
    assert "revision = {revision:UInt64}" in statement.sql
    assert "superseded_at IS NULL LIMIT 1" in statement.sql
    selected = statement.sql.split(" FROM ")[0]
    assert "AS doc_format" in selected and "AS source_revision" in selected
    assert "content_preview" not in selected and ", content," not in selected
    assert ", metadata," not in selected and "loom.chunks" not in statement.sql


async def test_missing_clickhouse_title_never_reads_body(world, monkeypatch):
    row, _ch = world
    ch = RecordingCH().on("FROM loom.documents FINAL", [])
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    assert await store.title_projection("acme", row["namespace"], row["source_uri"], 11, 0) is None
    assert len(ch.calls) == 1


async def test_postgres_title_projection_transfers_only_selected_scalars(world, monkeypatch):
    row, ch = world
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    saved = {**row, "ingested_at": row["ingested_at"].isoformat()}
    fetch = AsyncMock(return_value={"row": saved})
    monkeypatch.setattr(store.pg, "fetchrow", fetch)
    assert await store.title_projection("acme", row["namespace"], row["source_uri"], 11, 0) == row
    sql, *args = fetch.await_args.args
    assert args == ["acme", row["namespace"], row["source_uri"], "11", 0, "0"]
    assert "NOT h.deleted" in sql and "h.row ->> 'version' = $4" in sql
    assert "h.revision = $5" in sql and "h.row ->> 'revision' = $6" in sql
    assert "'content'" not in sql and "content_preview" not in sql and "chunks" not in sql
    assert "jsonb_build_object" in sql and "AS row" in sql
    projected = sql.rsplit("SELECT jsonb_build_object", 1)[1]
    assert "'metadata'," not in projected and sql.endswith("AS row FROM formatted")
    assert not ch.calls


async def test_missing_postgres_title_has_no_clickhouse_fallback(world, monkeypatch):
    row, ch = world
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    monkeypatch.setattr(store.pg, "fetchrow", AsyncMock(return_value=None))
    assert await store.title_projection("acme", row["namespace"], row["source_uri"], 11, 0) is None
    assert not ch.calls
