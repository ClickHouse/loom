"""Default memory reads exclude rejected and out-of-validity neighbours."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from server.core.ch import CHClient
from server.core.datetimes import naive_utc_now
from server.memory.app.retrieval.summaries import fetch_session_summaries
from server.memory.store import lifecycle
from server.memory.store.lifecycle import bulk_tombstone_scope
from server.memory.store.rows import MEMORY_PERSISTED_COLS, next_version
from tests.integration._resources import minted_identity


def _memory(ch: Any, me: dict[str, str], key: str, **fields: Any) -> None:
    row = {
        "org": me["org"],
        "namespace": me["namespace"],
        "agent": "writer",
        "user_id": "reader",
        "session_id": "session",
        "memory_key": key,
        "value": json.dumps(key),
        "files": ["src/read.py"],
        **fields,
    }
    ch.insert("loom.memory", [list(row.values())], column_names=list(row))


@pytest.mark.parametrize("sort_by", ["recency", "score"])
async def test_default_listing_and_counts_exclude_rejected_rows(
    control_plane_url: str, mint_test_token: Any, ch_client: Any, sort_by: str
) -> None:
    me = minted_identity(mint_test_token, "list-rejected")
    for status in ("active", "conflicted", "rejected", "proposed", "superseded", "duplicate"):
        _memory(ch_client, me, status, status=status)
    body = {
        "org": me["org"],
        "namespace": me["namespace"],
        "agent": "*",
        "user_id": "reader",
        "session_id": "*",
        "sort_by": sort_by,
    }
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {me['token']}"},
        timeout=30,
    ) as client:
        response = await client.post("/v1/memory.list", json=body)
        assert response.status_code == 200, response.text
        out = response.json()
        assert {r["memory_key"] for r in out["entries"]} == {"active", "conflicted"}, out
        if sort_by == "recency":
            assert out["total"] == 2, out
            response = await client.post("/v1/memory.list", json={**body, "count_only": True})
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 2


async def test_file_context_excludes_a_fact_past_its_validity_end(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    me = minted_identity(mint_test_token, "file-validity")
    now = naive_utc_now()
    _memory(ch_client, me, "past", valid_to=now - timedelta(days=1))
    _memory(ch_client, me, "future", valid_to=now + timedelta(days=1))
    _memory(ch_client, me, "open")
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {me['token']}"},
        timeout=30,
    ) as client:
        response = await client.post(
            "/v1/memory.for_file",
            json={"org": me["org"], "namespace": me["namespace"], "path": "src/read.py"},
        )
    assert response.status_code == 200, response.text
    out = response.json()
    assert {r["memory_key"] for r in out["hits"]} == {"future", "open"}, out


async def test_session_summaries_inherit_known_source_state_and_version(
    mint_test_token: Any, ch_client: Any
) -> None:
    me = minted_identity(mint_test_token, "summary-sources")
    vector = [1.0] + [0.0] * 3071
    states = ["active", "conflicted", "rejected", "proposed", "superseded", "tombstoned"]
    for status in [*states, "rewritten", "high", "expired", "past", "standalone", "missing"]:
        fields: dict[str, Any] = {"version": 100, "session_id": status}
        if status in states:
            fields["status"] = status
        if status == "tombstoned":
            fields["tombstoned_at"] = naive_utc_now()
        if status == "rewritten":
            fields["version"] = 300
        if status == "high":
            fields["sensitivity"] = "high"
        if status in ("past", "expired"):
            fields["valid_to" if status == "past" else "expires_at"] = naive_utc_now() - timedelta(
                days=1
            )
        if status not in ("standalone", "missing"):
            _memory(ch_client, me, "shared-key", **fields)
        row = {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "writer",
            "user_id": "reader",
            "session_id": status,
            "version": 200,
            "summary_text": status,
            "summary_embedding": vector,
            "embedding_model": "fixture-model",
            "embedding_dim": len(vector),
            "derived_from": ["shared-key"] if status != "standalone" else [],
        }
        ch_client.insert("loom.session_summaries", [list(row.values())], column_names=list(row))
    # The same key under another user must neither suppress nor revive this user's digest.
    _memory(ch_client, me, "shared-key", session_id="active", user_id="outsider", status="rejected")
    _memory(ch_client, me, "shared-key", session_id="rejected", user_id="outsider", status="active")
    ch = CHClient()
    await ch.connect()
    try:
        hits = await fetch_session_summaries(
            ch,
            org=me["org"],
            namespace=me["namespace"],
            agent="writer",
            user_id="reader",
            query_vec=vector,
            embedding_model="fixture-model",
            n=20,
        )
    finally:
        await ch.disconnect()
    assert {h.content_excerpt for h in hits} == {"active", "conflicted", "standalone"}


async def test_clear_keeps_a_complete_tombstone_in_version_history(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    me = minted_identity(mint_test_token, "clear-history")
    me["token"] = mint_test_token(org=me["org"], namespaces=["*"], roles=["admin"])
    _memory(ch_client, me, "clear-me", version=100, tags=["retained"], actor="human:writer")
    _memory(ch_client, me, "locked", version=100, lock_mode="read_only", locked_by="owner")
    _memory(ch_client, me, "outside", version=100, user_id="other")
    query = (
        f"SELECT {', '.join(MEMORY_PERSISTED_COLS)} FROM loom.memory_versions FINAL "
        "WHERE org = {org:String} AND namespace = {namespace:String} "
        "AND memory_key = 'clear-me' ORDER BY version"
    )
    before = list(ch_client.query(query, parameters=me).named_results())
    assert len(before) == 1
    async with httpx.AsyncClient(
        base_url=control_plane_url,
        headers={"Authorization": f"Bearer {me['token']}"},
        timeout=30,
    ) as client:
        body = {
            "org": me["org"],
            "namespace": me["namespace"],
            "user_id": "reader",
            "confirm_subject": "reader",
        }
        response = await client.post("/v1/memory.clear", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["locked_skipped"] == 1
        after = list(ch_client.query(query, parameters=me).named_results())
        assert len(after) == 2, after
        original, tombstone = after
        assert original == before[0]
        assert tombstone["version"] > original["version"]
        assert tombstone["tombstoned_at"] is not None
        changed = {"version", "tombstoned_at", "status", "updated_at", "expires_at"}
        assert {k: v for k, v in tombstone.items() if k not in changed} == {
            k: v for k, v in original.items() if k not in changed
        }
        response = await client.post("/v1/memory.clear", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["rows_affected"]["loom.memory"] == 0
        assert list(ch_client.query(query, parameters=me).named_results()) == after
    live = ch_client.query(
        "SELECT memory_key FROM loom.memory FINAL WHERE org = {org:String} "
        "AND namespace = {namespace:String} AND tombstoned_at IS NULL",
        parameters=me,
    ).result_rows
    assert {row[0] for row in live} == {"locked", "outside"}


@pytest.mark.parametrize("when", ["before", "after"])
async def test_clear_yields_to_a_write_above_its_cutoff(
    mint_test_token: Any, ch_client: Any, monkeypatch: Any, when: str
) -> None:
    me = minted_identity(mint_test_token, "clear-concurrent")
    _memory(ch_client, me, "shared-key", version=100)
    ch = CHClient()
    await ch.connect()
    read = ch.query_all
    advanced = False

    def write_new_value(version: int) -> None:
        _memory(ch_client, me, "shared-key", version=version, value=json.dumps("new value"))
        vector = [1.0] + [0.0] * 3071
        row = {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "writer",
            "user_id": "reader",
            "session_id": "session",
            "memory_key": "shared-key",
            "version": version,
            "embedding_model": "fixture-model",
            "embedding_dim": 3072,
            "embedding": vector,
            "memory_key_embedding": vector,
        }
        ch_client.insert("loom.memory_embeddings", [list(row.values())], column_names=list(row))

    async def advance(sql: str, params: Any = None) -> list[dict[str, Any]]:
        nonlocal advanced
        if "SELECT * FROM loom.memory FINAL" in sql and not advanced and when == "before":
            advanced = True
            write_new_value(params["clear_version"] + 100)
        rows = await read(sql, params=params)
        if "SELECT * FROM loom.memory FINAL" in sql and rows and not advanced and when == "after":
            advanced = True
            write_new_value(next_version())
        return rows

    monkeypatch.setattr(ch, "query_all", advance)
    announced: list[Any] = []

    async def announce(rows: list[dict[str, Any]]) -> None:
        announced.extend(rows)

    try:
        counts = await bulk_tombstone_scope(
            org=me["org"],
            namespace=me["namespace"],
            user_id="reader",
            ch=ch,
            on_tombstoned=announce,
        )
        memory = ch_client.query(
            "SELECT value, tombstoned_at FROM loom.memory FINAL WHERE org = {org:String} "
            "AND namespace = {namespace:String}",
            parameters=me,
        ).result_rows
        vectors = ch_client.query(
            "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {org:String} "
            "AND namespace = {namespace:String} AND tombstoned_at IS NULL",
            parameters=me,
        ).result_rows
    finally:
        await ch.disconnect()
    assert advanced
    assert memory == [(json.dumps("new value"), None)]
    assert vectors == [(1,)]
    assert counts["loom.memory"] == 0
    assert announced == []


async def test_clear_pages_heads_and_removes_orphan_vectors(
    mint_test_token: Any, ch_client: Any, monkeypatch: Any
) -> None:
    me = minted_identity(mint_test_token, "clear-pages")
    monkeypatch.setattr(lifecycle, "CLEAR_PAGE_ROWS", 2)
    keys = [f"row-{i}" for i in range(5)]
    for key in keys:
        _memory(ch_client, me, key, version=100)
    _memory(ch_client, me, "locked", version=100, lock_mode="read_only")
    vector = [1.0] + [0.0] * 3071
    for key in [*keys, "orphan", "locked"]:
        row = {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "writer",
            "user_id": "reader",
            "session_id": "session",
            "memory_key": key,
            "version": 100,
            "embedding_model": "fixture-model",
            "embedding_dim": 3072,
            "embedding": vector,
            "memory_key_embedding": vector,
        }
        ch_client.insert("loom.memory_embeddings", [list(row.values())], column_names=list(row))
    pages: list[list[str]] = []

    async def announce(rows: list[dict[str, Any]]) -> None:
        pages.append([r["memory_key"] for r in rows])

    ch = CHClient()
    await ch.connect()
    try:
        counts = await bulk_tombstone_scope(
            org=me["org"],
            namespace=me["namespace"],
            user_id="reader",
            ch=ch,
            on_tombstoned=announce,
        )
    finally:
        await ch.disconnect()
    assert counts == {"loom.memory": 5, "loom.memory_embeddings": 6, "locked_skipped": 1}
    assert pages == [keys[:2], keys[2:4], keys[4:]]
    live = ch_client.query(
        "SELECT memory_key FROM loom.memory_embeddings FINAL WHERE org = {org:String} "
        "AND namespace = {namespace:String} AND tombstoned_at IS NULL",
        parameters=me,
    ).result_rows
    assert live == [("locked",)]


async def test_clear_includes_a_visible_head_from_an_ahead_writer_clock(
    mint_test_token: Any, ch_client: Any, monkeypatch: Any
) -> None:
    me = minted_identity(mint_test_token, "clear-clock")
    _memory(ch_client, me, "ahead", version=200)
    monkeypatch.setattr(lifecycle, "next_version", lambda: 100)
    ch = CHClient()
    await ch.connect()
    try:
        counts = await bulk_tombstone_scope(
            org=me["org"], namespace=me["namespace"], user_id="reader", ch=ch
        )
    finally:
        await ch.disconnect()
    assert counts["loom.memory"] == 1
    rows = ch_client.query(
        "SELECT version, tombstoned_at IS NOT NULL FROM loom.memory FINAL "
        "WHERE org = {org:String} AND namespace = {namespace:String}",
        parameters=me,
    ).result_rows
    assert rows == [(202, 1)]


async def test_a_lock_that_overtakes_clear_keeps_its_old_vector(
    mint_test_token: Any, ch_client: Any, monkeypatch: Any
) -> None:
    me = minted_identity(mint_test_token, "clear-lock-race")
    _memory(ch_client, me, "target", version=100)
    _memory(ch_client, me, "locked", version=100, lock_mode="read_only")
    vector = [1.0] + [0.0] * 3071
    row = {
        "org": me["org"],
        "namespace": me["namespace"],
        "agent": "writer",
        "user_id": "reader",
        "session_id": "session",
        "memory_key": "target",
        "version": 100,
        "embedding_model": "fixture-model",
        "embedding_dim": 3072,
        "embedding": vector,
        "memory_key_embedding": vector,
    }
    ch_client.insert("loom.memory_embeddings", [list(row.values())], column_names=list(row))
    ch = CHClient()
    await ch.connect()
    read = ch.query_all
    locked = False

    async def lock_after_read(sql: str, params: Any = None) -> list[dict[str, Any]]:
        nonlocal locked
        rows = await read(sql, params=params)
        if "SELECT * FROM loom.memory FINAL" in sql and rows and not locked:
            locked = True
            _memory(
                ch_client,
                me,
                "target",
                version=params["clear_version"] + 100,
                lock_mode="read_only",
            )
        return rows

    monkeypatch.setattr(ch, "query_all", lock_after_read)
    try:
        counts = await bulk_tombstone_scope(
            org=me["org"], namespace=me["namespace"], user_id="reader", ch=ch
        )
    finally:
        await ch.disconnect()
    assert locked
    assert counts == {"loom.memory": 0, "loom.memory_embeddings": 0, "locked_skipped": 1}
    rows = ch_client.query(
        "SELECT memory_key FROM loom.memory_embeddings FINAL WHERE org = {org:String} "
        "AND namespace = {namespace:String} AND tombstoned_at IS NULL",
        parameters=me,
    ).result_rows
    assert rows == [("target",)]


async def test_nearest_stale_summary_does_not_hide_the_next_valid_digest(
    mint_test_token: Any, ch_client: Any
) -> None:
    me = minted_identity(mint_test_token, "summary-next")
    query = [1.0] + [0.0] * 3071
    for session, status, vector in (
        ("stale", "rejected", query),
        ("fresh", "active", [0.7, 0.2] + [0.0] * 3070),
    ):
        _memory(ch_client, me, "source", session_id=session, status=status, version=100)
        row = {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "writer",
            "user_id": "reader",
            "session_id": session,
            "version": 200,
            "summary_text": session,
            "summary_embedding": vector,
            "embedding_dim": 3072,
            "embedding_model": "fixture-model",
            "derived_from": ["source"],
        }
        ch_client.insert("loom.session_summaries", [list(row.values())], column_names=list(row))
    ch = CHClient()
    await ch.connect()
    try:
        hits = await fetch_session_summaries(
            ch,
            org=me["org"],
            namespace=me["namespace"],
            agent="writer",
            user_id="reader",
            query_vec=query,
            embedding_model="fixture-model",
            n=1,
        )
    finally:
        await ch.disconnect()
    assert [h.content_excerpt for h in hits] == ["fresh"]
