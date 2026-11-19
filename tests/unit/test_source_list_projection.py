"""A list projection reads bounded fields from exactly one admitted live version."""

from __future__ import annotations

import base64
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
        "source_uri": "gdrive:file#11111111-1111-4111-8111-111111111111",
        "version": 11,
        "revision": 0,
        "title": "Title",
        "description": "Description",
        "tags": ["kind:notes"],
        "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
        "superseded_at": None,
        "blob_path": "",
        "blob_size": 0,
        "blob_content_type": "",
        "original_filename": "",
        "content_preview": "Preview",
    }
    monkeypatch.setattr(store.pg, "enabled", lambda: False)
    ch = RecordingCH().on("FROM loom.documents FINAL", [row])
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    return row, ch


async def test_clickhouse_projection_uses_materialized_preview_at_exact_identity(world):
    row, ch = world
    result = await store.list_projection("acme", row["namespace"], row["source_uri"], 11, 0)
    assert result == row
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
    assert "content_preview" in selected
    assert "substring(content" not in selected and ", content," not in selected


async def test_missing_clickhouse_projection_never_fetches_full_content(world, monkeypatch):
    row, _ch = world
    ch = RecordingCH().on("FROM loom.documents FINAL", [])
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    assert await store.list_projection("acme", row["namespace"], row["source_uri"], 11, 0) is None
    assert len(ch.calls) == 1


@pytest.mark.parametrize("preview", [b"Preview", "é".encode() * 109 + b"a\xc3", b"a\x00b"])
async def test_postgres_projects_only_bounded_preview_and_preserves_other_fields(
    world, monkeypatch, preview
):
    row, ch = world
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    saved = {k: v for k, v in row.items() if k != "content_preview"}
    saved["ingested_at"] = row["ingested_at"].isoformat()
    saved["title"] = {"$loom_base64": base64.b64encode(b"a\x00b").decode()}
    fetch = AsyncMock(return_value={"row": saved, "preview": base64.b64encode(preview).decode()})
    monkeypatch.setattr(store.pg, "fetchrow", fetch)
    result = await store.list_projection("acme", row["namespace"], row["source_uri"], 11, 0)
    assert result["content_preview"] == preview.decode("utf-8", "replace")
    assert result["title"] == "a\x00b" and result["ingested_at"] == row["ingested_at"]
    sql, *args = fetch.await_args.args
    assert args == ["acme", row["namespace"], row["source_uri"], "11", 0, "0"]
    assert "NOT h.deleted" in sql and "h.row ->> 'version' = $4" in sql
    assert "h.revision = $5" in sql and "h.row ->> 'revision' = $6" in sql
    assert "h.row FROM" not in sql and "SELECT h.row," not in sql
    assert "for 220" in sql and "$loom_base64" in sql
    assert not ch.calls


async def test_missing_postgres_head_never_falls_back_to_clickhouse(world, monkeypatch):
    row, ch = world
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    monkeypatch.setattr(store.pg, "fetchrow", AsyncMock(return_value=None))
    assert await store.list_projection("acme", row["namespace"], row["source_uri"], 11, 0) is None
    assert not ch.calls
