import json
import time
import unittest
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
from loom_source_worker.credential_store import VaultCredentialStore
from loom_source_worker.protocol import ConnectionBinding
from loom_source_worker.read_service import ReadFailure
from pydantic import SecretStr


class _Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


class TestCredentialStore(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.binding = ConnectionBinding(
            worker_id=uuid4(),
            org="one",
            owner="alice",
            connection_id=uuid4(),
            generation=2,
            profile_digest="a" * 64,
        )
        self.seen: list[httpx.Request] = []
        self.status = 200
        self.body_override: bytes | None = None
        self.record = {
            "binding": self.binding.model_dump(mode="json"),
            "state": "active",
            "auth": "bearer",
            "audience": "https://source.example.test/mcp",
            "token": "synthetic-source",
            "expires_at": int(time.time()) + 60,
        }
        self.metadata = {"version": 2, "destroyed": False, "deletion_time": ""}

    async def _reply(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        body = self.body_override
        if body is None:
            body = json.dumps({"data": {"data": self.record, "metadata": self.metadata}}).encode()
        return httpx.Response(
            self.status,
            headers={"Location": "https://other.example.test"},
            stream=_Body(body),
        )

    def _store(self, origin: str = "https://backend.example.test") -> VaultCredentialStore:
        return VaultCredentialStore(
            origin=origin,
            mount="loom-sources",
            worker_id=self.binding.worker_id,
            token=lambda: SecretStr("synthetic-backend"),
            timeout_seconds=1,
            _transport=httpx.MockTransport(self._reply),
        )

    async def test_reads_only_configured_worker_and_exact_connection_path(self) -> None:
        store = self._store()
        self.addAsyncCleanup(store.aclose)
        credential = await store.get(self.binding.connection_id)
        self.assertEqual(credential.binding, self.binding)
        self.assertNotIn("synthetic-source", repr(credential))
        [request] = self.seen
        self.assertEqual(request.headers["X-Vault-Token"], "synthetic-backend")
        pool = f"/v1/loom-sources/data/{self.binding.worker_id}"
        self.assertEqual(request.url.path, f"{pool}/{self.binding.connection_id}")
        self.assertEqual(request.url.host, "backend.example.test")
        self.assertNotIn("synthetic-source", str(request.url))

    async def test_refuses_old_versions_mismatched_ids_and_deletion(self) -> None:
        store = self._store()
        self.addAsyncCleanup(store.aclose)
        for metadata_patch in ({"version": 3}, {"destroyed": True}, {"deletion_time": "deleted"}):
            original = self.metadata
            self.metadata = {**original, **metadata_patch}
            with self.assertRaisesRegex(ReadFailure, "^denied$"):
                await store.get(self.binding.connection_id)
            self.metadata = original
        with self.assertRaisesRegex(ReadFailure, "^denied$"):
            await store.get(uuid4())

    async def test_distinguishes_expiry_denial_and_backend_outage(self) -> None:
        store = self._store()
        self.addAsyncCleanup(store.aclose)
        self.record["expires_at"] = int(time.time()) - 1
        with self.assertRaisesRegex(ReadFailure, "^reconnect$"):
            await store.get(self.binding.connection_id)
        for status, expected in ((403, "denied"), (404, "reconnect"), (503, "unavailable")):
            self.status = status
            with self.assertRaisesRegex(ReadFailure, f"^{expected}$"):
                await store.get(self.binding.connection_id)

    async def test_rejects_redirect_or_unbounded_secret_envelope(self) -> None:
        store = self._store()
        self.addAsyncCleanup(store.aclose)
        self.status = 302
        with self.assertRaisesRegex(ReadFailure, "^unavailable$"):
            await store.get(self.binding.connection_id)
        self.assertEqual(len(self.seen), 1)
        self.status = 200
        self.body_override = b" " * 32_769
        with self.assertRaisesRegex(ReadFailure, "^unavailable$"):
            await store.get(self.binding.connection_id)

    def test_requires_exact_tls_origin_without_auth_or_query(self) -> None:
        for origin in (
            "http://backend.example.test",
            "https://privileged@backend.example.test",
            "https://backend.example.test/prefix",
            "https://backend.example.test?scope=other",
        ):
            with self.assertRaisesRegex(ValueError, "invalid credential backend"):
                self._store(origin)
