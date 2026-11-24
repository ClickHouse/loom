import asyncio
import hashlib
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from loom_source_worker.protocol import (
    ConnectionBinding,
    ReadBinding,
    ReadGrant,
    issue_read_grant,
)
from loom_source_worker.read_service import (
    BoundReader,
    CurrentAccess,
    Document,
    ReadFailure,
    ReadLimits,
    ReadService,
)


class _Backend:
    def __init__(self, binding: ConnectionBinding) -> None:
        self.binding = binding
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.return_identity: str | None = None
        self.payload = "actualité"
        self.calls: list[str] = []

    async def resolve(self, binding: ReadBinding) -> BoundReader:
        return self

    async def current(self, binding: ReadBinding) -> ConnectionBinding:
        return self.binding

    async def read_exact(self, resource_id: str, *, authorize: CurrentAccess) -> Document:
        await authorize()
        self.calls.append(resource_id)
        self.started.set()
        await self.release.wait()
        return Document(self.return_identity or resource_id, "Title", self.payload)


class TestReadService(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.binding = ReadBinding(
            worker_id=uuid4(),
            org="org",
            namespace="team",
            source_id=uuid4(),
            selection_generation=2,
            requester="alice",
            connection_id=uuid4(),
            connection_generation=1,
            profile_digest="d" * 64,
            resource_id="urn:resource:exact",
        )
        self.backend = _Backend(
            ConnectionBinding(
                worker_id=self.binding.worker_id,
                org=self.binding.org,
                owner=self.binding.requester,
                connection_id=self.binding.connection_id,
                generation=self.binding.connection_generation,
                profile_digest=self.binding.profile_digest,
            )
        )

    def _service(self, *, timeout: float = 1) -> ReadService:
        return ReadService(
            worker_id=self.binding.worker_id,
            issuer="loom",
            verifier=self.key.public_key(),
            backend=self.backend,
            limits=ReadLimits(
                concurrent=2, concurrent_per_org=1, timeout_seconds=timeout, content_bytes=128
            ),
        )

    def _token(self, grant: ReadGrant) -> str:
        return issue_read_grant(grant, issuer="loom", key=self.key, lifetime_seconds=10)

    def _live(self) -> ReadGrant:
        return ReadGrant(request_id=uuid4(), operation="live", binding=self.binding)

    async def test_live_and_access_check_are_separate(self) -> None:
        service = self._service()
        live = self._live()
        proof = ReadGrant(request_id=uuid4(), operation="check_access", binding=self.binding)
        result = await service.execute(self._token(live), "live")
        checked = await service.execute(self._token(proof), "check_access")
        self.assertEqual(result.request_id, live.request_id)
        self.assertEqual(result.document, Document(self.binding.resource_id, "Title", "actualité"))
        self.assertIsNone(checked.document)
        self.assertEqual(checked.content_sha256, hashlib.sha256("actualité".encode()).hexdigest())
        with self.assertRaisesRegex(ReadFailure, "^denied$"):
            await service.execute(self._token(proof), "live")
        self.assertEqual(self.backend.calls, [self.binding.resource_id] * 2)

    async def test_other_account_cannot_reuse_a_team_connection(self) -> None:
        bob = self.binding.model_copy(update={"requester": "bob"})
        grant = ReadGrant(request_id=uuid4(), operation="live", binding=bob)
        with self.assertRaisesRegex(ReadFailure, "^denied$"):
            await self._service().execute(self._token(grant), "live")
        self.assertEqual(self.backend.calls, [])

    async def test_revocation_before_response_refuses_data(self) -> None:
        self.backend.release.clear()
        service = self._service()
        read = asyncio.create_task(service.execute(self._token(self._live()), "live"))
        await self.backend.started.wait()
        self.backend.binding = self.backend.binding.model_copy(update={"generation": 2})
        self.backend.release.set()
        with self.assertRaisesRegex(ReadFailure, "^denied$"):
            await read

    async def test_wrong_resource_and_large_utf8_body_are_never_returned(self) -> None:
        service = self._service()
        self.backend.return_identity = "urn:another-document"
        with self.assertRaisesRegex(ReadFailure, "^denied$"):
            await service.execute(self._token(self._live()), "live")
        self.backend.return_identity = None
        self.backend.payload = "é" * 65
        with self.assertRaisesRegex(ReadFailure, "^unsupported$"):
            await service.execute(self._token(self._live()), "live")

    async def test_saturation_and_cancellation_release_the_live_slot(self) -> None:
        self.backend.release.clear()
        service = self._service()
        read = asyncio.create_task(service.execute(self._token(self._live()), "live"))
        await self.backend.started.wait()
        with self.assertRaisesRegex(ReadFailure, "^overloaded$"):
            await service.execute(self._token(self._live()), "live")
        read.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await read
        self.backend.release.set()
        self.assertIsNotNone((await service.execute(self._token(self._live()), "live")).document)

    async def test_stalled_upstream_has_a_distinct_timeout(self) -> None:
        self.backend.release.clear()
        with self.assertRaisesRegex(ReadFailure, "^timeout$"):
            await self._service(timeout=0.03).execute(self._token(self._live()), "live")
