"""Own connections to deployment provisioned source workers, never their upstreams."""

from __future__ import annotations

import re
import ssl
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from server.knowledge.app.mcp_worker_read import WorkerReadChannel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from uuid import UUID

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

_MAX_WORKERS = 256
_MAX_PROFILES_PER_WORKER = 4096
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, repr=False)
class WorkerRoute:
    worker_id: UUID
    tls_origin: str
    tls: ssl.SSLContext
    issuer: str
    grant_key: Ed25519PrivateKey
    enrollment_verifier: Ed25519PublicKey | None
    profiles: frozenset[tuple[str, str]]
    max_live_requests: int

    def _validate(self) -> None:
        if not (
            self.worker_id.int
            and isinstance(self.tls, ssl.SSLContext)
            and self.tls.check_hostname
            and self.tls.verify_mode == ssl.CERT_REQUIRED
            and self.issuer.isascii()
            and 0 < len(self.issuer) <= 255
            and 0 < len(self.profiles) <= _MAX_PROFILES_PER_WORKER
            and all(
                0 < len(org) <= 255 and org != "*" and _DIGEST.fullmatch(digest)
                for org, digest in self.profiles
            )
            and (
                self.enrollment_verifier is None
                or self.enrollment_verifier.public_bytes_raw()
                != self.grant_key.public_key().public_bytes_raw()
            )
        ):
            raise ValueError("invalid source worker route")


@dataclass(frozen=True, repr=False)
class WorkerRoutes:
    _channels: Mapping[UUID, WorkerReadChannel]
    _profiles: Mapping[UUID, frozenset[tuple[str, str]]]

    def get(self, worker_id: UUID) -> WorkerReadChannel | None:
        return self._channels.get(worker_id)

    def permits(self, worker_id: UUID, org: str, profile_digest: str) -> bool:
        return (org, profile_digest) in self._profiles.get(worker_id, frozenset())


@asynccontextmanager
async def open_worker_routes(routes: tuple[WorkerRoute, ...]) -> AsyncIterator[WorkerRoutes]:
    """Create one bounded client pool per configured worker and close them on shutdown."""
    if len(routes) > _MAX_WORKERS or len({route.worker_id for route in routes}) != len(routes):
        raise ValueError("invalid source worker route count")
    for route in routes:
        route._validate()
    async with AsyncExitStack() as stack:
        channels = {}
        profiles = {}
        for route in routes:
            channel = WorkerReadChannel(
                worker_id=route.worker_id,
                origin=route.tls_origin,
                issuer=route.issuer,
                signing_key=route.grant_key,
                grant_seconds=8,
                max_response_bytes=25_000_000,
                max_inflight=route.max_live_requests,
                tls=route.tls,
            )
            stack.push_async_callback(channel.close)
            channels[route.worker_id] = channel
            profiles[route.worker_id] = route.profiles
        yield WorkerRoutes(MappingProxyType(channels), MappingProxyType(profiles))
