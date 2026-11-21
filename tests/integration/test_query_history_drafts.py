"""Seed a private query log, queue one normalized draft, and approve it through HTTP."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.integration._resources import ch_client_from_env, ch_target, unique_org


@pytest_asyncio.fixture
async def history_store() -> AsyncIterator[Any]:
    from server.core.ch import get_client

    client = get_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


@pytest.fixture
def query_log_source(ch_client: Any) -> Any:
    suffix = uuid.uuid4().hex[:10]
    database, reader = f"gaps_metrics_{suffix}", f"gaps_log_{suffix}"
    users = [f"gaps_runner_{suffix}_{i}" for i in range(3)]
    password = uuid.uuid4().hex
    try:
        ch_client.command(f"CREATE DATABASE {database}")
        ch_client.command(
            f"CREATE TABLE {database}.sales (amount UInt64, segment String) ENGINE = Memory"
        )
        ch_client.command(f"INSERT INTO {database}.sales VALUES (12, 'private-literal')")
        for table in ("metric_101", "metric_202"):
            ch_client.command(f"CREATE TABLE {database}.{table} (amount UInt64) ENGINE = Memory")
        for index, user in enumerate(users):
            ch_client.command(
                f"CREATE USER {user} IDENTIFIED BY '{password}' SETTINGS readonly = 2"
            )
            ch_client.command(f"GRANT SELECT ON {database}.* TO {user}")
            ch_client.command(f"GRANT SELECT ON system.one TO {user}")
            client = ch_client_from_env(username=user, password=password, database=database)
            try:
                spacing = (
                    "segment='private-literal'",
                    "segment = 'private-literal'",
                    "segment= 'private-literal'",
                )[index]
                client.query(
                    f"SELECT sum(amount) AS revenue /* private-comment */ FROM {database}.sales WHERE {spacing}"
                )
                # Numbered table names share normalized_query_hash but hold different metrics.
                numbered = "metric_101" if index < 2 else "metric_202"
                client.query(f"SELECT sum(amount) FROM {database}.{numbered}")
                # This group's two accounts fall below the privacy threshold.
                if index < 2:
                    client.query(f"SELECT count() FROM {database}.sales")
                # A query touching a database outside the registration is excluded.
                client.query(f"SELECT sum(amount) FROM {database}.sales CROSS JOIN system.one")
            finally:
                client.close()
        ch_client.command(f"CREATE USER {reader} IDENTIFIED BY '{password}' SETTINGS readonly = 2")
        ch_client.command(f"GRANT SELECT ON system.query_log TO {reader}")
        ch_client.command("SYSTEM FLUSH LOGS")
        target = ch_target()
        yield {
            "name": "metrics",
            "host": target["host"],
            "port": target["port"],
            "secure": target.get("secure", False),
            "user": reader,
            "password": password,
            "databases": [database],
        }
    finally:
        for user in [reader, *users]:
            ch_client.command(f"DROP USER IF EXISTS {user}")
        ch_client.command(f"DROP DATABASE IF EXISTS {database} SYNC")


@pytest.mark.asyncio
async def test_query_log_to_private_draft_to_governed_definition(
    control_plane_url: str,
    ch_client: Any,
    mint_test_token: Any,
    history_store: Any,
    query_log_source: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_query_history import (
        QueryHistoryAddInput,
        QueryHistoryRemoveInput,
    )
    from server.knowledge.app.query_history_sources import handle_add, handle_remove
    from server.knowledge.store import query_history as sources
    from server.knowledge.store.query_history_remote import read_groups
    from server.knowledge.workers.query_history import _source
    from server.llm import client as llm_client
    from server.llm.fake import FakeLLMClient
    from server.memory.store import learnings
    from server.settings import settings

    org, namespace = unique_org("query-history"), "metrics"
    monkeypatch.setattr(settings, "knowledge_query_history_enabled", True)
    monkeypatch.setattr(settings, "tracker_self_service_allow_private_hosts", True)
    monkeypatch.setattr(llm_client, "get_llm_client", FakeLLMClient)
    from server.core.errors import ValidationError

    reader = query_log_source["user"]
    database = query_log_source["databases"][0]
    ch_client.command(f"GRANT INSERT ON {database}.* TO {reader}")
    try:
        with pytest.raises(ValidationError, match="read-only"):
            await handle_add(QueryHistoryAddInput(org=org, namespace=namespace, **query_log_source))
        assert await sources.find(org, namespace, "metrics") is None
    finally:
        ch_client.command(f"REVOKE INSERT ON {database}.* FROM {reader}")
    added = await handle_add(QueryHistoryAddInput(org=org, namespace=namespace, **query_log_source))
    assert added.kind == "clickhouse_queries" and "password" not in added.model_dump()
    source = await sources.find(org, namespace, "metrics")
    assert source is not None and source["password_enc"] != query_log_source["password"]
    groups = await read_groups(source)
    assert len(groups) == 1 and groups[0]["users"] == 3
    sql = groups[0]["sql"]
    assert "private-literal" not in sql and "private-comment" not in sql and "?" in sql
    assert query_log_source["databases"][0] in sql
    assert await _source(source, time.monotonic() + 60) == 1
    assert await _source(source, time.monotonic() + 60) == 0
    pending = await learnings.list_by_status(org, namespace, "pending", limit=10)
    assert len(pending) == 1 and pending[0]["source_kind"] == "query_history"
    proposal = json.loads(pending[0]["proposed_value"])
    assert proposal["rests_on"]["sql"] == sql
    admin = mint_test_token(org=org, namespaces=[namespace], roles=["admin"], subject="reviewer")
    viewer = mint_test_token(org=org, namespaces=[namespace], roles=["viewer"], subject="reader")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as http:

        async def call(op: str, token: str, **args: Any) -> httpx.Response:
            return await http.post(
                f"/v1/{op}",
                json={"org": org, "namespace": namespace, **args},
                headers={"Authorization": f"Bearer {token}"},
            )

        hidden = await call("learnings.list", viewer)
        assert hidden.status_code == 200 and hidden.json()["rows"] == []
        denied = await call("knowledge.query_history.list", viewer)
        assert denied.status_code == 403
        before = await call("memory.definitions.list", admin)
        assert before.status_code == 200 and not before.json()["definitions"]
        approved = await call(
            "learnings.review",
            admin,
            candidate_id=str(pending[0]["candidate_id"]),
            decision="promote",
            approver="forged",
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["promoted_to_shared"]
        after = await call("memory.definitions.list", admin)
        assert after.status_code == 200, after.text
        assert (
            len(after.json()["definitions"]) == 1
            and after.json()["definitions"][0]["verdict"] == "governed"
        )
        repeated = await call(
            "learnings.review",
            admin,
            candidate_id=str(pending[0]["candidate_id"]),
            decision="promote",
        )
        assert repeated.status_code == 200 and not repeated.json()["promoted_to_shared"]
        definition = after.json()["definitions"][0]
        lineage = await call(
            "memory.lineage", admin, agent="-", memory_key=definition["memory_key"]
        )
        assert lineage.status_code == 200, lineage.text
        assert any(
            event["event_type"] == "memory.promote" for event in lineage.json()["audit_chain"]
        )
        saved = await call("memory.get", admin, agent="-", memory_key=definition["memory_key"])
        assert saved.status_code == 200, saved.text
        old = saved.json()
        changed = {**old["value"], "body": "A revised definition."}
        edited = await call(
            "memory.set",
            admin,
            agent="-",
            memory_key=definition["memory_key"],
            memory_type="definition",
            value=changed,
        )
        assert edited.status_code == 200, edited.text
        redacted = await call(
            "memory.redact",
            admin,
            memory_id=old["memory_id"],
            version=old["version"],
            reason="remove draft copy",
        )
        assert redacted.status_code == 200 and redacted.json()["redacted"], redacted.text
        erased = await learnings.find_candidate(org, namespace, str(pending[0]["candidate_id"]))
        assert erased is not None and erased["proposed_value"] == ""
        refused = await call(
            "learnings.review",
            admin,
            candidate_id=str(pending[0]["candidate_id"]),
            decision="promote",
        )
        assert refused.status_code == 404
        current = await call("memory.get", admin, agent="-", memory_key=definition["memory_key"])
        assert current.json()["value"] == changed
    audit = await history_store.query_one(
        "SELECT actor, JSONExtractString(subject, 'promotion_event') AS event "
        "FROM loom.audit_log WHERE org = {org:String} AND namespace = {ns:String} "
        "AND event_type = 'memory.promote' LIMIT 1",
        params={"org": org, "ns": namespace},
    )
    assert audit is not None and "reviewer" in audit["actor"] and audit["event"]
    removed = await handle_remove(
        QueryHistoryRemoveInput(org=org, namespace=namespace, name="metrics")
    )
    assert removed.removed
    assert await _source(source, time.monotonic() + 60) == 0
