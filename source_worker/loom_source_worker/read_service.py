"""Transient delegated reads in the source worker."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol import ConnectionBinding, ReadBinding, ReadGrantRefused, verify_read_grant

FailureCode = Literal["denied", "reconnect", "overloaded", "timeout", "unavailable", "unsupported"]
CurrentAccess = Callable[[], Awaitable[None]]


class ReadFailure(Exception):
    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, repr=False)
class Document:
    resource_id: str
    title: str
    text: str
    revision: str | None = None
    source_link: str | None = None
    mime_type: str = "text/plain"


class BoundReader(Protocol):
    @property
    def binding(self) -> ConnectionBinding: ...

    async def read_exact(
        self, resource_id: str, *, authorize: CurrentAccess, deadline: float
    ) -> Document: ...


class ReadBackend(Protocol):
    async def resolve(self, binding: ReadBinding) -> BoundReader: ...

    async def current(self, binding: ReadBinding) -> ConnectionBinding: ...


@dataclass(frozen=True, repr=False)
class ReadResult:
    request_id: UUID
    fetched_at: int
    content_sha256: str
    document: Document | None


@dataclass(frozen=True)
class ReadLimits:
    concurrent: int
    concurrent_per_org: int
    timeout_seconds: float
    content_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.concurrent) is not int
            or type(self.concurrent_per_org) is not int
            or not 1 <= self.concurrent_per_org <= self.concurrent <= 256
            or type(self.content_bytes) is not int
            or not 1 <= self.content_bytes <= 4_000_000
            or type(self.timeout_seconds) not in (float, int)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 30
        ):
            raise ValueError("invalid worker read limits")


class ReadService:
    def __init__(
        self,
        *,
        worker_id: UUID,
        issuer: str,
        verifier: Ed25519PublicKey,
        backend: ReadBackend,
        limits: ReadLimits,
    ) -> None:
        self._worker_id = worker_id
        self._issuer = issuer
        self._verifier = verifier
        self._backend = backend
        self._limits = limits
        self._total = 0
        self._by_org: dict[str, int] = {}

    @asynccontextmanager
    async def _slot(self, org: str) -> AsyncIterator[None]:
        if (
            self._total >= self._limits.concurrent
            or self._by_org.get(org, 0) >= self._limits.concurrent_per_org
        ):
            raise ReadFailure("overloaded")
        self._total += 1
        self._by_org[org] = self._by_org.get(org, 0) + 1
        try:
            yield
        finally:
            self._total -= 1
            self._by_org[org] -= 1
            if not self._by_org[org]:
                del self._by_org[org]

    async def execute(self, token: str, operation: Literal["live", "check_access"]) -> ReadResult:
        try:
            grant, expires_at = verify_read_grant(
                token, issuer=self._issuer, worker_id=self._worker_id, key=self._verifier
            )
            if grant.operation != operation:
                raise ReadGrantRefused("operation binding refused")
            binding = grant.binding

            async def require_current() -> None:
                if time.time() >= expires_at:
                    raise ReadFailure("timeout")
                current = await self._backend.current(binding)
                try:
                    current.authorize(grant)
                except ReadGrantRefused:
                    raise ReadFailure("denied") from None
                if time.time() >= expires_at:
                    raise ReadFailure("timeout")

            deadline = asyncio.get_running_loop().time() + min(
                self._limits.timeout_seconds, expires_at - time.time()
            )
            async with self._slot(binding.org), asyncio.timeout_at(deadline):
                reader = await self._backend.resolve(binding)
                reader.binding.authorize(grant)
                await require_current()
                try:
                    document = await reader.read_exact(
                        binding.resource_id, authorize=require_current, deadline=deadline
                    )
                    fetched_at = int(time.time())
                    content_sha256 = self._check_document(binding, document)
                except ReadFailure as failure:
                    if failure.code == "unsupported":
                        await require_current()
                    raise
                await require_current()
                return ReadResult(
                    grant.request_id,
                    fetched_at,
                    content_sha256,
                    document if operation == "live" else None,
                )
        except TimeoutError:
            raise ReadFailure("timeout") from None
        except ReadGrantRefused:
            raise ReadFailure("denied") from None

    def _check_document(self, binding: ReadBinding, document: Document) -> str:
        if document.resource_id != binding.resource_id:
            raise ReadFailure("denied")
        if len(document.text) > self._limits.content_bytes:
            raise ReadFailure("unsupported")
        metadata = (
            (document.title, 4096),
            (document.revision, 2048),
            (document.source_link, 2048),
            (document.mime_type, 128),
        )
        for value, limit in metadata:
            if value is not None and not _fits_utf8(value, limit):
                raise ReadFailure("unsupported")
        try:
            body = document.text.encode("utf-8")
        except UnicodeError:
            raise ReadFailure("unsupported") from None
        if not body or len(body) > self._limits.content_bytes:
            raise ReadFailure("unsupported")
        return hashlib.sha256(body).hexdigest()


def _fits_utf8(value: str, maximum: int) -> bool:
    try:
        return len(value) <= maximum and len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False
