"""loom.embedding_cache against a real ClickHouse: a request's vector lands
with its org, and an erasure in the org, at namespace or user scope, deletes
the org's cached vectors without the texts they were keyed by, leaving every
other org's."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

import server.embedding._cache as ec
from server.core.ch import get_client, own_write_reads
from server.erasure.store import steps_ledgers
from server.observability import genai
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def ch() -> Any:
    from server.settings import settings

    if not settings.embedding_cache_shared:
        pytest.skip("the shared tier is off")
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


async def _rows(ch: Any, model: str) -> list[dict[str, Any]]:
    with own_write_reads():
        return await ch.query_all(
            "SELECT * FROM loom.embedding_cache WHERE model = {m:String} ORDER BY org",
            params={"m": model},
        )


async def _two_orgs(ch: Any) -> tuple[str, str, str]:
    model = f"test-{uuid.uuid4().hex[:8]}"
    a, b = unique_org("ecache-a"), unique_org("ecache-b")
    await _embed(a, model, "the same query")
    await _embed(b, model, "the same query")
    for _ in range(50):  # async inserts land shortly after
        if len(await _rows(ch, model)) == 2:
            return model, a, b
        await asyncio.sleep(0.1)
    pytest.fail(f"the two orgs' cache rows never landed in loom.embedding_cache (model {model})")


async def test_an_orgs_cached_vector_lands_with_its_org(ch: Any) -> None:
    model, a, b = await _two_orgs(ch)
    rows = await _rows(ch, model)
    assert sorted(r["org"] for r in rows) == sorted([a, b])
    for r in rows:
        assert r["model"] == model and r["dim"] == 2
        assert [float(x) for x in r["embedding"]] == [0.5, 0.25]
        assert r["key"] == ec._key(model, "the same query", r["org"])  # a key per org
    assert rows[0]["key"] != rows[1]["key"]


@pytest.mark.parametrize("scope", ["namespace", "user_id"])
async def test_an_erasure_in_the_org_deletes_its_cached_vectors_only(ch: Any, scope: str) -> None:
    # idempotency.delete, the step that clears the org's caches at both scopes
    model, a, b = await _two_orgs(ch)
    if scope == "namespace":
        deleted = await steps_ledgers.delete_idempotency_by_namespace(a, ["ns"])
    else:
        deleted = await steps_ledgers.delete_idempotency_by_user(a, "u1")
    assert deleted == 1  # the org's one cached vector; it had no replay body
    assert [r["org"] for r in await _rows(ch, model)] == [b]
