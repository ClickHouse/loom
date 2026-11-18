"""Checked-leg metadata reads keep org, source and current-head boundaries."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.integration._resources import unique_org


async def test_group_snapshot_readback_and_erasure_are_scoped() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.erasure.store.steps_ledgers import delete_group_members_by_namespace
    from server.knowledge.store import source_access as store

    ch = get_client()
    set_query_tag("test.source_access_group_store")
    org, other_org = unique_org("access-groups"), unique_org("other-access-groups")
    mirror, other_mirror, sid = new_mirror_name(), new_mirror_name(), str(uuid4())
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    row = {
        "org": org,
        "namespace": mirror,
        "source_id": sid,
        "group": "team@example.com",
        "name": "documents",
        "members": ["lee@example.com"],
        "read_at": now,
        "deleted": False,
        "version": 1,
    }
    try:
        await ch.connect()
        await ch.insert(
            "loom.knowledge_group_members",
            [row, {**row, "org": other_org}, {**row, "namespace": other_mirror}],
        )
        saved = await ch.query_all(
            "SELECT * FROM loom.knowledge_group_members FINAL WHERE org = {org:String} AND namespace = {ns:String}",
            {"org": org, "ns": mirror},
        )
        assert saved == [row]
        selected = await store.group_members(org, [(mirror, sid, "team@example.com")])
        assert selected == [
            {
                k: row[k]
                for k in ("namespace", "source_id", "group", "members", "read_at", "deleted")
            }
        ]
        assert await store.group_members(org, [(mirror, str(uuid4()), "team@example.com")]) == []
        await delete_group_members_by_namespace(org, mirror)
        assert await store.group_members(org, [(mirror, sid, "team@example.com")]) == []
        assert await store.group_members(other_org, [(mirror, sid, "team@example.com")]) == selected
        assert await store.group_members(org, [(other_mirror, sid, "team@example.com")]) == [
            {**selected[0], "namespace": other_mirror}
        ]
    finally:
        await ch.disconnect()


async def test_chunk_hydration_reads_only_its_exact_live_admitted_key() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    import json

    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.store import source_candidates as store

    ch = get_client()
    set_query_tag("test.source_chunk_hydration")
    org, mirror, uri = (
        unique_org("access-hydrate"),
        new_mirror_name(),
        "gdrive:file#" + str(uuid4()),
    )
    model = "synthetic-3072"
    row = {
        "org": org,
        "namespace": mirror,
        "source_uri": uri,
        "chunk_idx": 0,
        "doc_version": 10,
        "doc_superseded_at": None,
        "chunk_metadata": json.dumps({"revision": 0}),
        "content": "exact admitted content",
        "embedding_model": model,
        "embedding_dim": 3072,
        "embedding": [1.0] + [0.0] * 3071,
    }
    alternatives = [
        {"org": unique_org("other-access-hydrate")},
        {"namespace": new_mirror_name()},
        {"source_uri": uri + "other"},
        {"chunk_idx": 1},
        {"doc_version": 11},
        {"chunk_metadata": json.dumps({"revision": 1})},
        {"embedding_model": "different-model"},
        {"embedding_dim": 0},
        {"doc_superseded_at": datetime.now(UTC)},
    ]
    # The same exact row with a superseded marker is also excluded.
    try:
        await ch.connect()
        await ch.insert(
            "loom.chunks",
            [row, *[{**row, "content": "not admitted", **change} for change in alternatives]],
        )
        key = (mirror, uri, 0, 10, 0)
        selected = await store.chunks(org, model, [key])
        assert len(selected) == 1 and selected[0]["content"] == "exact admitted content"
        assert (
            tuple(
                selected[0][field]
                for field in ("namespace", "source_uri", "chunk_idx", "doc_version", "revision")
            )
            == key
        )
        await ch.insert(
            "loom.chunk_supersessions",
            [
                {
                    "org": org,
                    "namespace": mirror,
                    "source_uri": uri,
                    "embedding_model": model,
                    "doc_version": 10,
                    "superseded_at": datetime.now(UTC),
                }
            ],
        )
        assert await store.chunks(org, model, [key]) == []
    finally:
        await ch.disconnect()


async def test_pg_head_metadata_has_current_acl_and_recorded_chunks() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import pg

    if not pg.enabled():
        pytest.skip("Postgres authority not selected")
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.domain.access_list import AccessList, to_json
    from server.knowledge.store import pg_documents
    from server.knowledge.store import source_access as store

    ch = get_client()
    set_query_tag("test.source_access_head_store")
    org, mirror, sid = unique_org("access-head"), new_mirror_name(), str(uuid4())
    uri = "gdrive:file#" + sid
    row = {
        "org": org,
        "namespace": mirror,
        "source_uri": uri,
        "doc_id": str(uuid4()),
        "content": "must not be fetched before admission",
        "title": "checked title",
        "metadata": "{}",
        "description": "not candidate metadata",
        "tags": ["source-id:" + sid],
        "version": 10,
        "revision": 0,
        "superseded_at": None,
        "ingested_at": datetime.now(UTC).replace(microsecond=0),
        "access_json": to_json(AccessList(emails=("lee@example.com",))),
    }
    try:
        await ch.connect()
        await pg_documents.commit([row], over={}, targets={uri: 1}, chunked={uri})
        narrowed = {**row, "version": 11, "access_json": to_json(AccessList())}
        await pg_documents.commit([narrowed], over={uri: 10}, targets={uri: 1})
        got = await store.heads(org, [(mirror, uri)])
        head = got[(mirror, uri)]
        assert head.serving.revision == head.serving.chunks_revision == 0
        assert head.serving.chunks_version == 10 and not head.serving.deleted
        assert head.row["version"] == 11 and head.row["access_json"] == narrowed["access_json"]
        assert not {"content", "description", "metadata"}.intersection(head.row)
        assert await store.heads(unique_org("other-access-head"), [(mirror, uri)]) == {}
        assert await store.heads(org, [(mirror, uri + "missing")]) == {}
    finally:
        try:
            await ch.disconnect()
        finally:
            await pg.close_pool()


async def test_coarse_access_keys_do_not_drop_strictly_admitted_permissions() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    import json

    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.domain.access_list import allows, candidate_keys, from_json

    ch = get_client()
    set_query_tag("test.source_access_candidate_keys")
    org, mirror = unique_org("access-key"), new_mirror_name()
    permissions = [
        {"emails": ["  Lee@EXAMPLE.COM  "]},
        {"emails": ["\x1cLee@example.com\x1f"]},
        {"emails": ["\u2003Lee@example.com\u00a0"]},
        {"emails": ["K@example.com"]},
        {"domains": ["  EXAMPLE.COM  "]},
        {"domains": ["\u2003EXAMPLE.COM"]},
        {"owner": "alias-for-reader"},
        {"groups": ["group@example.com"]},
        {"anyone": True},
    ]
    rows = [
        {
            "org": org,
            "namespace": mirror,
            "source_uri": str(i),
            "doc_id": str(uuid4()),
            "access_json": json.dumps({"unread": False, **access}),
            "version": 1,
        }
        for i, access in enumerate(permissions)
    ]
    keys = candidate_keys(
        subject="reader",
        emails=frozenset({"lee@example.com", "k@example.com"}),
        domains=frozenset({"example.com"}),
    )
    assert all(
        allows(
            from_json(row["access_json"]),
            subject="reader",
            canonical_owner="reader",
            emails=frozenset({"lee@example.com", "k@example.com"}),
            domains=frozenset({"example.com"}),
            groups=frozenset({"group@example.com"}),
        )
        for row in rows
    )
    try:
        await ch.connect()
        await ch.insert("loom.documents", rows)
        admitted = await ch.query_all(
            "SELECT source_uri FROM loom.documents FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} AND hasAny(access_keys, {keys:Array(String)}) "
            "ORDER BY source_uri LIMIT 100",
            {"org": org, "ns": mirror, "keys": keys},
        )
        assert admitted == [{"source_uri": str(i)} for i in range(len(permissions))]
        anyone = await ch.query_all(
            "SELECT source_uri FROM loom.documents FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} AND hasAny(access_keys, ['anyone']) LIMIT 100",
            {"org": org, "ns": mirror},
        )
        assert anyone == [{"source_uri": "8"}]
    finally:
        await ch.disconnect()


async def test_both_chunk_frequency_views_exclude_private_namespaces() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name

    ch = get_client()
    set_query_tag("test.source_access_term_frequency")
    org, mirror = unique_org("access-freq"), new_mirror_name()
    row = {
        "org": org,
        "namespace": mirror,
        "source_uri": "gdrive:synthetic",
        "chunk_idx": 0,
        "content": "privatetexttoken",
        "context": "privatecontexttoken",
        "doc_version": 1,
        "embedding": [1.0] + [0.0] * 3071,
        "embedding_model": "synthetic-3072",
        "embedding_dim": 3072,
    }
    try:
        await ch.connect()
        await ch.insert("loom.chunks", [row, {**row, "namespace": "docs"}])
        found = await ch.query_all(
            "SELECT namespace, term, uniqExactMerge(df_state) AS df "
            "FROM loom.chunk_term_freq WHERE org = {org:String} "
            "GROUP BY namespace, term ORDER BY namespace, term LIMIT 100",
            {"org": org},
        )
        assert found == [
            {"namespace": "docs", "term": "privatecontexttoken", "df": 1},
            {"namespace": "docs", "term": "privatetexttoken", "df": 1},
        ]
        from server.memory.store.term_freq import rebuild

        await rebuild(ch, org=org, namespace="docs", planes=("knowledge",))
        ordinary = await ch.query_all(
            "SELECT namespace, term, uniqExactMerge(df_state) AS df "
            "FROM loom.chunk_term_freq WHERE org = {org:String} "
            "GROUP BY namespace, term ORDER BY namespace, term LIMIT 100",
            {"org": org},
        )
        assert ordinary == found
        await rebuild(ch, org=org, namespace=mirror, planes=("knowledge",))
        rebuilt = await ch.query_all(
            "SELECT term FROM loom.chunk_term_freq WHERE org = {org:String} "
            "AND namespace = {ns:String} LIMIT 100",
            {"org": org, "ns": mirror},
        )
        assert rebuilt == []
    finally:
        await ch.disconnect()


@pytest.mark.parametrize("leg", ["semantic", "lexical", "keys"])
async def test_discovery_stream_projects_metadata_and_only_ch_uses_acl_keys(leg) -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    import json

    from server.core import pg
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.app.search import HybridSearchQuery, _common_where
    from server.knowledge.domain.access_list import AccessList, to_json
    from server.knowledge.store import source_candidates as store

    ch = get_client()
    set_query_tag("test.source_candidate_stream")
    org, mirror, sid = unique_org("access-stream"), new_mirror_name(), str(uuid4())
    uris = ["gdrive:allowed#" + sid, "gdrive:denied#" + sid]
    vector = [1.0] + [0.0] * 3071
    docs = [
        {
            "org": org,
            "namespace": mirror,
            "source_uri": uri,
            "doc_id": str(uuid4()),
            "version": 1,
            "access_json": to_json(AccessList(anyone=(i == 0))),
        }
        for i, uri in enumerate(uris)
    ]
    chunks = [
        {
            "org": org,
            "namespace": mirror,
            "source_uri": uri,
            "chunk_idx": 0,
            "content": "candidateonlytoken",
            "context": "private content context",
            "doc_version": 1,
            "chunk_metadata": json.dumps({"revision": 0}),
            "tags": ["source-id:" + sid],
            "embedding": vector,
            "embedding_model": "synthetic-3072",
            "embedding_dim": 3072,
            "keys": ["object"],
        }
        for uri in uris
    ]
    try:
        await ch.connect()
        await ch.insert("loom.documents", docs)
        await ch.insert("loom.chunks", chunks)
        where, params = _common_where(
            HybridSearchQuery(org, (mirror,), "synthetic-3072", vector, 1)
        )
        params.update(toks=["candidateonlytoken"], keys=["object"])
        sql, params = store.statement(
            where, params, leg=leg, access_keys=None if pg.enabled() else ["anyone"]
        )
        async with store.candidates(sql, params) as stream:
            rows = [row async for row in stream]
        assert {r["source_uri"] for r in rows} == set(uris if pg.enabled() else uris[:1])
        assert all(
            set(r)
            == {
                "namespace",
                "source_uri",
                "chunk_idx",
                "doc_version",
                "revision",
                "distance",
                "lexical",
                "matched",
            }
            for r in rows
        )
        assert all(int(r["doc_version"]) == 1 and int(r["revision"]) == 0 for r in rows)
    finally:
        await ch.disconnect()
