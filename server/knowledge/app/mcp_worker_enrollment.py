"""Verify an approved host's credential-free account enrollment statement."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from server.core.diagnostics import private_work
from server.knowledge.app.mcp_worker_authority import CurrentConnection
from server.knowledge.app.mcp_worker_read import WorkerReadRefused
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection

_Identity = Annotated[str, StringConstraints(min_length=1, max_length=255)]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Counter = Annotated[int, Field(ge=1, le=2**63 - 1)]
_MAX_LIFETIME = 120


class EnrollmentRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("source connection enrollment unavailable")


@dataclass(frozen=True, repr=False)
class EnrollmentChallenge:
    """Resolved from Loom's authenticated account and current worker approval."""

    org: str
    requester: str
    worker_id: UUID
    profile_digest: str
    nonce: UUID
    issued_at: int
    expires_at: int
    audience: str
    connection_id: UUID
    connection_generation: int


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    org: _Identity = Field(strict=True)
    requester: _Identity = Field(strict=True)
    worker_id: UUID
    profile_digest: _Digest = Field(strict=True)
    nonce: UUID
    connection_id: UUID
    generation: _Counter = Field(strict=True)
    resource_space: _Digest | None = Field(default=None, strict=True)


def issue_enrollment_grant(
    challenge: EnrollmentChallenge, *, issuer: str, key: Ed25519PrivateKey
) -> str:
    """The caller holds current approval for the challenge's authenticated owner."""
    now = int(time.time())
    if not (
        challenge.issued_at <= now < challenge.expires_at
        and 0 < challenge.expires_at - challenge.issued_at <= _MAX_LIFETIME
        and challenge.requester != "-"
        and challenge.worker_id.int
        and challenge.nonce.int
        and challenge.connection_id.int
        and 1 <= len(issuer) <= 255
        and 1 <= len(challenge.audience) <= 255
    ):
        raise EnrollmentRefused()
    with private_work():
        try:
            binding = _Binding(
                org=challenge.org,
                requester=challenge.requester,
                worker_id=challenge.worker_id,
                profile_digest=challenge.profile_digest,
                nonce=challenge.nonce,
                connection_id=challenge.connection_id,
                generation=challenge.connection_generation,
            )
            return jwt.encode(
                {
                    "iss": issuer,
                    "aud": challenge.audience,
                    "iat": now,
                    "exp": min(challenge.expires_at, now + 30),
                    "operation": "enroll",
                    "binding": binding.model_dump(mode="json", exclude_none=True),
                },
                key,
                algorithm="EdDSA",
            )
        except (ValidationError, jwt.PyJWTError):
            raise EnrollmentRefused() from None


def verify_enrollment(
    challenge: EnrollmentChallenge, statement: str, *, verifier: Ed25519PublicKey
) -> CurrentConnection:
    """A matching statement attests metadata; the caller consumes the challenge once."""
    with private_work():
        if not statement.isascii() or not 1 <= len(statement) <= 8192:
            raise EnrollmentRefused()
        try:
            claims = jwt.decode(
                statement,
                verifier,
                algorithms=["EdDSA"],
                issuer=f"urn:loom:source-worker:{challenge.worker_id}",
                audience=challenge.audience,
                options={"require": ["iss", "aud", "iat", "exp", "binding"], "strict_aud": True},
            )
            if set(claims) != {"iss", "aud", "iat", "exp", "binding"}:
                raise EnrollmentRefused()
            binding = _Binding.model_validate(claims["binding"])
            issued, expires, now = claims["iat"], claims["exp"], int(time.time())
            if not (
                type(issued) is type(expires) is int
                and challenge.issued_at <= issued <= now < expires <= challenge.expires_at
                and 0 < challenge.expires_at - challenge.issued_at <= _MAX_LIFETIME
                and challenge.requester != "-"
                and challenge.worker_id.int != 0
                and challenge.nonce.int != 0
                and binding.connection_id.int != 0
                and binding.connection_id == challenge.connection_id
                and binding.generation == challenge.connection_generation
                and (
                    binding.org,
                    binding.requester,
                    binding.worker_id,
                    binding.profile_digest,
                    binding.nonce,
                )
                == (
                    challenge.org,
                    challenge.requester,
                    challenge.worker_id,
                    challenge.profile_digest,
                    challenge.nonce,
                )
            ):
                raise EnrollmentRefused()
        except (jwt.PyJWTError, ValidationError, TypeError, RecursionError):
            raise EnrollmentRefused() from None
        return CurrentConnection(
            org=binding.org,
            binding=ReaderConnection(
                binding.requester,
                binding.worker_id,
                binding.connection_id,
                binding.generation,
                binding.profile_digest,
            ),
            active=True,
            resource_space=binding.resource_space,
        )


def check_enrollment_write(previous: CurrentConnection | None, proposed: CurrentConnection) -> bool:
    """Decide under the challenge and connection locks; return true for a new version."""
    if proposed.active is not True:
        raise EnrollmentRefused()
    if previous is None:
        return True
    before, after = previous.binding, proposed.binding
    if (previous.org, previous.resource_space) != (proposed.org, proposed.resource_space) or (
        before.worker_id,
        before.owner,
        before.connection_id,
        before.profile_digest,
    ) != (after.worker_id, after.owner, after.connection_id, after.profile_digest):
        raise EnrollmentRefused()
    if after.connection_generation <= before.connection_generation:
        if proposed == previous:
            return False
        raise EnrollmentRefused()
    return True


class _EnrollmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    statement: Annotated[str, StringConstraints(min_length=1, max_length=8192)] = Field(repr=False)


async def enroll_worker_connection(
    challenge: EnrollmentChallenge,
    *,
    issuer: str,
    key: Ed25519PrivateKey,
    verifier: Ed25519PublicKey,
    require_current: Callable[[], Awaitable[EnrollmentChallenge | None]],
    request_statement: Callable[[UUID, str], Awaitable[bytearray]],
    consume: Callable[[EnrollmentChallenge, CurrentConnection], Awaitable[None]],
    deadline: float,
) -> CurrentConnection:
    """Consume a verified challenge under durable approval and account locks."""
    if key.public_key().public_bytes_raw() == verifier.public_bytes_raw():
        raise EnrollmentRefused()
    with private_work():
        try:
            async with asyncio.timeout_at(deadline):
                if await require_current() != challenge:
                    raise EnrollmentRefused()
                grant = issue_enrollment_grant(challenge, issuer=issuer, key=key)
                response = await request_statement(challenge.worker_id, grant)
                if len(response) > 8192 + 64 or await require_current() != challenge:
                    raise EnrollmentRefused()
                statement = _EnrollmentResponse.model_validate_json(response).statement
                connection = verify_enrollment(challenge, statement, verifier=verifier)
                await consume(challenge, connection)
                return connection
        except (ValidationError, WorkerReadRefused, TimeoutError):
            raise EnrollmentRefused() from None
