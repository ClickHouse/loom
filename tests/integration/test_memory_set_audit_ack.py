"""Receipt storage failure is visible after a real committed point write."""

import uuid

import httpx
import pytest

from server.core import pg
from server.core.ch import get_client
from server.core.errors import BackendUnavailableError
from server.core.hashing import sha256_value
from server.memory.api.models_crud import MemorySetInput
from server.memory.app.write import handle_memory_set
from server.observability import audit
from tests.integration._resources import unique_org


@pytest.fixture
async def connected():
    ch = get_client()
    await ch.connect()
    if pg.enabled():
        await pg.open_pool()
    try:
        yield ch
    finally:
        if pg.enabled():
            await pg.close_pool()
        await ch.disconnect()


@pytest.mark.parametrize(
    "kind,high",
    [
        ("episodic", False),
        ("profile", False),
        ("semantic", True),
        ("semantic", False),
    ],
)
async def test_failed_receipt_reports_committed_memory_and_retry_recovers(
    control_plane_url, mint_test_token, connected, monkeypatch, kind, high
):
    org = unique_org("set-audit-ack")
    identity = {"org": org, "namespace": "facts", "agent": "writer", "memory_key": "key"}
    token = mint_test_token(org=org, namespaces=["facts"], roles=["writer"])
    value = f"replacement content {uuid.uuid4().hex}"
    inp = MemorySetInput(
        **identity, memory_type=kind, sensitivity="high" if high else "low", value=value
    )
    original = audit.emit_strict

    async def unavailable(event_type, **kwargs):
        if event_type == "memory.set":
            raise TimeoutError("receipt storage unavailable")
        return await original(event_type, **kwargs)

    async with httpx.AsyncClient(
        base_url=control_plane_url, headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as client:
        seed = await client.post(
            "/v1/memory.set",
            json={
                **identity,
                "memory_type": kind,
                "value": "old public content",
            },
        )
        assert seed.status_code == 200, seed.text
        monkeypatch.setattr(audit, "emit_strict", unavailable)
        if pg.enabled():
            await native_receipt_survives_optional_emit_failure(inp)
            return
        with pytest.raises(BackendUnavailableError) as error:
            await handle_memory_set(inp)
        assert error.value.status_code == 503
        assert error.value.extra["operation"] == "memory.set.audit"
        assert error.value.extra["outcome_unknown"]
        current = await client.post("/v1/memory.get", json=identity)
        assert current.status_code == 200, current.text
        assert current.json()["found"] and current.json()["value"] == value
        if high:
            assert (
                await connected.query_all(
                    "SELECT memory_key FROM loom.memory_embeddings FINAL "
                    "WHERE org={org:String} AND namespace='facts' AND memory_key='key' "
                    "AND tombstoned_at IS NULL",
                    params={"org": org},
                )
                == []
            )
        params = {"org": org, "sha": sha256_value(value)}
        count_sql = (
            "SELECT count() AS n FROM loom.audit_log WHERE org={org:String} "
            "AND namespace='facts' AND event_type='memory.set' "
            "AND JSONExtractString(subject,'content_sha256')={sha:String}"
        )
        assert (await connected.query_one(count_sql, params=params))["n"] == 0
        monkeypatch.setattr(audit, "emit_strict", original)
        retried = await handle_memory_set(inp)
        assert retried.content_sha256 == sha256_value(value)
        assert (await connected.query_one(count_sql, params=params))["n"] == 1


async def native_receipt_survives_optional_emit_failure(inp):
    out = await handle_memory_set(inp)
    assert out.content_sha256 == sha256_value(inp.value)
    rows = await pg.fetch(
        "SELECT audit->>'event_type' AS kind, audit->>'row_hash' AS hash "
        "FROM loom.memory_revisions WHERE org=$1 AND namespace=$2 AND content_sha256=$3",
        inp.org,
        inp.namespace,
        out.content_sha256,
    )
    assert rows and all(row["kind"] == "memory.revision" and len(row["hash"]) == 64 for row in rows)
