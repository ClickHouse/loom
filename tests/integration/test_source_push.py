"""Authenticated provider hints use the live source registry and settled erasure."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


async def test_push_setup_provider_proofs_source_lifecycle_and_erasure(
    ch_client: Any, mint_test_token: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.clients import gdrive, gdrive_push, linear, notion
    from server.core.ch import set_query_tag
    from server.knowledge.app import source_probes
    from server.knowledge.app.source_push import renew_drive
    from server.knowledge.store import source_push as push_store
    from server.main import app
    from server.settings import settings

    set_query_tag("test.source_push")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "knowledge_source_push_enabled", True)
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    monkeypatch.setattr(notion, "workspace_id", AsyncMock(return_value="workspace"))
    monkeypatch.setattr(linear, "organization_id", AsyncMock(return_value="organization"))

    async def probe(kind: str, config: dict[str, Any], credential: str, *, items: Any = None):
        return source_probes.Probe(documents=0, items=items or [])

    monkeypatch.setattr(source_probes, "probe", probe)
    org = unique_org("source-push")
    namespace = "push-docs"
    token = mint_test_token(org=org, namespaces=["*"], roles=["org_admin", "admin", "writer"])
    viewer = mint_test_token(
        org=org, namespaces=[namespace], roles=["viewer"], subject="push-viewer"
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30
        ) as client:

            async def post(op: str, **body: Any) -> dict[str, Any]:
                for attempt in range(6):
                    response = await client.post(
                        f"/v1/{op}",
                        headers=headers,
                        json={"org": org, "namespace": namespace, **body},
                    )
                    if response.status_code != 429 or attempt == 5:
                        assert response.status_code == 200, response.text
                        return response.json()
                    detail = response.json()["detail"]
                    assert detail["code"] == "RATE_LIMITED", response.text
                    delay = detail["retry_after_s"]
                    assert 0 < delay <= 5, response.text
                    await asyncio.sleep(delay)
                raise AssertionError("rate-limit retry bound exhausted")

            await post("namespace.register", owner_email="owner@example.com")
            await post(
                "knowledge.source.add",
                name="pages",
                kind="notion",
                items=[{"type": "page", "id": str(uuid.uuid4())}],
                credential="read-only-source-credential",
            )
            prepared = await post("knowledge.source.push_configure", name="pages")
            assert prepared["status"] == "pending" and prepared["callback_url"]
            path = httpx.URL(prepared["callback_url"]).raw_path.decode()
            challenge = "provider-verification-token"
            assert (
                await client.post(path, json={"verification_token": challenge})
            ).status_code == 200
            assert (await post("knowledge.source.push_status", name="pages"))[
                "verification_token"
            ] == ""
            assert (await post("knowledge.source.inspect", name="pages"))["sync"][
                "requested_at"
            ] == ""
            refused = await client.post(
                "/v1/knowledge.source.push_status",
                headers={"Authorization": f"Bearer {viewer}"},
                json={
                    "org": org,
                    "namespace": namespace,
                    "name": "pages",
                    "reveal_challenge": True,
                },
            )
            assert refused.status_code == 403
            actor = ch_client.query(
                "SELECT actor FROM loom.knowledge_source_push FINAL WHERE org = {org:String}",
                parameters={"org": org},
            ).first_item["actor"]
            assert actor == "process:source-push"
            # A requested replay key cannot persist a revealed challenge.
            revealed = await client.post(
                "/v1/knowledge.source.push_status",
                headers={**headers, "Idempotency-Key": "pending-challenge"},
                json={
                    "org": org,
                    "namespace": namespace,
                    "name": "pages",
                    "reveal_challenge": True,
                },
            )
            assert (
                revealed.status_code == 200 and revealed.json()["verification_token"] == challenge
            )
            assert (
                ch_client.query(
                    "SELECT count() FROM loom.idempotency WHERE org = {org:String}",
                    parameters={"org": org},
                ).first_item["count()"]
                == 0
            )
            active = await post(
                "knowledge.source.push_confirm",
                name="pages",
                generation=prepared["generation"],
                challenge_fingerprint=hashlib.sha256(challenge.encode()).hexdigest(),
            )
            assert active["status"] == "active" and active["verification_token"] == ""
            raw = json.dumps({"workspace_id": "workspace", "type": "page.content_updated"}).encode()
            signature = "sha256=" + hmac.new(challenge.encode(), raw, hashlib.sha256).hexdigest()
            assert (await client.post(path, content=raw)).status_code == 403
            for _ in range(2):
                assert (
                    await client.post(path, content=raw, headers={"X-Notion-Signature": signature})
                ).status_code == 200
            assert (await post("knowledge.source.inspect", name="pages"))["sync"]["requested_at"]
            replay = await client.post(
                "/v1/knowledge.source.push_status",
                headers={**headers, "Idempotency-Key": "pending-challenge"},
                json={
                    "org": org,
                    "namespace": namespace,
                    "name": "pages",
                    "reveal_challenge": True,
                },
            )
            assert replay.status_code == 200 and replay.json()["verification_token"] == ""
            sealed = ch_client.query(
                "SELECT secret_enc FROM loom.knowledge_source_push FINAL WHERE org = {org:String}",
                parameters={"org": org},
            ).first_item["secret_enc"]
            assert challenge not in sealed and "read-only-source-credential" not in sealed
            await post(
                "knowledge.source.rotate",
                name="pages",
                credential="replacement-read-only-credential",
            )
            assert (
                await client.post(path, content=raw, headers={"X-Notion-Signature": signature})
            ).status_code == 403
            assert (await post("knowledge.source.push_status", name="pages"))[
                "status"
            ] == "disabled"

            await post(
                "knowledge.source.add",
                name="projects",
                kind="linear",
                items=[{"type": "project", "id": str(uuid.uuid4())}],
                credential="read-only-project-credential",
            )
            linear_pending = await post("knowledge.source.push_configure", name="projects")
            assert linear_pending["status"] == "pending"
            linear_active = await post(
                "knowledge.source.push_confirm",
                name="projects",
                generation=linear_pending["generation"],
                signing_secret="linear-webhook-secret",
            )
            assert linear_active["callback_url"] == linear_pending["callback_url"]
            linear_path = httpx.URL(linear_active["callback_url"]).raw_path.decode()
            linear_raw = json.dumps(
                {"organizationId": "organization", "webhookTimestamp": int(time.time() * 1000)}
            ).encode()
            proof = hmac.new(b"linear-webhook-secret", linear_raw, hashlib.sha256).hexdigest()
            assert (
                await client.post(
                    linear_path, content=linear_raw, headers={"Linear-Signature": proof}
                )
            ).status_code == 200
            await post("knowledge.source.remove", name="projects", confirm="projects")
            assert (
                await client.post(
                    linear_path, content=linear_raw, headers={"Linear-Signature": proof}
                )
            ).status_code == 403

            channels: list[dict[str, Any]] = []
            monkeypatch.setattr(
                gdrive, "access_token", AsyncMock(return_value="read-only-access-token")
            )
            monkeypatch.setattr(
                gdrive_push, "member_drives", AsyncMock(return_value={"shared-drive"})
            )
            stop = AsyncMock()
            monkeypatch.setattr(gdrive_push, "stop", stop)

            async def watch(token: str, **body: Any) -> dict[str, Any]:
                channel = {
                    "id": body["channel_id"],
                    "token": body["channel_token"],
                    "resource_id": "resource-" + body["channel_id"],
                    "drive_id": body["drive_id"],
                    "expiration": time.time() + 3600,
                }
                channels.append(channel)
                # The provider can send sync before its watch response arrives.
                early_path = httpx.URL(body["address"]).raw_path.decode()
                early = await client.post(
                    early_path,
                    headers={
                        "X-Goog-Channel-ID": channel["id"],
                        "X-Goog-Channel-Token": channel["token"],
                        "X-Goog-Resource-ID": channel["resource_id"],
                        "X-Goog-Resource-State": "sync",
                    },
                )
                assert early.status_code == 200, early.text
                return channel

            monkeypatch.setattr(gdrive_push, "watch", watch)
            credential = json.dumps(
                {
                    "type": "service_account",
                    "client_email": "reader@example.test",
                    "private_key": "test-key",
                }
            )
            await post(
                "knowledge.source.add",
                name="folders",
                kind="gdrive",
                items=[{"type": "folder", "id": "1AbCdEfGhIjKlMnOp"}],
                credential=credential,
            )
            drive = await post("knowledge.source.push_configure", name="folders")
            assert drive["status"] == "active" and drive["channels"] == 2
            assert {c["drive_id"] for c in channels} == {"", "shared-drive"}
            assert (await post("knowledge.source.inspect", name="folders"))["sync"][
                "requested_at"
            ] == ""
            drive_path = httpx.URL(drive["callback_url"]).raw_path.decode()

            def drive_headers(channel: dict[str, Any]) -> dict[str, str]:
                return {
                    "X-Goog-Channel-ID": channel["id"],
                    "X-Goog-Channel-Token": channel["token"],
                    "X-Goog-Resource-ID": channel["resource_id"],
                    "X-Goog-Resource-State": "change",
                }

            old_headers = drive_headers(channels[0])
            assert (
                await client.post(
                    drive_path, headers={**old_headers, "X-Goog-Channel-Token": "wrong"}
                )
            ).status_code == 403
            assert (await client.post(drive_path, headers=old_headers)).status_code == 200
            assert (await post("knowledge.source.inspect", name="folders"))["sync"]["requested_at"]
            source_id = drive_path.split("/")[-2]
            registration = await push_store.get(org, namespace, source_id)
            assert registration is not None
            await renew_drive(registration)
            assert len(channels) == 4 and stop.await_count == 2
            assert (await client.post(drive_path, headers=old_headers)).status_code == 403
            assert (
                await client.post(drive_path, headers=drive_headers(channels[-1]))
            ).status_code == 200

            erased = await post(
                "erasure.request",
                scope_type="namespace",
                scope_value=namespace,
                requester="owner@example.com",
                reason="source push lifecycle check",
            )
            assert erased["status"] == "completed", erased
            assert (
                ch_client.query(
                    "SELECT count() AS n FROM loom.knowledge_source_push WHERE org = {org:String}",
                    parameters={"org": org},
                ).first_item["n"]
                == 0
            )
