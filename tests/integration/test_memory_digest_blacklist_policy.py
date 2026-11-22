"""A pattern refuses derived text without relying on a source-row sweep."""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from server.core.ch import get_client, set_query_tag
from server.core.datetimes import naive_utc_now
from server.memory.app import blacklist_guard, period_digest, session_summary
from tests.integration._resources import unique_org


@pytest.fixture
async def connected():
    ch = get_client()
    await ch.connect()
    yield ch
    await ch.disconnect()


async def call(client, op, identity, **fields):
    result = await client.post(f"/v1/{op}", json={**identity, **fields})
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize("pattern_type", ["substring", "regex"])
async def test_later_pattern_withholds_digest_without_tombstoning_sources(
    control_plane_url, mint_test_token, connected, pattern_type
):
    org = unique_org("digest-pattern")
    identity = {"org": org, "namespace": "facts", "agent": "writer", "session_id": "session"}
    token = mint_test_token(org=org, namespaces=["facts"], roles=["admin"])
    nonce = uuid.uuid4().hex
    facts = [f"north_{nonce}", f"star_{nonce}"]
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:
        written = []
        for n, value in enumerate(facts):
            set_query_tag("test.digest_blacklist.seed")
            written.append(
                await call(
                    client,
                    "memory.set",
                    identity,
                    memory_key=f"fact-{n}",
                    memory_type="episodic",
                    value=value,
                )
            )
        inp = SimpleNamespace(**identity, user_id="-", run_id="-")
        state = SimpleNamespace(written=written, observation_date=None)
        blacklist_guard.invalidate_cache()
        await session_summary._write_session_summary(inp, state)
        before = await call(client, "memory.digests", identity)
        assert [r["summary_text"] for r in before["rows"]] == ["\n".join(facts)]
        pattern = "\n".join(facts)
        if pattern_type == "regex":
            pattern = "^" + pattern + "$"
        await call(
            client,
            "blacklist.add",
            identity,
            pattern=pattern,
            pattern_type=pattern_type,
            sweep=False,
        )
        after = await call(client, "memory.digests", identity)
        assert after["rows"] == [] and not after["truncated"]
        blacklist_guard.invalidate_cache()
        with pytest.raises(HTTPException) as error:
            await session_summary._write_session_summary(inp, state)
        assert error.value.status_code == 422
        for n, value in enumerate(facts):
            set_query_tag("test.digest_blacklist.control")
            current = await call(client, "memory.get", identity, memory_key=f"fact-{n}")
            assert current["found"] and current["value"] == value
        rows = await connected.query_all(
            "SELECT version FROM loom.session_summaries FINAL "
            "WHERE org={org:String} AND namespace='facts'",
            params={"org": org},
        )
        assert [r["version"] for r in rows] == [before["rows"][0]["version"]]


async def test_period_output_refuses_pattern_before_persistence(
    control_plane_url, mint_test_token, connected, monkeypatch
):
    org = unique_org("period-pattern")
    identity = {"org": org, "namespace": "facts", "agent": "writer"}
    token = mint_test_token(org=org, namespaces=["facts"], roles=["admin"])
    blocked = f"refused period {uuid.uuid4().hex}"
    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:
        await call(client, "memory.set", identity, memory_key="seed", value="allowed source")
        await call(client, "blacklist.add", identity, pattern=blocked, sweep=False)
    blacklist_guard.invalidate_cache()
    monkeypatch.setattr(period_digest, "_source_memories", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(period_digest, "_period_writer", AsyncMock(return_value="writer"))
    llm = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(text=blocked)))
    now = naive_utc_now()
    period = period_digest.Period(
        org,
        "facts",
        "writer",
        "-",
        tuple(
            {
                "session_id": f"session-{n}",
                "at": now - timedelta(days=n),
                "summary_text": f"allowed source {n}",
            }
            for n in (2, 1)
        ),
    )
    result = period_digest.PeriodDigestResult()
    from server.embedding import get_embedder

    assert not await period_digest._write_period(connected, llm, get_embedder(), period, result)
    assert result.failures
    assert (
        await connected.query_all(
            "SELECT memory_key FROM loom.memory FINAL WHERE org={org:String} "
            "AND namespace='facts' AND memory_key={key:String}",
            params={"org": org, "key": period.memory_key},
        )
        == []
    )
    llm.complete.return_value = SimpleNamespace(text="an allowed period")
    result = period_digest.PeriodDigestResult()
    assert await period_digest._write_period(connected, llm, get_embedder(), period, result)
    row = await connected.query_one(
        "SELECT value, status, source_class, content_sha256 FROM loom.memory FINAL "
        "WHERE org={org:String} AND namespace='facts' AND memory_key={key:String}",
        params={"org": org, "key": period.memory_key},
    )
    import json

    from server.core.hashing import sha256_value

    assert json.loads(row["value"]) == "an allowed period"
    assert row["status"] == "proposed" and row["source_class"] == "derived"
    actual = row["content_sha256"]
    assert (actual.decode() if isinstance(actual, bytes) else actual) == sha256_value(
        "an allowed period"
    )
