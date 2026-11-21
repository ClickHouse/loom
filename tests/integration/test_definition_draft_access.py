"""Private draft visibility through queue, quality and lineage HTTP reads."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.integration._resources import unique_org


@pytest_asyncio.fixture
async def draft_store() -> AsyncIterator[Any]:
    from server.core.ch import get_client

    client = get_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_only_admins_read_unapproved_definition_drafts(
    control_plane_url: str, mint_test_token: Any, draft_store: Any
) -> None:
    from server.memory.store import learnings as repository
    from server.memory.store.rows import next_version

    org, namespace = unique_org("draft-access"), f"draft-{uuid.uuid4().hex[:8]}"
    admin = mint_test_token(org=org, namespaces=[namespace], roles=["admin"], subject="reviewer")
    viewer = mint_test_token(org=org, namespaces=[namespace], roles=["viewer"], subject="reader")
    base = datetime(2026, 1, 1)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, kind in enumerate(("conversation_seeder", "knowledge_gap", "query_history")):
        await repository.insert_row(
            dict(
                org=org,
                namespace=namespace,
                candidate_id=ids[i],
                memory_key=f"definition/{ids[i]}",
                memory_type="definition",
                proposed_value=json.dumps({"term": "ARR", "body": "draft-canary"}),
                supporter_count=0,
                confidence_avg=1.0,
                supporters=[],
                rationale="draft-canary",
                source_kind=kind,
                agent="-",
                status="pending",
                proposed_at=base + timedelta(seconds=i),
                version=next_version(),
            ),
            ch=draft_store,
        )
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=20) as client:

        async def call(op: str, token: str, **args: Any) -> dict[str, Any]:
            result = await client.post(
                f"/v1/{op}",
                json={"org": org, "namespace": namespace, **args},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert result.status_code == 200, result.text
            return result.json()

        for token, expected in ((viewer, ids[0]), (admin, ids[2])):
            listed = await call("learnings.list", token, limit=1)
            assert [r["candidate_id"] for r in listed["rows"]] == [expected]
            counts = await call("learnings.quality", token, source_kind=None)
            assert [r["pending"] for r in counts["rows"]] == [1 if token == viewer else 3]
        for scope in (namespace, ""):
            empty_kind = await call("learnings.quality", viewer, namespace=scope, source_kind="")
            assert empty_kind["rows"] == []
        await call(
            "memory.set",
            admin,
            agent="-",
            memory_key="published",
            value="Approved text",
            tags=[f"candidate-id:{ids[2]}", "source-kind:query_history"],
        )
        for token in (viewer, admin):
            lineage = await call("memory.lineage", token, agent="-", memory_key="published")
            assert lineage["found"]
            assert (lineage["candidate"] is not None) is (token == admin)
