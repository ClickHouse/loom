import hashlib
import json
import time
import unittest
from dataclasses import replace
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
        for enrollment, refusal in (
            (None, "reconnect"),
            (replace(original, connection_id=uuid4()), "reconnect"),
            (replace(original, selection_generation=2), "reconnect"),
            (replace(original, source_id=uuid4()), "denied"),
            (replace(original, requester="alice"), "denied"),
        ):
            self.bob_enrollment = enrollment
            with self.assertRaisesRegex(WorkerReadRefused, f"^{refusal}$"):
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
            with self.assertRaisesRegex(WorkerReadRefused, "^reconnect$"):
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
