"""Signed list cursors traverse logical families, independently of stored copies."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from tests.integration._resources import unique_org


async def test_list_pages_never_repeat_old_copies_or_replace_ordinary_families(mint_test_token):
    base = os.environ.get("LOOM_BASE_URL") or pytest.skip("LOOM_BASE_URL not set")
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.auth.jwt import mint_token
    from server.core import pg
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.domain.access_list import AccessList, to_json
    from server.knowledge.domain.sources import KnowledgeSource
    from server.knowledge.store import documents, mirrors, pg_documents, sources
    from server.settings import settings

    assert settings.auth_actually_required(), "Run signed mirror HTTP cases in the auth-on profile"

    org, namespace = unique_org("list-pages"), "team"
    mirror = new_mirror_name()
    copies = [(mirror, sid) for sid in sorted((str(uuid4()) for _ in range(2)), reverse=True)]
    owner = mint_token(
        "source-owner",
        org,
        [namespace],
        ["viewer"],
        ttl_seconds=600,
        is_service_account=False,
        session_id=str(uuid4()),
    )
    outsider = mint_test_token(org=org, namespaces=[namespace], subject="outsider")
    owner_bot = mint_test_token(org=org, namespaces=[namespace], subject="source-owner")
    ch = get_client()
    set_query_tag("test.checked_list_pages")
    now = datetime.now(UTC).replace(microsecond=0)

    def row(ns, uri, stamp, title, tags, access=""):
        return documents.build_document_row(
            org=org,
            namespace=ns,
            source_uri=uri,
            doc_id=str(uuid4()),
            content=title,
            title=title,
            tags=tags,
            access_json=access,
            version=10,
            revision=0,
            ingested_at=now - timedelta(seconds=300 - stamp),
            agent="fixture",
            actor="process:fixture",
            source_class="external",
        )

    ordinary = row(namespace, "gdrive:f", 60, "Ordinary", ["ordinary"])
    private = []
    for index, (mirror, sid) in enumerate(copies):
        private.append(
            row(
                mirror,
                "gdrive:a#" + sid,
                100 if index == 0 else 80,
                "New" if index == 0 else "Old",
                ["source-id:" + sid, "new" if index == 0 else "old"],
                to_json(AccessList(owner="source-owner")),
            )
        )
    mirror, sid = copies[0]
    private.extend(
        [
            row(
                mirror,
                f"gdrive:{uri}#" + sid,
                stamp,
                title,
                ["source-id:" + sid, "wanted"],
                to_json(AccessList(owner="someone-else" if uri == "hidden" else "source-owner")),
            )
            for uri, stamp, title in [
                ("b", 90, "B"),
                ("c", 70, "C"),
                ("f", 150, "Mirror F"),
                ("hidden", 200, "Hidden"),
            ]
        ]
    )
    try:
        await ch.connect()
        await mirrors.add_mirror(org, namespace, mirror)
        for index, (mirror, sid) in enumerate(copies):
            await sources.insert_source(
                KnowledgeSource(
                    org=org,
                    namespace=namespace,
                    name="checked-" + str(index),
                    kind="gdrive",
                    source_id=sid,
                    items=[{"type": "file", "id": "synthetic-" + str(index)}],
                    access="mirrored",
                    active=True,
                )
            )
            await mirrors.record_listing(org, mirror, sid, "checked-" + str(index), now)
        await ch.insert("loom.documents", [ordinary])
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:

            async def listing(token, **fields):
                response = await client.post(
                    "/v1/knowledge.list",
                    json={"org": org, "namespace": namespace, **fields},
                    headers={"Authorization": "Bearer " + token},
                )
                assert response.status_code == 200, response.text
                return response.json()

            baseline = await listing(outsider)
            if pg.enabled():
                # Mirrors have committed heads and no published CH document rows.
                for mirror in sorted({item[0] for item in copies}):
                    selected = [item for item in private if item["namespace"] == mirror]
                    await pg_documents.commit(
                        selected, over={}, targets={item["source_uri"]: 1 for item in selected}
                    )
            else:
                await ch.insert("loom.documents", private)
            assert await listing(outsider) == await listing(owner_bot) == baseline
            pages, cursor = [], None
            for _ in range(5):
                result = await listing(owner, limit=1, **({"cursor": cursor} if cursor else {}))
                pages.extend(result["rows"])
                cursor = result["next_cursor"]
                if cursor is None:
                    break
            assert cursor is None
            assert [(item["source_uri"], item["title"]) for item in pages] == [
                ("gdrive:a", "New"),
                ("gdrive:b", "B"),
                ("gdrive:c", "C"),
                ("gdrive:f", "Ordinary"),
            ]
            assert all(item["chunk_count"] == 0 for item in pages)
            assert (await listing(owner, tags=["old"]))["rows"] == []
            assert [
                item["source_uri"] for item in (await listing(owner, tags=["wanted"]))["rows"]
            ] == ["gdrive:b", "gdrive:c"]
            selected = await client.post(
                "/v1/knowledge.get",
                json={"org": org, "namespace": namespace, "source_uri": "gdrive:a"},
                headers={"Authorization": "Bearer " + owner},
            )
            assert selected.status_code == 200 and selected.json()["content"] == "New"
            downloaded = await client.get(
                "/v1/knowledge.download",
                params={"org": org, "namespace": namespace, "source_uri": "gdrive:a"},
                headers={"Authorization": "Bearer " + owner},
            )
            assert downloaded.status_code == 200 and downloaded.content == b"New"
    finally:
        try:
            await ch.disconnect()
        finally:
            await pg.close_pool()
