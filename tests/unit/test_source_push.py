"""Setup activation and sync hints stay bound across source lifecycle changes."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_source_push import (
    PushConfigureInput,
    PushConfirmInput,
    PushStatusInput,
)
from server.knowledge.app import source_push as app
from server.knowledge.domain.source_push import DeliveryRefused, PushRegistration
from server.knowledge.domain.sources import KnowledgeSource


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        src=KnowledgeSource(
            org="org", namespace="docs", name="source", kind="notion", source_id="source-id"
        ),
        reg=None,
        sync={},
        writes=[],
        requests=[],
        ready=True,
    )
    monkeypatch.setattr(app.settings, "public_base_url", "https://loom.example")
    monkeypatch.setattr(app.settings, "knowledge_source_push_enabled", True)
    monkeypatch.setattr(app.settings, "jwt_secret", "source-push-test-root-" * 4)
    monkeypatch.setattr(app.sources, "credential_of", lambda src: "credential")

    @asynccontextmanager
    async def lease(*args: object, **kwargs: object):
        yield

    @asynccontextmanager
    async def renewal_lease(*args: object, **kwargs: object):
        yield True

    @asynccontextmanager
    async def fence(*args: object, **kwargs: object):
        yield lambda uncertain: None

    async def get_source(*args: object, **kwargs: object):
        return state.src

    async def get_reg(org: str, namespace: str, source_id: str, *, ch=None):
        if (
            state.reg
            and state.reg.org == org
            and state.reg.namespace == namespace
            and state.reg.source_id == source_id
        ):
            return state.reg
        return None

    async def put(reg: PushRegistration, *, ch=None):
        state.reg = reg
        state.writes.append(reg)

    async def read_state(src: KnowledgeSource):
        return state.sync

    async def update_state(
        src: KnowledgeSource, previous: dict[str, str], changes: dict[str, object]
    ):
        state.requests.append((src, changes))
        detail = json.loads(state.sync.get("detail") or "{}")
        state.sync = {"detail": json.dumps({**detail, **changes})}

    monkeypatch.setattr(app.sources, "sources_lease", lease)
    monkeypatch.setattr(app.leases, "holding", renewal_lease)
    monkeypatch.setattr(app, "fenced", fence)
    monkeypatch.setattr(app.sources, "_source_or_404", get_source)
    monkeypatch.setattr(app.source_store, "get_source", get_source)
    monkeypatch.setattr(app.sources, "_require_served", lambda src: None)
    monkeypatch.setattr(app.sources, "_require_source_namespace", AsyncMock())
    monkeypatch.setattr(app.sources, "_require_no_erasure", AsyncMock())
    monkeypatch.setattr(app.sources, "_read_state", read_state)
    monkeypatch.setattr(app.sources, "_update_state", update_state)
    monkeypatch.setattr(app.store, "get", get_reg)
    monkeypatch.setattr(app.store, "put", put)
    monkeypatch.setattr(app.machines, "fleet_at", AsyncMock(side_effect=lambda *args: state.ready))
    monkeypatch.setattr(app.notion, "workspace_id", AsyncMock(return_value="workspace"))
    monkeypatch.setattr(app.linear, "organization_id", AsyncMock(return_value="organization"))
    return state


async def setup_notion(env: SimpleNamespace) -> None:
    await app.handle_configure(PushConfigureInput(org="org", namespace="docs", name="source"))


async def capture(env: SimpleNamespace, token: str = "verification-challenge") -> None:
    await app.receive(
        "org",
        "docs",
        "source-id",
        env.reg.generation,
        json.dumps({"verification_token": token}).encode(),
        {},
    )


async def activate(env: SimpleNamespace) -> None:
    await capture(env)
    await app.handle_confirm(
        PushConfirmInput(
            org="org",
            namespace="docs",
            name="source",
            generation=env.reg.generation,
            challenge_fingerprint=app.domain.challenge_fingerprint("verification-challenge"),
        )
    )


@pytest.mark.asyncio
async def test_notion_challenge_does_not_sync_or_activate_and_only_explicitly_reveals(
    env: SimpleNamespace,
) -> None:
    await setup_notion(env)
    await capture(env)
    normal = await app.handle_status(PushStatusInput(org="org", namespace="docs", name="source"))
    assert normal.status == "pending" and normal.verification_token == ""
    reveal = await app.handle_status(
        PushStatusInput(org="org", namespace="docs", name="source", reveal_challenge=True)
    )
    assert reveal.verification_token == "verification-challenge"
    assert env.requests == []
    assert "verification-challenge" not in env.reg.secret_enc
    with pytest.raises(DeliveryRefused, match="already captured"):
        await capture(env, "attacker-replacement")
    await activate(env)
    assert env.reg.status == "active"
    assert (
        await app.handle_status(
            PushStatusInput(org="org", namespace="docs", name="source", reveal_challenge=True)
        )
    ).verification_token == ""


@pytest.mark.asyncio
async def test_confirmation_requires_exact_generation_and_fingerprint(env: SimpleNamespace) -> None:
    await setup_notion(env)
    await capture(env)
    for generation, fingerprint in (
        ("old", app.domain.challenge_fingerprint("verification-challenge")),
        (env.reg.generation, "wrong"),
    ):
        with pytest.raises(app.ValidationError):
            await app.handle_confirm(
                PushConfirmInput(
                    org="org",
                    namespace="docs",
                    name="source",
                    generation=generation,
                    challenge_fingerprint=fingerprint,
                )
            )
    assert env.reg.status == "pending" and not env.requests


@pytest.mark.asyncio
async def test_active_notion_only_records_one_hint_and_preserves_full_request(
    env: SimpleNamespace,
) -> None:
    await setup_notion(env)
    await activate(env)
    raw = json.dumps({"workspace_id": "workspace", "type": "page.content_updated"}).encode()
    signature = "sha256=" + hmac.new(b"verification-challenge", raw, hashlib.sha256).hexdigest()
    for _ in range(2):
        await app.receive(
            "org", "docs", "source-id", env.reg.generation, raw, {"x-notion-signature": signature}
        )
    assert len(env.requests) == 1
    env.sync = {"detail": json.dumps({"requested_at": "earlier", "full": True, "cursor": "keep"})}
    await app.receive(
        "org", "docs", "source-id", env.reg.generation, raw, {"x-notion-signature": signature}
    )
    assert json.loads(env.sync["detail"]) == {
        "requested_at": "earlier",
        "full": True,
        "cursor": "keep",
    }
    with pytest.raises(DeliveryRefused):
        await app.receive("org", "docs", "source-id", env.reg.generation, raw, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["removed", "replaced", "items", "retired", "other-org", "other-namespace"]
)
async def test_late_delivery_cannot_reach_changed_registration(
    env: SimpleNamespace, change: str
) -> None:
    await setup_notion(env)
    generation = env.reg.generation
    if change == "removed":
        env.src = None
    elif change == "replaced":
        env.src = dataclasses.replace(env.src, source_id="replacement")
    elif change == "items":
        env.src = dataclasses.replace(env.src, items=[{"type": "page", "id": "new"}])
    elif change == "retired":
        await app.retire(env.src)
    before = len(env.writes)
    with pytest.raises(DeliveryRefused):
        await app.receive(
            "other-org" if change == "other-org" else "org",
            "other-docs" if change == "other-namespace" else "docs",
            "source-id",
            generation,
            b'{"verification_token":"verification-challenge"}',
            {},
        )
    assert not env.requests and len(env.writes) == before


@pytest.mark.asyncio
async def test_fleet_downgrade_refuses_unsigned_challenge_write(env: SimpleNamespace) -> None:
    await setup_notion(env)
    env.ready = False
    with pytest.raises(app.BadConfigError):
        await capture(env)
    assert "challenge" not in app._secrets(env.reg)


@pytest.mark.asyncio
async def test_setup_requires_explicit_https_origin(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for url in (
        "",
        "http://loom.example",
        "https://user:secret@loom.example",
        "https://loom.example/path",
        "https://[broken",
    ):
        monkeypatch.setattr(app.settings, "public_base_url", url)
        with pytest.raises(app.BadConfigError):
            await setup_notion(env)
    assert not env.writes


@pytest.mark.asyncio
async def test_removal_during_drive_watch_cannot_reactivate(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.src = dataclasses.replace(env.src, kind="gdrive")
    monkeypatch.setattr(app.gdrive, "parse_service_account", lambda key: object())
    monkeypatch.setattr(app.gdrive, "access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(app.gdrive_push, "member_drives", AsyncMock(return_value=set()))
    stopped = AsyncMock()
    monkeypatch.setattr(app.gdrive_push, "stop", stopped)

    async def watch(*args: object, **kwargs: object):
        await app.retire(env.src)
        env.src = None
        return {
            "id": kwargs["channel_id"],
            "token": kwargs["channel_token"],
            "resource_id": "resource",
            "expiration": time.time() + 3600,
            "drive_id": "",
        }

    monkeypatch.setattr(app.gdrive_push, "watch", watch)
    with pytest.raises(DeliveryRefused):
        await app.handle_configure(PushConfigureInput(org="org", namespace="docs", name="source"))
    assert env.reg.status == "retired" and env.reg.secret_enc == ""
    stopped.assert_awaited_once()


def test_pending_secret_read_cannot_enter_idempotency_replay_storage() -> None:
    from server.knowledge.api.ops_source_push import OPERATIONS

    status = next(op for op in OPERATIONS if op.name.endswith(".push_status"))
    assert not status.idempotent


def test_setup_audit_accepts_response_and_omits_secrets() -> None:
    body = PushConfirmInput(
        org="org", namespace="docs", name="source", generation="generation", signing_secret="secret"
    )
    assert app.audit_subject(body, {"verification_token": "secret"}) == {
        "org": "org",
        "namespace": "docs",
        "name": "source",
    }


@pytest.mark.asyncio
async def test_reseal_rechecks_row_after_scanning_before_erasure(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_notion(env)
    scanned = env.reg
    pages = AsyncMock(side_effect=[[scanned], []])
    monkeypatch.setattr(app.store, "sealed_page", pages)

    @asynccontextmanager
    async def erase_before_claim(*args: object, **kwargs: object):
        env.reg = None
        yield

    monkeypatch.setattr(app.sources, "sources_lease", erase_before_claim)
    before = len(env.writes)
    assert await app.reseal_registry(dry_run=False) == (0, [])
    assert env.reg is None and len(env.writes) == before


@pytest.mark.asyncio
async def test_reseal_uses_current_generation_and_fleet_gate(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_notion(env)
    scanned = env.reg
    await setup_notion(env)
    generation = env.reg.generation
    monkeypatch.setattr(app.store, "sealed_page", AsyncMock(side_effect=[[scanned], []]))
    monkeypatch.setattr(app, "resealed", lambda purpose, value: value)
    assert await app.reseal_registry(dry_run=False) == (1, [])
    assert env.reg.generation == generation
    env.ready = False
    monkeypatch.setattr(app.store, "sealed_page", AsyncMock(side_effect=[[env.reg], []]))
    with pytest.raises(app.BadConfigError):
        await app.reseal_registry(dry_run=False)


@pytest.mark.asyncio
async def test_lost_publication_ack_does_not_stop_current_drive_channels(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.src = dataclasses.replace(env.src, kind="gdrive")
    monkeypatch.setattr(app.gdrive, "parse_service_account", lambda key: object())
    monkeypatch.setattr(app.gdrive, "access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(app.gdrive_push, "member_drives", AsyncMock(return_value=set()))
    stopped = AsyncMock()
    monkeypatch.setattr(app.gdrive_push, "stop", stopped)
    original_put = app.store.put

    async def uncertain_put(reg: PushRegistration, *, ch=None):
        await original_put(reg, ch=ch)
        if reg.status == "active":
            raise ConnectionError("ack lost after commit")

    async def watch(*args: object, **kwargs: object):
        return {
            "id": kwargs["channel_id"],
            "token": kwargs["channel_token"],
            "resource_id": "resource",
            "expiration": time.time() + 3600,
            "drive_id": "",
        }

    monkeypatch.setattr(app.store, "put", uncertain_put)
    monkeypatch.setattr(app.gdrive_push, "watch", watch)
    with pytest.raises(ConnectionError, match="ack lost"):
        await app.handle_configure(PushConfigureInput(org="org", namespace="docs", name="source"))
    assert env.reg.status == "active" and app._secrets(env.reg)["channels"]
    stopped.assert_not_awaited()


@pytest.mark.asyncio
async def test_linear_prepares_stable_address_then_requires_exact_confirmation(
    env: SimpleNamespace,
) -> None:
    env.src = dataclasses.replace(env.src, kind="linear")
    pending = await app.handle_configure(
        PushConfigureInput(org="org", namespace="docs", name="source")
    )
    assert pending.status == "pending"
    assert pending.callback_url == (
        f"https://loom.example/hooks/knowledge/docs/source-id/{pending.generation}?org=org"
    )
    generation = pending.generation
    raw = json.dumps(
        {"organizationId": "organization", "webhookTimestamp": int(time.time() * 1000)}
    ).encode()
    signature = hmac.new(b"signing-secret", raw, hashlib.sha256).hexdigest()
    with pytest.raises(DeliveryRefused):
        await app.receive(
            "org", "docs", "source-id", generation, raw, {"linear-signature": signature}
        )
    with pytest.raises(app.ValidationError):
        await app.handle_confirm(
            PushConfirmInput(
                org="org",
                namespace="docs",
                name="source",
                generation="old",
                signing_secret="signing-secret",
            )
        )
    active = await app.handle_confirm(
        PushConfirmInput(
            org="org",
            namespace="docs",
            name="source",
            generation=generation,
            signing_secret="signing-secret",
        )
    )
    assert active.status == "active" and active.callback_url == pending.callback_url
    await app.receive("org", "docs", "source-id", generation, raw, {"linear-signature": signature})
    assert len(env.requests) == 1


@pytest.mark.asyncio
async def test_deployment_opt_out_refuses_setup(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.settings, "knowledge_source_push_enabled", False)
    with pytest.raises(app.BadConfigError):
        await setup_notion(env)
    assert not env.writes


@pytest.mark.asyncio
async def test_deployment_opt_out_refuses_existing_delivery(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_notion(env)
    await activate(env)
    monkeypatch.setattr(app.settings, "knowledge_source_push_enabled", False)
    raw = json.dumps({"workspace_id": "workspace"}).encode()
    signature = "sha256=" + hmac.new(b"verification-challenge", raw, hashlib.sha256).hexdigest()
    with pytest.raises(DeliveryRefused):
        await app.receive(
            "org", "docs", "source-id", env.reg.generation, raw, {"x-notion-signature": signature}
        )
    assert not env.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["", "https://[broken"])
async def test_paused_setup_can_be_inspected_and_disabled_without_public_origin(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    await setup_notion(env)
    monkeypatch.setattr(app.settings, "knowledge_source_push_enabled", False)
    monkeypatch.setattr(app.settings, "public_base_url", origin)
    status = await app.handle_status(PushStatusInput(org="org", namespace="docs", name="source"))
    assert not status.enabled and status.callback_url == "" and status.status == "pending"
    disabled = await app.handle_configure(
        PushConfigureInput(org="org", namespace="docs", name="source", enabled=False)
    )
    assert disabled.status == "disabled" and env.reg.secret_enc == ""


@pytest.mark.asyncio
async def test_provider_and_channel_cleanup_read_configured_timeouts(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits = []

    @asynccontextmanager
    async def timeout(seconds: float):
        waits.append(seconds)
        yield

    monkeypatch.setattr(app.asyncio, "timeout", timeout)
    monkeypatch.setattr(app.settings, "knowledge_source_push_provider_timeout_s", 17.0)
    monkeypatch.setattr(app.settings, "knowledge_source_push_stop_timeout_s", 1.25)
    monkeypatch.setattr(app.settings, "knowledge_source_push_cleanup_timeout_s", 3.0)
    await setup_notion(env)
    assert waits == [17.0]
    env.src = dataclasses.replace(env.src, kind="gdrive")
    env.reg = dataclasses.replace(env.reg, provider="gdrive")
    monkeypatch.setattr(app.gdrive, "parse_service_account", lambda key: object())
    monkeypatch.setattr(app.gdrive, "access_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(app.gdrive_push, "member_drives", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        app.gdrive_push,
        "watch",
        AsyncMock(
            return_value={
                "id": "created",
                "token": "channel-token",
                "resource_id": "resource",
                "expiration": time.time() + 3600,
                "drive_id": "",
            }
        ),
    )
    stopped = AsyncMock()
    monkeypatch.setattr(app.gdrive_push, "stop", stopped)
    await app._renew_drive(env.reg, initial=True)
    assert waits == [17.0, 17.0, 3.0]
    await app._stop_channels("token", [{"id": "superseded"}])
    assert waits == [17.0, 17.0, 3.0, 1.25]
    stopped.assert_awaited_once_with("token", {"id": "superseded"})


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"knowledge_source_push_stop_timeout_s": 6.0}, "STOP_TIMEOUT_S"),
        ({"knowledge_source_push_provider_timeout_s": 60.0}, "renewal work window"),
        ({"knowledge_source_push_max_pass_seconds": 50.0}, "MAX_PASS_SECONDS"),
    ],
)
def test_push_budgets_refuse_cleanup_or_attempt_outside_deadline(
    overrides: dict[str, float], error: str
) -> None:
    from pydantic import ValidationError

    from server.settings.catalog import KnowledgeSettings

    with pytest.raises(ValidationError, match=error):
        KnowledgeSettings(**overrides)


def test_push_budgets_accept_configured_deadlines() -> None:
    from server.settings.catalog import KnowledgeSettings

    configured = KnowledgeSettings(
        knowledge_source_push_provider_timeout_s=36.0,
        knowledge_source_push_stop_timeout_s=3.0,
        knowledge_source_push_cleanup_timeout_s=6.0,
        knowledge_source_push_max_pass_seconds=180.0,
    )
    assert configured.knowledge_source_push_provider_timeout_s == 36.0
    assert configured.knowledge_source_push_stop_timeout_s == 3.0
    assert configured.knowledge_source_push_cleanup_timeout_s == 6.0
    assert configured.knowledge_source_push_max_pass_seconds == 180.0


def test_push_budget_boundary_matches_the_actual_renewal_lease_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from server.settings.catalog import KnowledgeSettings

    monkeypatch.setattr(app.leases, "time", SimpleNamespace(monotonic=lambda: 100.0))
    lease = app.leases.Lease("push-budget", ttl_s=120, ch=object(), holder="budget-test")
    lease._renewed = 100.0
    work_window = app.leases.work_left(lease, 120)
    assert work_window == 48.0
    with pytest.raises(ValidationError, match="renewal work window"):
        KnowledgeSettings(
            knowledge_source_push_provider_timeout_s=40.0,
            knowledge_source_push_cleanup_timeout_s=work_window - 40.0,
        )
    configured = KnowledgeSettings(
        knowledge_source_push_provider_timeout_s=40.0,
        knowledge_source_push_cleanup_timeout_s=work_window - 41.0,
    )
    assert configured.knowledge_source_push_cleanup_timeout_s == 7.0


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
