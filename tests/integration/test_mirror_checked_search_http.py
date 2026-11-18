"""Signed document reads follow current permissions under either storage authority."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from tests.integration._resources import unique_org


@pytest.mark.parametrize("mode", ["semantic", "lexical", "hybrid"])
async def test_signed_search_permissions_follow_current_heads(mode, mint_test_token) -> None:
    base = os.environ.get("LOOM_BASE_URL") or pytest.skip("LOOM_BASE_URL not set")
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.auth.jwt import mint_token
    from server.core import pg
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.embedding import get_embedder
    from server.knowledge.domain.access_list import AccessList, to_json
    from server.knowledge.domain.sources import KnowledgeSource
    from server.knowledge.store import blobs, mirrors, pg_documents, sources
    from server.llm.client import get_llm_client_for_model
    from server.settings import settings

    assert settings.auth_actually_required(), "Run signed mirror HTTP cases in the auth-on profile"

    embedder = get_embedder()
    assert embedder.model_name.startswith("fake-"), "This fixture uses only fake embeddings"
    assert not settings.llm_fallback_model.strip(), "This fixture uses no fallback provider"
    synthesis_client = get_llm_client_for_model(settings.memory_ask_synth_model.strip())
    assert synthesis_client.provider == "fake", "This fixture uses only fake synthesis"
    org, namespace, mirror = unique_org("checked-search"), "team", new_mirror_name()
    source = str(uuid4())
    query = "markedsearchcanary"
    private_content = query + " private\n\n# Pending\nUnembedded section."
    public_uri = "gdrive:checked-document#native-fragment"
    uri = public_uri + "#" + source
    vector = await embedder.embed(query)
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
    foreign = mint_test_token(org=unique_org("foreign-search"), namespaces=[namespace])
    ch = get_client()
    set_query_tag("test.checked_search_http")
    now = datetime.now(UTC).replace(microsecond=0)

    async def write_document(ns, address, content, access="", tags=()):
        row = {
            "org": org,
            "namespace": ns,
            "source_uri": address,
            "doc_id": str(uuid4()),
            "content": content,
            "title": "Checked search",
            "metadata": "{}",
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "tags": list(tags),
            "version": 10,
            "revision": 0,
            "superseded_at": None,
            "ingested_at": now,
            "access_json": access,
        }
        await ch.insert("loom.documents", [row])
        await ch.insert(
            "loom.chunks",
            [
                {
                    "org": org,
                    "namespace": ns,
                    "source_uri": address,
                    "chunk_idx": 0,
                    "content": content,
                    "doc_version": 10,
                    "chunk_metadata": json.dumps({"revision": 0}),
                    "embedding_model": embedder.model_name,
                    "embedding_dim": len(vector),
                    "embedding": vector,
                    "tags": list(tags),
                }
            ],
        )
        if pg.enabled():
            await pg_documents.commit([row], over={}, targets={address: 1}, chunked={address})
        return row

    async with httpx.AsyncClient(base_url=base, timeout=30) as client:

        async def search(token):
            response = await client.post(
                "/v1/knowledge.search",
                json={
                    "org": org,
                    "namespace": namespace,
                    "query": query,
                    "namespace_scope": "this",
                    "search_mode": mode,
                    "collapse_sections": False,
                    "top_k": 10,
                },
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def get(token):
            response = await client.post(
                "/v1/knowledge.get",
                json={"org": org, "namespace": namespace, "source_uri": public_uri},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def listing(token, **fields):
            response = await client.post(
                "/v1/knowledge.list",
                json={"org": org, "namespace": namespace, **fields},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def synthesize(token):
            response = await client.post(
                "/v1/knowledge.synthesize",
                json={
                    "org": org,
                    "namespace": namespace,
                    "source_uris": [public_uri],
                    "task": "Summarize this document",
                },
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def inspect(token):
            response = await client.post(
                "/v1/knowledge.inspect",
                json={"org": org, "namespace": namespace},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            report = response.json()
            report.pop("computed_at")
            return report

        async def source_inspect(token):
            response = await client.post(
                "/v1/knowledge.source.inspect",
                json={"org": org, "namespace": namespace, "name": "checked"},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def outline(token):
            response = await client.post(
                "/v1/knowledge.outline",
                json={
                    "org": org,
                    "namespace": namespace,
                    "source_uri": public_uri,
                    "section_path": ["Pending"],
                },
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def download(token):
            response = await client.get(
                "/v1/knowledge.download",
                params={"org": org, "namespace": namespace, "source_uri": public_uri},
                headers={"Authorization": "Bearer " + token},
            )
            headers = {
                key: response.headers.get(key)
                for key in (
                    "content-type",
                    "content-length",
                    "content-disposition",
                    "cache-control",
                    "x-loom-download-source",
                )
            }
            return response.status_code, response.content, headers

        try:
            await ch.connect()
            anonymous = await client.post(
                "/v1/knowledge.search",
                json={
                    "org": org,
                    "namespace": namespace,
                    "query": query,
                },
            )
            assert anonymous.status_code == 401
            # Install a trusted fixture directly; public source creation stays disabled.
            registration = KnowledgeSource(
                org=org,
                namespace=namespace,
                name="checked",
                kind="gdrive",
                source_id=source,
                created_at=now,
                items=[{"type": "file", "id": "synthetic-file"}],
                access="mirrored",
                active=True,
            )
            await write_document(namespace, "ordinary:visible", query + " ordinary")
            inactive_baselines = (
                await listing(outsider),
                await search(outsider),
                await get(outsider),
                await synthesize(outsider),
                await outline(outsider),
            )
            inactive_inspect_baseline = await inspect(outsider)
            inactive_download_baseline = await download(outsider)
            for ordinary in (*inactive_baselines, inactive_inspect_baseline):
                assert "mirrored" not in ordinary
            await sources.insert_source(registration)
            await mirrors.add_mirror(org, namespace, mirror)
            await mirrors.record_listing(org, mirror, source, "checked", now)
            baseline = await search(outsider)
            assert [r["source_uri"] for r in baseline["results"]] == ["ordinary:visible"]
            ordinary_list = await listing(outsider)
            assert [r["source_uri"] for r in ordinary_list["rows"]] == ["ordinary:visible"]
            ordinary_inspect = await inspect(outsider)
            assert [r["source_uri"] for r in ordinary_inspect["listed"]] == ["ordinary:visible"]
            assert not ordinary_inspect["listed_complete"]
            empty_source_inspect = await source_inspect(outsider)
            assert empty_source_inspect["documents"] == []
            missing_get = await get(outsider)
            assert not missing_get["found"]
            missing_synthesis = await synthesize(outsider)
            assert missing_synthesis["abstain"] and missing_synthesis["documents"] == 0
            missing_outline = await outline(outsider)
            assert not missing_outline["found"]
            missing_download = await download(outsider)
            assert missing_download[0] == 404
            row = await write_document(
                mirror,
                uri,
                private_content,
                to_json(AccessList(owner="source-owner")),
                ["source-id:" + source],
            )
            await ch.insert(
                "loom.chunks",
                [
                    {
                        "org": org,
                        "namespace": mirror,
                        "source_uri": uri,
                        "chunk_idx": 1,
                        "content": "Unembedded section.",
                        "doc_version": 10,
                        "chunk_metadata": json.dumps(
                            {
                                "revision": 0,
                                "section_path": ["Pending"],
                                "section_depth": 1,
                                "section_idx": 1,
                            }
                        ),
                        "embedding_model": embedder.model_name,
                        "embedding_dim": 0,
                        "embedding": [0.0] * len(vector),
                        "tags": ["source-id:" + source],
                    }
                ],
            )
            assert await search(outsider) == baseline
            assert await search(owner_bot) == baseline
            assert await listing(outsider) == await listing(owner_bot) == ordinary_list
            assert await inspect(outsider) == await inspect(owner_bot) == ordinary_inspect
            assert (
                await source_inspect(outsider)
                == await source_inspect(owner_bot)
                == empty_source_inspect
            )
            assert await get(outsider) == await get(owner_bot) == missing_get
            assert await synthesize(outsider) == await synthesize(owner_bot) == missing_synthesis
            assert await outline(outsider) == await outline(owner_bot) == missing_outline
            assert await download(outsider) == await download(owner_bot) == missing_download
            allowed = await search(owner)
            private = [r for r in allowed["results"] if r["source_uri"] == public_uri]
            assert len(private) == 1 and private[0]["content"] == private_content
            assert private[0]["namespace"] == namespace
            assert not any(tag.startswith("source-id:") for tag in private[0]["tags"])
            document = await get(owner)
            assert document["found"] and document["content"] == private_content
            assert document["source_uri"] == public_uri
            assert not any(tag.startswith("source-id:") for tag in document["tags"])
            assert "access_json" not in document
            synthesis = await synthesize(owner)
            assert synthesis["documents"] == synthesis["windows"] == 1
            assert synthesis["chunks_read"] == 2
            assert not synthesis["abstain"] and source not in json.dumps(synthesis)
            inspection = await inspect(owner)
            assert {doc["source_uri"] for doc in inspection["listed"]} == {
                "ordinary:visible",
                public_uri,
            }
            [inspected_copy] = [
                doc for doc in inspection["listed"] if doc["source_uri"] == public_uri
            ]
            assert inspected_copy["title"] == "Checked search" and inspected_copy["version"] == 10
            assert inspected_copy["ingestion_id"] == ""
            for field in ("retired_versions", "chunks", "decisions", "extraction", "jobs"):
                assert inspected_copy[field] is None
            assert source not in json.dumps(inspected_copy)
            assert {k: v for k, v in inspection.items() if k != "listed"} == {
                k: v for k, v in ordinary_inspect.items() if k != "listed"
            }
            source_inspection = await source_inspect(owner)
            assert [doc["source_uri"] for doc in source_inspection["documents"]] == [public_uri]
            assert source not in json.dumps(source_inspection["documents"])
            listed = await listing(owner)
            assert len(listed["rows"]) == 2 and listed["next_cursor"] is None
            [listed_copy] = [item for item in listed["rows"] if item["source_uri"] == public_uri]
            assert listed_copy["namespace"] == namespace and listed_copy["chunk_count"] == 2
            assert listed_copy["content_preview"] == private_content
            assert listed_copy["tags"] == [] and not listed_copy["has_blob"]
            assert len((await listing(owner, uri_prefix=["", "gdrive:"]))["rows"]) == 1
            projected = await listing(owner, fields=["title"], uri_prefix=["gdrive:"])
            assert projected["rows"] == [
                {"namespace": namespace, "source_uri": public_uri, "title": "Checked search"}
            ]
            first_page = await listing(owner, limit=1)
            assert len(first_page["rows"]) == 1 and first_page["next_cursor"]
            second_page = await listing(owner, limit=1, cursor=first_page["next_cursor"])
            assert len(second_page["rows"]) == 1 and second_page["next_cursor"] is None
            assert {first_page["rows"][0]["source_uri"], second_page["rows"][0]["source_uri"]} == {
                "ordinary:visible",
                public_uri,
            }
            sections = await outline(owner)
            assert sections["found"] and sections["source_uri"] == public_uri
            assert sections["chunk_count"] == 2 and sections["structured"]
            assert sections["section_content"] == "Unembedded section."
            assert sections["section_chunk_idxs"] == [1]
            text_download = await download(owner)
            assert text_download[0] == 200 and text_download[1] == private_content.encode()
            assert text_download[2]["x-loom-download-source"] == "text"
            original = b"Private original file bytes."
            sha = await blobs.put(org, mirror, original)
            row = {
                **row,
                "version": 11,
                "blob_path": sha,
                "blob_size": len(original),
                "blob_content_type": "text/plain",
                "original_filename": "checked.txt",
            }
            if pg.enabled():
                await pg_documents.commit([row], over={uri: 10}, targets={uri: 1})
            else:
                await ch.insert("loom.documents", [row])
            original_download = await download(owner)
            assert original_download[0] == 200 and original_download[1] == original
            assert original_download[2]["x-loom-download-source"] == "blob"
            assert source not in original_download[2]["content-disposition"]
            blob_list = await listing(owner, uri_prefix=["gdrive:"])
            assert blob_list["rows"][0]["has_blob"] and blob_list["rows"][0]["blob_size"] == len(
                original
            )
            assert await listing(outsider) == await listing(owner_bot) == ordinary_list
            assert await download(outsider) == await download(owner_bot) == missing_download
            denied = await client.post(
                "/v1/knowledge.search",
                json={
                    "org": org,
                    "namespace": namespace,
                    "query": query,
                },
                headers={"Authorization": "Bearer " + foreign},
            )
            assert denied.status_code == 403
            narrowed = {**row, "version": 12, "access_json": to_json(AccessList())}
            if pg.enabled():
                await pg_documents.commit([narrowed], over={uri: 11}, targets={uri: 1})
            else:
                await ch.insert("loom.documents", [narrowed])
            assert await search(owner) == baseline
            assert await listing(owner) == ordinary_list
            assert await inspect(owner) == ordinary_inspect
            assert await source_inspect(owner) == empty_source_inspect
            assert await get(owner) == missing_get
            assert await synthesize(owner) == missing_synthesis
            assert await outline(owner) == missing_outline
            assert await download(owner) == missing_download
            widened = {**row, "version": 13, "access_json": to_json(AccessList(anyone=True))}
            if pg.enabled():
                await pg_documents.commit([widened], over={uri: 12}, targets={uri: 1})
            else:
                await ch.insert("loom.documents", [widened])
            assert public_uri in [r["source_uri"] for r in (await search(owner_bot))["results"]]
            assert (await get(owner_bot))["found"]
            assert (await synthesize(owner_bot))["documents"] == 1
            assert (await download(owner_bot))[:2] == (200, original)
            assert (await outline(owner_bot))["section_content"] == "Unembedded section."
            assert len((await listing(owner_bot))["rows"]) == 2
            assert len((await inspect(owner_bot))["listed"]) == 2
            assert len((await source_inspect(owner_bot))["documents"]) == 1
            # An anyone-readable ACL cannot widen an answer's pre-output admission.
            for promise, admission in [(True, False), (False, True), (True, True)]:
                answer = mint_token(
                    "source-owner",
                    org,
                    [namespace],
                    ["viewer"],
                    ttl_seconds=600,
                    is_service_account=False,
                    session_id=str(uuid4()),
                    consumer="loom-assistant",
                    jti="assistant-answer-" + str(uuid4()),
                    session_protection=promise,
                    answer_mirrors=admission,
                )
                found = await search(answer)
                addresses = [r["source_uri"] for r in found["results"]]
                assert "ordinary:visible" in addresses
                document = await get(answer)
                if promise and admission:
                    assert public_uri in addresses and document["content"] == private_content
                    assert found["mirrored"] is True and document["mirrored"] is True
                else:
                    assert public_uri not in addresses and not document["found"]
                    assert private_content not in json.dumps(found)
                    assert found.get("mirrored") is not True
                    assert document.get("mirrored") is not True
            await sources.insert_source(replace(registration, active=False))
            inactive_responses = (
                await listing(owner),
                await search(owner),
                await get(owner),
                await synthesize(owner),
                await outline(owner),
            )
            assert inactive_responses == inactive_baselines
            assert await inspect(owner) == inactive_inspect_baseline
            assert await download(owner) == inactive_download_baseline
            await sources.insert_source(registration)
            assert (await get(owner))["found"]
            stale = now - timedelta(seconds=settings.knowledge_source_acl_stale_seconds + 1)
            await mirrors.record_listing(org, mirror, source, "checked", stale)
            assert await listing(owner) == ordinary_list
            assert await inspect(owner) == ordinary_inspect
            assert await source_inspect(owner) == empty_source_inspect
            assert await search(owner) == baseline
            assert await get(owner) == missing_get
            assert await synthesize(owner) == missing_synthesis
            assert await outline(owner) == missing_outline
            assert await download(owner) == missing_download
        finally:
            try:
                await ch.disconnect()
            finally:
                await pg.close_pool()
