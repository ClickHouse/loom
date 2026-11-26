import asyncio
import hashlib
import json
import time
import unittest
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.knowledge.app.mcp_worker_authority import (
    CurrentConnection,
    CurrentRead,
    ReaderEnrollment,
    ReaderStanding,
    RequestedResource,
)
from server.knowledge.app.mcp_worker_proof import confirm_stored_read
from server.knowledge.app.mcp_worker_read import WorkerReadChannel, WorkerReadRefused
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection, StoredCopyRevision
from server.knowledge.domain.mcp_worker_source import WORKER_REGISTRATION
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource


class TestCurrentRead(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.request = RequestedResource("org", "collection", "documents", "urn:exact:Résumé")
        self.standing: ReaderStanding | None = ReaderStanding("org", "alice", "collection")
        worker = uuid4()
        alice = ReaderConnection("alice", worker, uuid4(), 2, "a" * 64)
        bob = ReaderConnection("bob", worker, uuid4(), 5, "a" * 64)
        self.connections = {
            "alice": CurrentConnection("org", alice, True, "approved-realm"),
            "bob": CurrentConnection("org", bob, True, "approved-realm"),
        }
        self.config = {
            "version": 2,
            "worker_id": str(worker),
            "profile_digest": alice.profile_digest,
            "owner_connection_id": str(alice.connection_id),
            "owner_connection_generation": alice.connection_generation,
            "approval_generation": 3,
            "selection_generation": 1,
            "mode": "live",
            "reader_policy": "current_team_access",
        }
        self.row: KnowledgeSource | None = KnowledgeSource(
            org="org",
            namespace="collection",
            name="documents",
            kind="mcp",
            source_id=str(uuid4()),
            access=MIRRORED_ACCESS,
            credential_mode="worker",
            owner_subject="alice",
            connection_id=str(alice.connection_id),
            config={WORKER_REGISTRATION: json.dumps(self.config)},
            items=[{"id": self.request.resource_id, "type": "resource"}],
        )
        self.approval: int | None = 3
        self.approval_reads = []
        self.connection_reads: list[str] = []
        self.bob_enrollment: ReaderEnrollment | None = ReaderEnrollment(
            self.request.org,
            self.request.namespace,
            UUID(self.row.source_id),
            1,
            "bob",
            bob.connection_id,
            bob.connection_generation,
            4,
        )

    async def _standing(self, request: RequestedResource) -> ReaderStanding | None:
        return self.standing

    async def _source(self, org: str, namespace: str, name: str) -> KnowledgeSource | None:
        return self.row

    async def _connection(
        self, org: str, owner: str, worker_id: UUID, connection_id: UUID
    ) -> CurrentConnection | None:
        self.connection_reads.append(owner)
        return self.connections.get(owner)

    async def _enrollment(
        self, org: str, namespace: str, source_id: UUID, owner: str
    ) -> ReaderEnrollment | None:
        return self.bob_enrollment

    async def _approval(self, org, namespace, worker_id, profile, mode, team) -> int | None:
        self.approval_reads.append((mode, team))
        return self.approval

    def _authority(self) -> CurrentRead:
        return CurrentRead(
            self.request,
            self._standing,
            self._source,
            self._connection,
            self._enrollment,
            self._approval,
            "live",
        )

    async def test_grant_and_exact_selection_precede_approval_lookup(self) -> None:
        self.standing = None
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await self._authority().resolve()
        self.standing = ReaderStanding("org", "alice", "collection")
        self.request = replace(self.request, resource_id="urn:exact:Résumé")
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await self._authority().resolve()
        self.assertEqual(self.connection_reads, [])

    async def test_current_worker_approval_generation_cannot_restore_old_selection(self) -> None:
        read = self._authority()
        self.assertEqual((await read.resolve()).approval_generation, 3)
        for generation in (None, 5):
            with self.subTest(generation=generation):
                self.connection_reads.clear()
                self.approval = generation
                with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
                    await read.resolve()
                self.assertEqual(self.connection_reads, [])

    async def test_stored_access_checks_the_approved_storage_mode_and_team_scope(self) -> None:
        read = replace(self._authority(), purpose="stored")
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await read.resolve()
        assert self.row is not None
        self.row = replace(
            self.row, config={WORKER_REGISTRATION: json.dumps({**self.config, "mode": "snapshot"})}
        )
        self.standing = ReaderStanding("org", "bob", "collection")
        self.assertEqual((await read.resolve()).requester, "bob")
        self.assertEqual(self.approval_reads, [("snapshot", True)])

    async def test_team_uses_only_bobs_current_connection(self) -> None:
        self.standing = ReaderStanding("org", "bob", "collection")
        scope = await self._authority().resolve()
        self.assertEqual(scope.requester, "bob")
        self.assertEqual(scope.connection_id, self.connections["bob"].binding.connection_id)
        self.assertEqual(scope.connection_generation, 5)
        self.assertEqual(scope.enrollment_generation, 4)

    async def test_team_needs_an_exact_current_source_enrollment(self) -> None:
        self.standing = ReaderStanding("org", "bob", "collection")
        original = self.bob_enrollment
        assert original is not None
        for enrollment in (
            None,
            replace(original, connection_id=uuid4()),
            replace(original, selection_generation=2),
            replace(original, source_id=uuid4()),
            replace(original, requester="alice"),
        ):
            self.bob_enrollment = enrollment
            with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
                await self._authority().resolve()

    async def test_team_refuses_unknown_or_mismatched_resource_space(self) -> None:
        self.standing = ReaderStanding("org", "bob", "collection")
        for space in (None, "other-realm"):
            self.connections["bob"] = replace(self.connections["bob"], resource_space=space)
            with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
                await self._authority().resolve()

    async def test_owner_disconnect_and_generation_change_fence_existing_source(self) -> None:
        self.standing = ReaderStanding("org", "bob", "collection")
        authority = self._authority()
        await authority.resolve()
        prior = self.connections["alice"]
        for current in (
            replace(prior, active=False),
            replace(prior, binding=replace(prior.binding, connection_generation=3)),
        ):
            self.connections["alice"] = current
            for viewer, status in (("bob", "denied"), ("alice", "reconnect")):
                self.standing = ReaderStanding("org", viewer, "collection")
                with self.assertRaisesRegex(WorkerReadRefused, f"^{status}$"):
                    await authority.resolve()

    async def test_scope_changes_even_if_selection_returns_to_the_same_item(self) -> None:
        authority = self._authority()
        prior = await authority.resolve()
        assert self.row is not None
        self.row = replace(
            self.row,
            config={WORKER_REGISTRATION: json.dumps({**self.config, "selection_generation": 3})},
        )
        self.assertNotEqual(await authority.resolve(), prior)

    async def test_reenrolling_the_same_connection_changes_inflight_authority(self) -> None:
        self.standing = ReaderStanding("org", "bob", "collection")
        read = self._authority()
        before = await read.resolve()
        assert self.bob_enrollment is not None
        self.bob_enrollment = replace(self.bob_enrollment, link_generation=6)
        self.assertNotEqual(await read.resolve(), before)

    async def test_check_receipt_matches_current_reader_revision_and_content(self) -> None:
        self.standing = ReaderStanding("org", "bob", "collection")
        assert self.row is not None
        self.row = replace(
            self.row, config={WORKER_REGISTRATION: json.dumps({**self.config, "mode": "snapshot"})}
        )
        authority = replace(self._authority(), purpose="stored")
        scope = await authority.resolve()
        accepted = StoredCopyRevision(
            org=scope.org,
            namespace=scope.namespace,
            source_id=scope.source_id,
            selection_generation=scope.selection_generation,
            resource_id_sha256=hashlib.sha256(scope.resource_id.encode()).hexdigest(),
            revision_id=uuid4(),
            content_sha256=hashlib.sha256(b"copy").hexdigest(),
        )
        current = accepted
        reply_sha256 = accepted.content_sha256
        change_during_read = False
        signing_key = Ed25519PrivateKey.generate()

        async def answer(request: httpx.Request) -> httpx.Response:
            nonlocal current
            import jwt

            claims = jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                signing_key.public_key(),
                algorithms=["EdDSA"],
                audience=f"urn:loom:source-worker:{scope.worker_id}",
                issuer="loom",
            )
            assert claims["grant"]["binding"]["requester"] == "bob"
            assert claims["grant"]["operation"] == "check_access"
            assert request.url.path == "/v1/check-access"
            if change_during_read:
                current = replace(accepted, revision_id=uuid4())
            body = httpx.ByteStream(
                json.dumps(
                    {
                        "status": "ok",
                        "request_id": claims["grant"]["request_id"],
                        "content_sha256": reply_sha256,
                        "fetched_at": int(time.time()),
                        "document": None,
                    }
                ).encode()
            )
            return httpx.Response(200, stream=body, headers={"Content-Type": "application/json"})

        async def revision() -> StoredCopyRevision:
            return current

        channel = WorkerReadChannel(
            worker_id=scope.worker_id,
            origin="https://worker.example.test",
            issuer="loom",
            signing_key=signing_key,
            grant_seconds=5,
            max_response_bytes=4096,
            _transport=httpx.MockTransport(answer),
        )
        self.addAsyncCleanup(channel.close)
        proof = await confirm_stored_read(
            channel=channel, current=authority, revision=revision, max_age_seconds=15
        )
        self.assertEqual(proof.connection, self.connections["bob"].binding)
        self.assertEqual(proof.confirmed_revision, accepted)
        reply_sha256 = hashlib.sha256(b"other revision").hexdigest()
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await confirm_stored_read(
                channel=channel, current=authority, revision=revision, max_age_seconds=15
            )
        reply_sha256 = accepted.content_sha256
        change_during_read = True
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await confirm_stored_read(
                channel=channel, current=authority, revision=revision, max_age_seconds=15
            )

    async def _live_publication(
        self,
        *,
        revoke_for_publication: bool = False,
        unlink_during_body: bool = False,
        retire_during_body: bool = False,
        retire_during_delivery: bool = False,
        concurrent_publication: bool = False,
        settle_past_read_deadline: bool = False,
        retain_past_read_deadline: bool = False,
    ) -> tuple[list, list]:
        from unittest.mock import AsyncMock, patch

        import jwt

        from server.auth.context import set_current_auth
        from server.auth.jwt import AuthInfo
        from server.auth.session_protection import request_declaration
        from server.core import request_tail, request_work
        from server.knowledge.app import mcp_worker_protected as protected
        from server.operations.base import Role

        reader = self.connections["bob"].binding
        signing_key = Ed25519PrivateKey.generate()
        attempts = []
        published = []
        text = "synthetic live canary"

        async def answer(request: httpx.Request) -> httpx.Response:
            claims = jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                signing_key.public_key(),
                algorithms=["EdDSA"],
                audience=f"urn:loom:source-worker:{reader.worker_id}",
                issuer="loom",
            )
            grant = claims["grant"]
            attempts.append(grant)
            assert request.url.path == "/v1/live"
            payload = {
                "status": "ok",
                "request_id": grant["request_id"],
                "fetched_at": int(time.time()),
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "document": {
                    "resource_id": self.request.resource_id,
                    "title": "Document",
                    "text": text,
                    "revision": "fixture-1",
                    "source_link": "https://source.example/document",
                    "mime_type": "text/plain",
                },
            }
            return httpx.Response(
                200,
                stream=httpx.ByteStream(json.dumps(payload).encode()),
                headers={"Content-Type": "application/json"},
            )

        channel = WorkerReadChannel(
            worker_id=reader.worker_id,
            origin="https://worker.example.test",
            issuer="loom",
            signing_key=signing_key,
            grant_seconds=5,
            max_response_bytes=4096,
            _transport=httpx.MockTransport(answer),
        )
        self.addAsyncCleanup(channel.close)

        body = False
        first_body = asyncio.Event()
        second_claim = asyncio.Event()
        lease = asyncio.Lock()

        @asynccontextmanager
        async def source_lease(org, namespace, *, wait_s):
            from server.core.errors import OverloadedError

            assert (org, namespace) == ("org", "collection")
            if lease.locked():
                second_claim.set()
                if not wait_s:
                    raise OverloadedError("source lease busy")
            async with lease:
                yield

        async def connection(org, owner, worker_id, connection_id):
            record = await self._connection(org, owner, worker_id, connection_id)
            if body and unlink_during_body and owner == "bob":
                # The ordinary check already read the link and now sees a valid account.
                self.bob_enrollment = None
            if body and retire_during_body and owner == "bob" and not lease.locked():
                self.row = None
            return record

        async def fence_delivery(scope, deliver, deadline) -> None:
            assert lease.locked()
            assert scope.requester == "bob"
            assert deadline > asyncio.get_running_loop().time()
            link = await self._enrollment(
                scope.org, scope.namespace, scope.source_id, scope.requester
            )
            if link is None or link.link_generation != scope.enrollment_generation:
                raise WorkerReadRefused("denied")
            await deliver()

        directory = protected.LiveSourceDirectory(
            self._source,
            connection,
            self._enrollment,
            lambda _: channel,
            self._approval,
            fence_delivery,
        )
        auth = AuthInfo(
            subject="bob",
            org="org",
            namespaces=("collection",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )

        retain = protected._retain
        retention_started = False
        retention_wait_until = None

        async def mark_retention(*args) -> None:
            nonlocal retention_started
            retention_started = True
            await retain(*args)

        async def check_session(_auth) -> None:
            nonlocal retention_wait_until
            if retention_started and retention_wait_until is not None:
                wait_until, retention_wait_until = retention_wait_until, None
                await asyncio.sleep(max(0, wait_until - asyncio.get_running_loop().time()) + 0.01)

        async def downstream(scope, _receive, send) -> None:
            nonlocal body, retention_wait_until
            with request_declaration(scope), request_work.http_owner(scope):
                set_current_auth(auth)
                try:
                    request_work.begin()
                    loop = asyncio.get_running_loop()
                    short_read = settle_past_read_deadline or retain_past_read_deadline
                    deadline = loop.time() + (0.8 if short_read else 5)
                    if retain_past_read_deadline:
                        retention_wait_until = deadline
                    result = await protected.protected_live_read(
                        self.request, directory, deadline=deadline
                    )
                    assert result.text == text
                    if settle_past_read_deadline:
                        await asyncio.sleep(max(0, deadline - loop.time()) + 0.01)
                    if revoke_for_publication:
                        self.connections["bob"] = replace(self.connections["bob"], active=False)
                    await send({"type": "http.response.start", "status": 200, "headers": []})
                    body = True
                    await send(
                        {
                            "type": "http.response.body",
                            "body": json.dumps(asdict(result), default=str).encode(),
                        }
                    )
                finally:
                    set_current_auth(None)

        async def send(message) -> None:
            if message["type"] == "http.response.body" and retire_during_delivery:
                self.assertTrue(lease.locked())
            if concurrent_publication and message["type"] == "http.response.body":
                if not first_body.is_set():
                    first_body.set()
                    await second_claim.wait()
            published.append(message)

        middleware = request_tail.RequestTailMiddleware(downstream)

        async def publish() -> None:
            scope = {"type": "http", "headers": [(b"x-loom-session-protection", b"v1")]}
            await middleware(scope, None, send)

        with (
            patch.object(type(protected.settings), "auth_actually_required", return_value=True),
            patch.object(protected.settings, "knowledge_mirrored_sources_enabled", True),
            patch("server.knowledge.app.sources.sources_lease", source_lease),
            patch.object(protected, "_retain", mark_retention),
            patch.object(protected.session_admission, "require", check_session),
            patch.object(protected.authz, "row_in_scope", AsyncMock(return_value=True)),
            patch.object(
                protected.connected_accounts, "own_subject", AsyncMock(return_value=("org", "bob"))
            ),
        ):
            if revoke_for_publication or unlink_during_body or retire_during_body:
                with self.assertRaises(request_tail.RequiredTailError):
                    await publish()
            elif concurrent_publication:
                async with asyncio.TaskGroup() as group:
                    group.create_task(publish())
                    await first_body.wait()
                    group.create_task(publish())
            else:
                await publish()
        return attempts, published

    async def test_two_live_reads_deliver_while_sharing_the_source_lease(self) -> None:
        attempts, published = await self._live_publication(concurrent_publication=True)
        self.assertEqual(len(attempts), 2)
        bodies = [frame["body"] for frame in published if frame["type"] == "http.response.body"]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(b"synthetic live canary" in body for body in bodies))

    async def test_authorized_live_document_passes_publication_with_bobs_connection(self) -> None:
        attempts, published = await self._live_publication(retire_during_delivery=True)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["binding"]["requester"], "bob")
        self.assertEqual(
            attempts[0]["binding"]["connection_id"],
            str(self.connections["bob"].binding.connection_id),
        )
        self.assertIn(b"synthetic live canary", published[-1]["body"])

    async def test_revoked_live_document_is_withheld_even_after_the_worker_returns(self) -> None:
        attempts, published = await self._live_publication(revoke_for_publication=True)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(published, [])

    async def test_team_unlink_during_body_recheck_cannot_publish_document(self) -> None:
        attempts, published = await self._live_publication(unlink_during_body=True)
        self.assertEqual(len(attempts), 1)
        self.assertNotIn("source_owner", attempts[0]["binding"])
        self.assertIsNone(self.bob_enrollment)
        self.assertEqual([frame["type"] for frame in published], ["http.response.start"])

    async def test_source_retirement_after_ordinary_body_recheck_withholds_document(self) -> None:
        attempts, published = await self._live_publication(retire_during_body=True)
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(self.row)
        self.assertEqual([frame["type"] for frame in published], ["http.response.start"])

    async def test_live_document_remains_deliverable_after_read_budget_ends(self) -> None:
        attempts, published = await self._live_publication(settle_past_read_deadline=True)
        self.assertEqual(len(attempts), 1)
        self.assertIn(b"synthetic live canary", published[-1]["body"])

    async def test_initial_retention_can_finish_after_read_deadline(self) -> None:
        attempts, published = await self._live_publication(retain_past_read_deadline=True)
        self.assertEqual(len(attempts), 1)
        self.assertIn(b"synthetic live canary", published[-1]["body"])

    async def test_team_viewer_cannot_distinguish_selected_ids_without_current_authority(
        self,
    ) -> None:
        enrollment = self.bob_enrollment
        assert enrollment is not None
        for enrolled, outage in ((False, False), (False, True), (True, True)):
            with self.subTest(enrolled=enrolled, outage=outage):
                self.bob_enrollment = enrollment if enrolled else None
                await self._assert_team_refusal(enrolled=enrolled, outage=outage)

    async def _assert_team_refusal(self, *, enrolled: bool, outage: bool) -> None:
        from unittest.mock import AsyncMock, patch

        from server.auth.context import set_current_auth
        from server.auth.jwt import AuthInfo
        from server.auth.session_protection import request_declaration
        from server.core import request_tail, request_work
        from server.knowledge.api.models_source_live import KnowledgeSourceLiveInput
        from server.knowledge.app import mcp_worker_protected as protected
        from server.knowledge.app.mcp_worker_live_op import read_source_live
        from server.operations.base import Role

        worker = unittest.mock.Mock()
        approval = (
            AsyncMock(side_effect=WorkerReadRefused("unavailable"))
            if outage
            else AsyncMock(wraps=self._approval)
        )
        status = "unavailable" if enrolled else "denied"
        directory = protected.LiveSourceDirectory(
            self._source, self._connection, self._enrollment, worker, approval, AsyncMock()
        )
        auth = AuthInfo(
            subject="bob",
            org="org",
            namespaces=("collection",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
        scope = {"type": "http", "headers": [(b"x-loom-session-protection", b"v1")]}

        async def downstream(scope, _receive, _send) -> None:
            with request_declaration(scope), request_work.http_owner(scope):
                set_current_auth(auth)
                try:
                    request_work.begin()
                    for resource in ("urn:other", self.request.resource_id):
                        result = await read_source_live(
                            KnowledgeSourceLiveInput(
                                org="org",
                                namespace="collection",
                                source="documents",
                                resource_id=resource,
                            ),
                            directory,
                        )
                        self.assertEqual(result.model_dump(), {"status": status, "document": None})
                finally:
                    set_current_auth(None)

        with (
            patch.object(type(protected.settings), "auth_actually_required", return_value=True),
            patch.object(protected.settings, "knowledge_mirrored_sources_enabled", True),
            patch.object(protected.session_admission, "require", AsyncMock()),
            patch.object(protected.authz, "row_in_scope", AsyncMock(return_value=True)),
            patch.object(
                protected.connected_accounts, "own_subject", AsyncMock(return_value=("org", "bob"))
            ),
        ):
            await request_tail.RequestTailMiddleware(downstream)(scope, None, AsyncMock())
        self.assertEqual(approval.await_count, 2 if enrolled else 0)
        worker.assert_not_called()
