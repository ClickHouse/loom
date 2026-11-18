"""List metadata groups complete families before any cursor or public limit."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.knowledge.store import source_documents as store
from tests.unit.ch_fake import RecordingCH

NS = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"
SOURCES = {(NS, SID): "team"}


async def test_ch_metadata_stream_groups_families_without_filtering_or_page_cap(monkeypatch):
    monkeypatch.setattr(store.pg, "enabled", lambda: False)
    row = {
        "namespace": NS,
        "source_uri": "gdrive:file#" + SID,
        "public_namespace": "team",
        "public_uri": "gdrive:file",
    }
    ch = RecordingCH().on("FROM loom.documents FINAL", [row])
    ch.install(
        monkeypatch,
        "server.knowledge.store.source_documents",
        "server.knowledge.store.source_candidates",
    )
    async with store.list_candidates("acme", SOURCES, ["anyone"]) as stream:
        assert [r async for r in stream] == [row]
    statement = ch.calls[-1]
    assert statement.params["org"] == "acme" and statement.params["nss"] == [NS]
    assert statement.params["bindings"] == [(NS, SID)]
    assert statement.params["originals"] == ["team"]
    assert "LIMIT" not in statement.sql and "OFFSET" not in statement.sql
    assert "ORDER BY public_namespace, public_uri, namespace, source_uri" in statement.sql
    assert "hasAny(access_keys" in statement.sql and "superseded_at IS NULL" in statement.sql
    assert "content" not in statement.sql and "title" not in statement.sql
    assert "FORMAT JSONEachRow" in statement.sql


async def test_pg_metadata_releases_bounded_page_before_admission_and_never_ch_coarse_acl(
    monkeypatch,
):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    row = {
        "namespace": NS,
        "source_uri": "gdrive:file#" + SID,
        "public_namespace": "team",
        "public_uri": "gdrive:file",
    }
    events = []

    async def fetch(sql, *args):
        assert args[:4] == ("acme", [NS], [SID], ["team"])
        assert args[4:] == ("", "", "", "")
        assert "LIMIT 64" in sql and "NOT h.deleted" in sql
        assert 'COLLATE "C"' in sql and "access_keys" not in sql
        assert "h.row FROM" not in sql and "content" not in sql
        events.extend(["open", "released"])
        return [row]

    monkeypatch.setattr(store.pg, "fetch", AsyncMock(side_effect=fetch))
    monkeypatch.setattr(store, "get_client", lambda: pytest.fail("No ClickHouse fallback"))
    async with store.list_candidates("acme", SOURCES, ["untrusted-coarse-veto"]) as stream:
        async for result in stream:
            assert result == row
            break
        assert events == ["open", "released"]
    assert events == ["open", "released"]


async def test_pg_discovery_reads_every_keyset_page_without_candidate_cutoff(monkeypatch):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    rows = [
        {
            "namespace": NS,
            "source_uri": f"gdrive:{i:04d}#" + SID,
            "public_namespace": "team",
            "public_uri": f"gdrive:{i:04d}",
        }
        for i in range(130)
    ]
    fetch = AsyncMock(side_effect=[rows[:64], rows[64:128], rows[128:]])
    monkeypatch.setattr(store.pg, "fetch", fetch)
    async with store.list_candidates("acme", SOURCES, None) as stream:
        assert [r async for r in stream] == rows
    assert fetch.await_count == 3
    for call, last in zip(fetch.await_args_list[1:], [rows[63], rows[127]], strict=True):
        assert call.args[-4:] == tuple(
            last[k] for k in ("public_namespace", "public_uri", "namespace", "source_uri")
        )


async def test_pg_discovery_rejects_nonadvancing_page_instead_of_looping(monkeypatch):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    row = {
        "namespace": NS,
        "source_uri": "gdrive:file#" + SID,
        "public_namespace": "team",
        "public_uri": "gdrive:file",
    }
    monkeypatch.setattr(store.pg, "fetch", AsyncMock(return_value=[row, row]))
    with pytest.raises(ValueError):
        async with store.list_candidates("acme", SOURCES, None) as stream:
            _ = [r async for r in stream]


@pytest.mark.parametrize("authority", ["clickhouse", "postgres"])
async def test_ordinary_shadow_lookup_is_metadata_only_and_independent_of_cursor(
    monkeypatch, authority
):
    monkeypatch.setattr(store.pg, "enabled", lambda: authority == "postgres")
    pairs = [("team", "gdrive:file"), ("other", "gdrive:second")]
    rows = [{"namespace": "team", "source_uri": "gdrive:file"}]
    ch = RecordingCH().on("FROM loom.documents FINAL", rows)
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(store.pg, "fetch", fetch)
    assert await store.ordinary_families("acme", pairs) == {("team", "gdrive:file")}
    sql = fetch.await_args.args[0] if authority == "postgres" else ch.calls[-1].sql
    selected = sql.split("FROM", 1)[0]
    assert "content" not in selected and "tags" not in selected and "title" not in selected
    assert "ingested_at <" not in sql and "cursor" not in sql
    assert "superseded_at" in sql
    assert len(ch.calls) == 1


async def test_ordinary_shadow_lookup_enforces_bounded_batch_before_any_io(monkeypatch):
    monkeypatch.setattr(store, "get_client", lambda: pytest.fail("No oversized query"))
    monkeypatch.setattr(
        store.pg, "fetch", AsyncMock(side_effect=AssertionError("No oversized query"))
    )
    with pytest.raises(ValueError):
        await store.ordinary_families("acme", [("team", str(i)) for i in range(65)])


@pytest.mark.parametrize("authority", ["clickhouse", "postgres"])
async def test_unexpected_shadow_identity_is_refused(monkeypatch, authority):
    monkeypatch.setattr(store.pg, "enabled", lambda: authority == "postgres")
    rows = [{"namespace": "foreign", "source_uri": "gdrive:file"}]
    ch = RecordingCH().on("FROM loom.documents FINAL", rows)
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    monkeypatch.setattr(store.pg, "fetch", AsyncMock(return_value=rows))
    with pytest.raises(ValueError):
        await store.ordinary_families("acme", [("team", "gdrive:file")])


async def test_pg_shadow_union_keeps_unheaded_published_and_unpublished_ordinary_copies(
    monkeypatch,
):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    ch = RecordingCH().on(
        "FROM loom.documents FINAL", [{"namespace": "team", "source_uri": "published"}]
    )
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    monkeypatch.setattr(
        store.pg,
        "fetch",
        AsyncMock(return_value=[{"namespace": "team", "source_uri": "head-only"}]),
    )
    assert await store.ordinary_families(
        "acme", [("team", "published"), ("team", "head-only")]
    ) == {("team", "published"), ("team", "head-only")}


@pytest.mark.parametrize("include", [False, True])
async def test_ordinary_tombstones_shadow_only_when_requested(monkeypatch, include):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)
    ch = RecordingCH().on("FROM loom.documents FINAL", [])
    ch.install(monkeypatch, "server.knowledge.store.source_documents")
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(store.pg, "fetch", fetch)
    assert (
        await store.ordinary_families("acme", [("team", "file")], include_superseded=include)
        == set()
    )
    assert ("superseded_at IS NULL" in ch.calls[-1].sql) is not include
    assert ("NOT h.deleted" in fetch.await_args.args[0]) is not include
