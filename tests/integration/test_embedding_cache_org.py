"""loom.embedding_cache against a real ClickHouse: a request's vector lands
with its org, and an org's rows are deleted by org, without the texts they
were keyed by, leaving every other org's."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

import server.embedding._cache as ec
from server.core.ch import get_client, own_write_reads
from server.erasure.store.steps_ledgers import delete_embedding_cache_for_org
from server.observability import genai
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def ch() -> Any:
    client = get_client()
    await client.connect()
    yield client
    genai._identity.set(None)
    await client.disconnect()


async def _embed(org: str, model: str, text: str) -> None:
    async def fetch(t: str) -> list[float]:
        return [0.5, 0.25]

    genai.set_identity(org=org, namespace="ns", operation="memory.search")
    ec._cache._store.clear()
    await ec.cached_embed(model, text, fetch, dim=2)
    await asyncio.gather(*list(ec._SHARED_TASKS), return_exceptions=True)


async def _rows(ch: Any, model: str) -> list[tuple[str, str]]:
    with own_write_reads():
        rows = await ch.query_all(
            "SELECT org, key FROM loom.embedding_cache WHERE model = {m:String} ORDER BY org",
            params={"m": model},
        )
    return [(r["org"], r["key"]) for r in rows]


async def test_an_orgs_cached_vectors_carry_it_and_are_deleted_by_it(ch: Any) -> None:
    from server.settings import settings

    if not settings.embedding_cache_shared:
        pytest.skip("the shared tier is off")
    model = f"test-{uuid.uuid4().hex[:8]}"
    a, b = unique_org("ecache-a"), unique_org("ecache-b")
    await _embed(a, model, "the same query")
    await _embed(b, model, "the same query")
    for _ in range(50):  # async inserts land shortly after
        if len(await _rows(ch, model)) == 2:
            break
        await asyncio.sleep(0.1)
    rows = await _rows(ch, model)
    assert sorted(o for o, _ in rows) == sorted([a, b])
    assert rows[0][1] != rows[1][1]  # one text, a key per org

    deleted = await delete_embedding_cache_for_org(a)
    assert deleted == 1
    assert [o for o, _ in await _rows(ch, model)] == [b]
