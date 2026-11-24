import asyncio
import hashlib
import json
import ssl
import unittest
from dataclasses import replace
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.knowledge.app.mcp_worker_authority import RequestedResource
from server.knowledge.app.mcp_worker_live import _display_link, read_selected_live
from server.knowledge.app.mcp_worker_protected import LiveSourceDirectory, protected_live_read
from server.knowledge.app.mcp_worker_read import (
    WorkerReadChannel,
    WorkerReadRefused,
    WorkerReadScope,
)


class TestWorkerReadChannel(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.scope = WorkerReadScope(
            worker_id=uuid4(),
            org="org",
            namespace="team",
            source_id=uuid4(),
            selection_generation=3,
            requester="alice",
            connection_id=uuid4(),
            connection_generation=2,
            profile_digest="e" * 64,
            approval_generation=4,
            resource_id="urn:source:Résumé",
        )

    async def _wire_fixture(self, request: httpx.Request) -> httpx.Response:
        grant = jwt.decode(
            request.headers["authorization"].removeprefix("Bearer "),
            self.key.public_key(),
            algorithms=["EdDSA"],
            audience=f"urn:loom:source-worker:{self.scope.worker_id}",
            issuer="loom",
        )
        claims = grant["grant"]
        binding = claims["binding"]
        self.assertEqual(claims["version"], 1)
        self.assertEqual(request.method, "POST")
        self.assertEqual(binding["org"], "org")
        self.assertEqual(binding["namespace"], "team")
        self.assertEqual(binding["connection_id"], str(self.scope.connection_id))
        if binding["requester"] != "alice":
            return httpx.Response(403)
        if request.url.path == "/v1/live":
            self.assertEqual(claims["operation"], "live")
            document = {
                "resource_id": binding["resource_id"],
                "title": "Unpublished",
                "text": "alpha",
                "revision": None,
                "source_link": None,
                "mime_type": "text/plain",
            }
        else:
            self.assertEqual(
                (request.url.path, claims["operation"]), ("/v1/check-access", "check_access")
            )
            document = None

        async def wire():
            yield json.dumps(
                {
                    "status": "ok",
                    "request_id": claims["request_id"],
                    "fetched_at": grant["iat"],
                    "content_sha256": hashlib.sha256(b"alpha").hexdigest(),
                    "document": document,
                }
            ).encode()

        return httpx.Response(200, headers={"Content-Type": "application/json"}, content=wire())

    def _channel(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_inflight: int = 8,
        tls: ssl.SSLContext | None = None,
    ) -> WorkerReadChannel:
        if transport is None:
            transport = httpx.MockTransport(self._wire_fixture)
        channel = WorkerReadChannel(
            worker_id=self.scope.worker_id,
            origin="https://source-worker.example.test",
            issuer="loom",
            signing_key=self.key,
            grant_seconds=5,
            max_response_bytes=8192,
            max_inflight=max_inflight,
            tls=tls,
            _transport=transport,
        )
        self.addAsyncCleanup(channel.close)
        return channel

    async def _authorized(self) -> WorkerReadScope:
        return self.scope

    async def test_channel_accepts_checked_live_and_proof_wire(self) -> None:
        channel = self._channel()
        result = await channel.run(self.scope, "live", require_current=self._authorized)
        self.assertIsNotNone(result.document)
        assert result.document is not None
        self.assertEqual(result.document.resource_id, self.scope.resource_id)
        self.assertEqual(result.document.text, "alpha")
        proof = await channel.run(self.scope, "check_access", require_current=self._authorized)
        self.assertIsNone(proof.document)
        self.assertEqual(proof.content_sha256, result.content_sha256)

    async def test_revoked_core_scope_blocks_a_successful_worker_reply(self) -> None:
        validations = 0

        async def still_current() -> WorkerReadScope:
            nonlocal validations
            validations += 1
            return self.scope if validations == 1 else replace(self.scope, selection_generation=4)

        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await self._channel().run(self.scope, "live", require_current=still_current)
        self.assertEqual(validations, 2)

    async def test_different_requester_cannot_use_alices_connection(self) -> None:
        self.scope = replace(self.scope, requester="bob")
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await self._channel().run(self.scope, "live", require_current=self._authorized)

    async def test_live_publication_rechecks_selection_after_fetch(self) -> None:
        channel = self._channel()
        tail = []
        route = channel

        async def retain(validate, deadline) -> None:
            await validate()
            tail.append(validate)

        result = await read_selected_live(
            resolve=self._authorized,
            worker=lambda worker_id: route if worker_id == self.scope.worker_id else None,
            retain=retain,
            deadline=asyncio.get_running_loop().time() + 2,
        )
        self.assertEqual(result.text, "alpha")
        self.assertEqual(result.resource_id, self.scope.resource_id)
        self.assertTrue(result.uri.endswith("#" + str(self.scope.source_id)))
        self.assertGreater(result.fetched_at, 0)
        self.assertEqual(len(tail), 1)
        route = self._channel()
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await tail[0]()
        route = channel
        self.scope = replace(self.scope, selection_generation=4)
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await tail[0]()

    async def test_unprotected_request_cannot_look_up_any_worker_or_source(self) -> None:
        def refuse(*args):
            self.fail("directory was used by an unprotected request")

        directory = LiveSourceDirectory(refuse, refuse, refuse, refuse, refuse)
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await protected_live_read(
                RequestedResource("org", "team", "source", self.scope.resource_id),
                directory,
                deadline=asyncio.get_running_loop().time() + 2,
            )

    def test_only_https_document_links_are_rendered_as_links(self) -> None:
        url = "https://source.example.test/document#section"
        self.assertEqual(_display_link(url), url)
        for untrusted in ("urn:ref", "https://name@source.example.test/doc", "https://[invalid"):
            self.assertIsNone(_display_link(untrusted))

    async def test_refuses_worker_redirect_without_forwarding_grant(self) -> None:
        seen = []

        async def redirect(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            return httpx.Response(307, headers={"Location": "https://upstream.example.test/mcp"})

        channel = self._channel(httpx.MockTransport(redirect))
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await channel.run(self.scope, "live", require_current=self._authorized)
        self.assertEqual(seen, ["source-worker.example.test"])

    async def test_live_capacity_refuses_without_opening_another_request(self) -> None:
        started = asyncio.Event()

        async def wait_for_cancel(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Future[None]()
            raise AssertionError("waiting request was not cancelled")

        channel = self._channel(httpx.MockTransport(wait_for_cancel), max_inflight=1)
        blocked = asyncio.create_task(
            channel.run(self.scope, "live", require_current=self._authorized)
        )
        await started.wait()
        try:
            with self.assertRaisesRegex(WorkerReadRefused, "^overloaded$"):
                await channel.run(self.scope, "live", require_current=self._authorized)
        finally:
            blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked

    def test_cannot_disable_tls_peer_verification(self) -> None:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.check_hostname = False
        with self.assertRaisesRegex(ValueError, "TLS verification is required"):
            self._channel(tls=tls)
