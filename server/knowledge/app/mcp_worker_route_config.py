"""Load the operator-managed allowlist for remote source workers."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Annotated
from uuid import UUID

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from server.knowledge.app.mcp_worker_routes import WorkerRoute

_MAX_CONFIG_BYTES = 262_144
_MAX_KEY_BYTES = 8192
_Org = Annotated[str, StringConstraints(min_length=1, max_length=255)]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Digests = Annotated[list[_Digest], Field(min_length=1, max_length=4096)]


class _WorkerFile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)

    worker_id: UUID
    origin: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    issuer: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    trust_ca_file: Path
    client_certificate_file: Path
    client_key_file: Path
    grant_key_file: Path
    enrollment_verifier_file: Path | None = None
    profiles: Annotated[dict[_Org, _Digests], Field(min_length=1, max_length=256)]
    max_live_requests: Annotated[int, Field(ge=1, le=64)] = 8


class _RoutesFile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)

    version: Annotated[int, Field(ge=1, le=1)]
    workers: Annotated[list[_WorkerFile], Field(max_length=256)]


def _read_file(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError("invalid source worker file size")
    return data


def _grant_key(path: Path) -> Ed25519PrivateKey:
    key = load_pem_private_key(_read_file(path, _MAX_KEY_BYTES), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("invalid source worker key type")
    return key


def _verifier(path: Path) -> Ed25519PublicKey:
    key = load_pem_public_key(_read_file(path, _MAX_KEY_BYTES))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("invalid source worker key type")
    return key


def _build(root: Path, worker: _WorkerFile) -> WorkerRoute:
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_verify_locations(cafile=root / worker.trust_ca_file)
    tls.load_cert_chain(
        root / worker.client_certificate_file, root / worker.client_key_file, password=lambda: b""
    )
    enrollment = (
        _verifier(root / worker.enrollment_verifier_file)
        if worker.enrollment_verifier_file is not None
        else None
    )
    return WorkerRoute(
        worker.worker_id,
        worker.origin,
        tls,
        worker.issuer,
        _grant_key(root / worker.grant_key_file),
        enrollment,
        frozenset((org, digest) for org, digests in worker.profiles.items() for digest in digests),
        worker.max_live_requests,
    )


def load_worker_routes(path: Path) -> tuple[WorkerRoute, ...]:
    try:
        config = _RoutesFile.model_validate_json(_read_file(path, _MAX_CONFIG_BYTES))
        return tuple(_build(path.parent, worker) for worker in config.workers)
    except (OSError, ValidationError, ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("invalid source worker route configuration") from None
