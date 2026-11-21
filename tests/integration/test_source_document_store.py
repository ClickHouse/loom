"""Exact document content reads never substitute another tenant, head or revision."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.integration._resources import unique_org


async def test_content_identity_and_pending_head_are_exact() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.domain.access_list import AccessList, to_json
    from server.knowledge.store import documents, pg_documents, source_documents

    org, mirror, uri = (
        unique_org("checked-content"),
        new_mirror_name(),
        "gdrive:file#" + str(uuid4()),
    )
    ch = get_client()
    set_query_tag("test.checked_document_content")
    row = documents.build_document_row(
        org=org,
        namespace=mirror,
        source_uri=uri,
        doc_id=str(uuid4()),
        title="Checked title",
        description="Checked description",
        content="Exact content",
        metadata='{"key":"checked"}',
        tags=["checked"],
        version=10,
        revision=0,
        ingested_at=datetime.now(UTC).replace(microsecond=0, tzinfo=None),
        agent="fixture",
        user_id="fixture-user",
        session_id="fixture-session",
        actor="process:fixture",
        source_class="external",
        access_json=to_json(AccessList(owner="owner")),
    )
    try:
        await ch.connect()
        await ch.insert("loom.documents", [row])
        if pg.enabled():
            assert await source_documents.contents(org, mirror, uri, 10, 0) is None
            await pg_documents.commit([row], over={}, targets={uri: 1})
        got = await source_documents.contents(org, mirror, uri, 10, 0)
        assert got is not None and got.model_dump() == row
        assert (
            await source_documents.contents(unique_org("foreign-content"), mirror, uri, 10, 0)
            is None
        )
        assert await source_documents.contents(org, new_mirror_name(), uri, 10, 0) is None
        assert await source_documents.contents(org, mirror, uri + "other", 10, 0) is None
        assert await source_documents.contents(org, mirror, uri, 11, 0) is None
        assert await source_documents.contents(org, mirror, uri, 10, 1) is None
        next_row = {**row, "version": 11, "content": "Current head only"}
        if pg.enabled():
            await pg_documents.commit([next_row], over={uri: 10}, targets={uri: 1})
        else:
            await ch.insert("loom.documents", [next_row])
        assert await source_documents.contents(org, mirror, uri, 10, 0) is None
        current = await source_documents.contents(org, mirror, uri, 11, 0)
        assert current is not None and current.model_dump() == next_row
        deleted = {
            **next_row,
            "version": 12,
            "superseded_at": row["ingested_at"],
            "access_json": "",
        }
        if pg.enabled():
            await pg_documents.commit([deleted], over={uri: 11}, targets={uri: 1})
        else:
            await ch.insert("loom.documents", [deleted])
        assert await source_documents.contents(org, mirror, uri, 11, 0) is None
        assert await source_documents.contents(org, mirror, uri, 12, 0) is None
    finally:
        try:
            await ch.disconnect()
        finally:
            await pg.close_pool()


async def test_document_chunk_reads_include_stubs_without_widening_search() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.store import source_candidates, source_documents

    org, mirror, uri = (
        unique_org("document-stubs"),
        new_mirror_name(),
        "gdrive:file#" + str(uuid4()),
    )
    model = "fixture"
    ch = get_client()
    set_query_tag("test.checked_document_stubs")
    rows = [
        {
            "org": org,
            "namespace": mirror,
            "source_uri": uri,
            "chunk_idx": idx,
            "doc_version": 10,
            "chunk_metadata": '{"revision":0}',
            "embedding_model": model,
            "embedding_dim": 3072 if idx == 0 else 0,
            "embedding": [1.0] + [0.0] * 3071,
            "content": "embedded" if idx == 0 else "unembedded section",
        }
        for idx in range(2)
    ]
    keys = [(mirror, uri, idx, 10, 0) for idx in range(2)]
    try:
        await ch.connect()
        await ch.insert("loom.chunks", rows)
        async with source_documents.chunk_candidates(org, mirror, uri, model) as stream:
            discovered = [row async for row in stream]
        assert [int(row["chunk_idx"]) for row in discovered] == [0, 1]
        searchable = await source_candidates.chunks(org, model, keys)
        assert [row["content"] for row in searchable] == ["embedded"]
        document = await source_candidates.chunks(org, model, keys, embedded_only=False)
        assert {row["chunk_idx"]: row["content"] for row in document} == {
            0: "embedded",
            1: "unembedded section",
        }
        async with source_documents.chunk_candidates(org, mirror, uri, "other-model") as stream:
            assert [row async for row in stream] == []
    finally:
        await ch.disconnect()


@pytest.mark.parametrize(
    "content",
    ["é" * 110 + "large body" * 10000, "a\x00b" + "body" * 10000],
    ids=["unicode-preview", "encoded-text"],
)
async def test_list_projection_and_count_stay_in_one_live_copy(content: str) -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.domain.serving import Serving
    from server.knowledge.store import documents, pg_documents, source_documents

    org, mirror, uri = unique_org("checked-list"), new_mirror_name(), "gdrive:file#" + str(uuid4())
    ch = get_client()
    set_query_tag("test.checked_list_projection")
    row = documents.build_document_row(
        org=org,
        namespace=mirror,
        source_uri=uri,
        doc_id=str(uuid4()),
        title="List title",
        description="List description",
        content=content,
        tags=["kind:notes"],
        version=10,
        revision=0,
        ingested_at=datetime.now(UTC).replace(microsecond=0, tzinfo=None),
        agent="fixture",
        actor="process:fixture",
        source_class="external",
        user_id="fixture-user",
        session_id="fixture-session",
        blob_path="a" * 64,
        blob_size=123,
        blob_content_type="application/pdf",
        original_filename="notes.pdf",
    )
    chunks = [
        {
            "org": org,
            "namespace": mirror,
            "source_uri": uri,
            "embedding_model": "fixture",
            "chunk_idx": i,
            "doc_version": 10,
            "chunk_metadata": '{"revision":0}',
            "content": "section",
            "embedding_dim": 0,
            "embedding": [0.0] * 3072,
        }
        for i in (0, 1, 1)
    ]
    try:
        await ch.connect()
        await ch.insert("loom.documents", [row])
        await ch.insert(
            "loom.chunks",
            [*chunks, {**chunks[0], "chunk_idx": 99, "chunk_metadata": '{"revision":1}'}],
        )
        serving = None
        if pg.enabled():
            assert await source_documents.list_projection(org, mirror, uri, 10, 0) is None
            await pg_documents.commit([row], over={}, targets={uri: 1}, chunked={uri})
            # A metadata-only commit keeps the recorded chunks at version 10.
            row = {**row, "version": 11, "title": "Current head title"}
            await pg_documents.commit([row], over={uri: 10}, targets={uri: 1})
            await ch.insert("loom.chunks", [{**chunks[0], "chunk_idx": 98, "doc_version": 11}])
            serving = Serving(False, 0, 0, 10)
        got = await source_documents.list_projection(org, mirror, uri, row["version"], 0)
        assert got == {
            key: row[key]
            for key in (
                "org",
                "namespace",
                "source_uri",
                "version",
                "revision",
                "title",
                "description",
                "tags",
                "ingested_at",
                "superseded_at",
                "blob_path",
                "blob_size",
                "blob_content_type",
                "original_filename",
            )
        } | {"content_preview": content.encode("utf-8")[:220].decode("utf-8", "replace")}
        assert await source_documents.list_chunk_count(org, mirror, uri, 0, serving) == 2
        assert (
            await source_documents.list_projection(org + "-other", mirror, uri, row["version"], 0)
            is None
        )
        assert (
            await source_documents.list_projection(org, new_mirror_name(), uri, row["version"], 0)
            is None
        )
        assert (
            await source_documents.list_projection(org, mirror, uri + "other", row["version"], 0)
            is None
        )
        assert (
            await source_documents.list_projection(org, mirror, uri, row["version"] + 1, 0) is None
        )
        assert await source_documents.list_projection(org, mirror, uri, row["version"], 1) is None
        deleted = {**row, "version": row["version"] + 1, "superseded_at": row["ingested_at"]}
        if pg.enabled():
            await pg_documents.commit([deleted], over={uri: row["version"]}, targets={uri: 1})
        else:
            await ch.insert("loom.documents", [deleted])
        assert await source_documents.list_projection(org, mirror, uri, row["version"], 0) is None
        assert (
            await source_documents.list_projection(org, mirror, uri, deleted["version"], 0) is None
        )
    finally:
        try:
            await ch.disconnect()
        finally:
            await pg.close_pool()


async def test_list_discovery_pages_all_families_and_shadows_ordinary_rows(monkeypatch) -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.domain.access_list import AccessList, to_json
    from server.knowledge.store import documents, pg_documents, source_documents
    from server.settings import settings

    if pg.enabled():
        await pg.close_pool()
        monkeypatch.setattr(settings, "pg_pool_min_size", 1)
        monkeypatch.setattr(settings, "pg_pool_max_size", 1)

    org, first, second = unique_org("list-families"), new_mirror_name(), new_mirror_name()
    sid, other_sid = str(uuid4()), str(uuid4())
    sources = {(first, sid): "team", (second, other_sid): "team"}
    ch = get_client()
    set_query_tag("test.checked_list_discovery")
    at = datetime.now(UTC).replace(microsecond=0, tzinfo=None)

    def document(namespace, uri, tags):
        return documents.build_document_row(
            org=org,
            namespace=namespace,
            source_uri=uri,
            doc_id=str(uuid4()),
            content="body",
            title="Title",
            tags=tags,
            version=10,
            revision=0,
            ingested_at=at,
            agent="fixture",
            actor="process:fixture",
            source_class="external",
            access_json=to_json(AccessList(anyone=True)),
        )

    rows = [document(first, f"gdrive:{i:04d}#" + sid, ["source-id:" + sid]) for i in range(130)]
    rows.append(document(first, "gdrive:K#native#" + sid, ["source-id:" + sid]))
    other = document(second, "gdrive:0000#" + other_sid, ["source-id:" + other_sid])
    try:
        await ch.connect()
        if pg.enabled():
            await pg_documents.commit(rows, over={}, targets={r["source_uri"]: 1 for r in rows})
            await pg_documents.commit([other], over={}, targets={other["source_uri"]: 1})
            # Published mirror rows with no head cannot enter the PG metadata stream.
            await ch.insert(
                "loom.documents", [document(first, "gdrive:ch-only#" + sid, ["source-id:" + sid])]
            )
        else:
            await ch.insert("loom.documents", [*rows, other])
        async with source_documents.list_candidates(org, sources, ["anyone"]) as stream:
            found = []
            async for row in stream:
                found.append(row)
                if pg.enabled() and len(found) % 64 == 1:
                    # Admission can borrow the sole connection between metadata pages.
                    assert await pg.fetchval("SELECT 1") == 1
        expected = [
            {
                "namespace": row["namespace"],
                "source_uri": row["source_uri"],
                "public_namespace": "team",
                "public_uri": row["source_uri"].rsplit("#", 1)[0],
            }
            for row in [*rows, other]
        ]

        def order(row):
            return tuple(
                row[k] for k in ("public_namespace", "public_uri", "namespace", "source_uri")
            )

        assert found == sorted(expected, key=order)
        assert len(found) == 132
        async with source_documents.list_candidates(org + "-other", sources, ["anyone"]) as stream:
            assert [row async for row in stream] == []

        published = document("team", "gdrive:0000", [])
        retired = {**document("team", "gdrive:retired", []), "superseded_at": at}
        await ch.insert("loom.documents", [published, retired])
        pairs = [("team", uri) for uri in ("gdrive:0000", "gdrive:retired", "gdrive:head-only")]
        expected_shadows = {("team", "gdrive:0000")}
        if pg.enabled():
            head_only = document("team", "gdrive:head-only", [])
            await pg_documents.commit([head_only], over={}, targets={head_only["source_uri"]: 1})
            expected_shadows.add(("team", "gdrive:head-only"))
        assert await source_documents.ordinary_families(org, pairs) == expected_shadows
        assert await source_documents.ordinary_families(
            org, pairs, include_superseded=True
        ) == expected_shadows | {("team", "gdrive:retired")}
        assert (
            await source_documents.ordinary_families(org + "-other", pairs, include_superseded=True)
            == set()
        )
    finally:
        try:
            await ch.disconnect()
        finally:
            await pg.close_pool()
