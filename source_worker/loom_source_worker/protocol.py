"""Caller and selection bindings for the transient worker read channel."""

from __future__ import annotations

import json
import time
from typing import Annotated, Literal
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

_Principal = Annotated[str, StringConstraints(min_length=1, max_length=255)]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Generation = Annotated[int, Field(ge=1, le=2**63 - 1)]
_Resource = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
_MAX_GRANT_BYTES = 24_576
_MAX_GRANT_SECONDS = 30


class ReadGrantRefused(Exception):
    """The worker cannot authorize this read."""


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, strict=True)


class ReadBinding(_Wire):
    worker_id: UUID
    org: _Principal
    namespace: _Principal
    source_id: UUID
    selection_generation: _Generation
    requester: _Principal
    connection_id: UUID
    connection_generation: _Generation
    profile_digest: _Digest
    resource_id: _Resource

    @field_validator("resource_id")
    @classmethod
    def _bound_resource_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 2048:
            raise ValueError("resource identity exceeds the byte limit")
        return value


class ReadGrant(_Wire):
    version: Literal[1] = 1
    request_id: UUID
    operation: Literal["live", "check_access"]
    binding: ReadBinding


class _GrantClaims(_Wire):
    iss: _Principal
    aud: str
    iat: int
    exp: int
    grant: ReadGrant


class ConnectionBinding(_Wire):
    worker_id: UUID
    org: _Principal
    owner: _Principal
    connection_id: UUID
    generation: _Generation
    profile_digest: _Digest

    def authorize(self, grant: ReadGrant) -> None:
        binding = grant.binding
        expected = (
            binding.worker_id,
            binding.org,
            binding.requester,
            binding.connection_id,
            binding.connection_generation,
            binding.profile_digest,
        )
        actual = (
            self.worker_id,
            self.org,
            self.owner,
            self.connection_id,
            self.generation,
            self.profile_digest,
        )
        if actual != expected:
            raise ReadGrantRefused("connection binding refused")


def _audience(worker_id: UUID) -> str:
    return f"urn:loom:source-worker:{worker_id}"


def issue_read_grant(
    grant: ReadGrant, *, issuer: str, key: Ed25519PrivateKey, lifetime_seconds: int
) -> str:
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= _MAX_GRANT_SECONDS:
        raise ValueError("invalid read grant lifetime")
    issued_at = int(time.time())
    claims = _GrantClaims(
        iss=issuer,
        aud=_audience(grant.binding.worker_id),
        iat=issued_at,
        exp=issued_at + lifetime_seconds,
        grant=grant,
    )
    signed = jwt.encode(claims.model_dump(mode="json"), key, algorithm="EdDSA")
    if len(signed) > _MAX_GRANT_BYTES:
        raise ValueError("read grant exceeds the byte limit")
    return signed


def verify_read_grant(
    token: str, *, issuer: str, worker_id: UUID, key: Ed25519PublicKey
) -> tuple[ReadGrant, int]:
    if not token or not token.isascii() or len(token) > _MAX_GRANT_BYTES:
        raise ReadGrantRefused("invalid read grant")
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            issuer=issuer,
            audience=_audience(worker_id),
            options={"require": ["iss", "aud", "iat", "exp", "grant"], "strict_aud": True},
        )
        claims = _GrantClaims.model_validate_json(json.dumps(payload))
        if (
            not 1 <= claims.exp - claims.iat <= _MAX_GRANT_SECONDS
            or claims.grant.binding.worker_id != worker_id
        ):
            raise ReadGrantRefused("invalid read grant")
    except (jwt.PyJWTError, ValueError, OverflowError, TypeError, RecursionError):
        raise ReadGrantRefused("invalid read grant") from None
    return claims.grant, claims.exp
