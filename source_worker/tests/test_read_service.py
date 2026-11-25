import asyncio
import hashlib
import unittest
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from loom_source_worker.http_service import create_app
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

    async def read_exact(
        self, resource_id: str, *, authorize: CurrentAccess, deadline: float
    ) -> Document:
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

    async def test_revocation_precedes_output_limit_refusal(self) -> None:
        self.backend.payload = "é" * 65
        service = self._service()
        with self.assertRaisesRegex(ReadFailure, "^unsupported$"):
            await service.execute(self._token(self._live()), "live")

        self.backend.release.clear()
        self.backend.started.clear()
        read = asyncio.create_task(service.execute(self._token(self._live()), "live"))
        try:
            await self.backend.started.wait()
            self.backend.binding = self.backend.binding.model_copy(update={"generation": 2})
        finally:
            self.backend.release.set()
        with self.assertRaisesRegex(ReadFailure, "^denied$"):
            await read
        self.assertEqual(self.backend.calls, [self.binding.resource_id] * 2)

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

    async def test_http_body_backpressure_holds_the_org_slot(self) -> None:
        app = create_app(self._service(timeout=3))
        sending = asyncio.Event()
        drain = asyncio.Event()

        async def blocked_app(scope, receive, send):
            async def send_when_ready(message):
                if message["type"] == "http.response.body":
                    sending.set()
                    await drain.wait()
                await send(message)

            await app(scope, receive, send_when_ready)

        def headers():
            return {"Authorization": "Bearer " + self._token(self._live())}

        async with (
            asyncio.timeout(2),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=blocked_app), base_url="http://worker.test"
            ) as slow,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://worker.test"
            ) as peer,
        ):
            first = asyncio.create_task(slow.post("/v1/live", headers=headers()))
            try:
                await sending.wait()
                refused = await peer.post("/v1/live", headers=headers())
                self.assertEqual(
                    (refused.status_code, refused.json()), (429, {"status": "overloaded"})
                )
                self.assertEqual(len(self.backend.calls), 1)
            finally:
                drain.set()
                self.assertEqual((await first).status_code, 200)
            self.assertEqual((await peer.post("/v1/live", headers=headers())).status_code, 200)

    async def test_partial_response_deadline_and_disconnect_release_the_slot(self) -> None:
        for reason in ("deadline", "disconnect"):
            with self.subTest(reason=reason):
                await self._assert_partial_response_releases_slot(reason)

    async def _assert_partial_response_releases_slot(self, reason: str) -> None:
        service = self._service(timeout=0.15 if reason == "deadline" else 3)
        app = create_app(service)
        sending = asyncio.Event()
        disconnected = asyncio.Event()
        cancelled = asyncio.Event()
        frames = []

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                sending.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            frames.append(message["type"])

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/live",
            "query_string": b"",
            "headers": [(b"authorization", ("Bearer " + self._token(self._live())).encode())],
        }
        async with asyncio.timeout(2):
            response = asyncio.create_task(app(scope, receive, send))
            await sending.wait()
            if reason == "disconnect":
                disconnected.set()
            await response
        self.assertTrue(cancelled.is_set())
        self.assertEqual(frames, ["http.response.start"])
        self.assertIsNotNone((await service.execute(self._token(self._live()), "live")).document)

    async def test_stalled_upstream_has_a_distinct_timeout(self) -> None:
        self.backend.release.clear()
        with self.assertRaisesRegex(ReadFailure, "^timeout$"):
            await self._service(timeout=0.03).execute(self._token(self._live()), "live")
