"""Retained notification registrations keep their exact namespace and secret."""

import dataclasses
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import source_push as app
from server.knowledge.domain.source_push import PushRegistration


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(
        reg=PushRegistration(
            org="org",
            namespace="docs",
            name="retired",
            source_id="retained-id",
            provider="notion",
            generation="retained-generation",
            fingerprint="retained-fingerprint",
            status="active",
            secret_enc="retained-secret",
        ),
        writes=[],
    )

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield

    async def get(org, namespace, source_id, **kwargs):
        assert (org, namespace, source_id) == ("org", "docs", "retained-id")
        return state.reg

    async def put(row, **kwargs):
        state.reg = row
        state.writes.append(row)

    monkeypatch.setattr(app.sources, "sources_lease", lease)
    monkeypatch.setattr(app.sources, "_require_no_erasure", AsyncMock())
    monkeypatch.setattr(app.store, "get", get)
    monkeypatch.setattr(app, "_put", put)
    monkeypatch.setattr(app, "resealed", lambda purpose, value: "resealed:" + value)
    return state


@pytest.mark.asyncio
async def test_reseal_rechecks_row_after_scanning_before_erasure(env, monkeypatch):
    scanned = env.reg
    monkeypatch.setattr(app.store, "sealed_page", AsyncMock(side_effect=[[scanned], []]))

    @asynccontextmanager
    async def erase_before_claim(*args, **kwargs):
        env.reg = None
        yield

    monkeypatch.setattr(app.sources, "sources_lease", erase_before_claim)
    assert await app.reseal_registry(dry_run=False) == (0, [])
    assert env.reg is None and env.writes == []


@pytest.mark.asyncio
async def test_reseal_uses_current_generation_and_preserves_retained_metadata(env, monkeypatch):
    scanned = env.reg
    current = dataclasses.replace(
        scanned, generation="replacement", secret_enc="replacement-secret"
    )
    env.reg = current
    monkeypatch.setattr(app.store, "sealed_page", AsyncMock(side_effect=[[scanned], []]))
    assert await app.reseal_registry(dry_run=False) == (1, [])
    assert env.writes == [dataclasses.replace(current, secret_enc="resealed:replacement-secret")]
    assert dataclasses.asdict(env.reg) == {
        **dataclasses.asdict(current),
        "secret_enc": "resealed:replacement-secret",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run,changed", [(True, "replacement"), (False, None)])
async def test_reseal_dry_run_or_unreadable_secret_never_deletes_a_registration(
    env, monkeypatch, dry_run, changed
):
    retained = env.reg
    monkeypatch.setattr(app.store, "sealed_page", AsyncMock(side_effect=[[retained], []]))
    monkeypatch.setattr(app, "resealed", lambda purpose, value: changed)
    expected_bad = [] if changed is not None else ["org/docs/retained-id"]
    assert await app.reseal_registry(dry_run=dry_run) == (int(changed is not None), expected_bad)
    assert env.reg is retained and env.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["docs", "other-docs", "missing"])
async def test_registry_lookup_requires_the_namespace_key(namespace: str) -> None:
    from server.knowledge.store import source_push

    rows = [
        PushRegistration(
            org="org",
            namespace=ns,
            name="source",
            source_id="shared-id",
            provider="notion",
            generation=ns,
            fingerprint="fp",
            secret_enc=ns,
        )
        for ns in ("docs", "other-docs")
    ]

    class Ch:
        async def query_one(self, sql: str, params: dict[str, str]):
            assert "org = {org:String} AND namespace = {ns:String}" in sql
            assert "source_id = {source:String}" in sql
            assert params == {"org": "org", "ns": namespace, "source": "shared-id"}
            return next(
                (dataclasses.asdict(row) for row in rows if row.namespace == params["ns"]), None
            )

    result = await source_push.get("org", namespace, "shared-id", ch=Ch())
    assert result == next((row for row in rows if row.namespace == namespace), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("scan", ["drive_page", "sealed_page"])
async def test_registry_maintenance_cursor_follows_the_namespace_key(scan: str) -> None:
    from server.knowledge.store import source_push

    after = ("org", "docs", "shared-id")

    class Ch:
        async def query_all(self, sql: str, params: dict[str, tuple[str, str, str]]):
            assert "(org, namespace, source_id) > {after:Tuple(String, String, String)}" in sql
            assert "ORDER BY org, namespace, source_id LIMIT" in sql
            assert params == {"after": after}
            return []

    assert await getattr(source_push, scan)(after, ch=Ch()) == []
