"""Call an approved worker without resolving an upstream connection in Loom."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from server.core.diagnostics import private_work
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection

WorkerOperation = Literal["live", "check_access"]
WorkerFailure = Literal[
    "denied", "reconnect", "overloaded", "timeout", "unavailable", "unsupported"
]
_ENROLLMENT_PATH = "/v1/enroll"
_MAX_ENROLLMENT_BODY = 8192 + 64
_ENROLLMENT_CAPACITY = 2
_Paths: dict[WorkerOperation, str] = {"live": "/v1/live", "check_access": "/v1/check-access"}
_Statuses: dict[int, WorkerFailure] = {
    401: "denied",
    403: "denied",
    409: "reconnect",
    422: "unsupported",
    429: "overloaded",
    503: "unavailable",
    504: "timeout",
}


class WorkerReadRefused(Exception):
    def __init__(self, reason: WorkerFailure) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, repr=False)
class WorkerReadScope:
    worker_id: UUID
    org: str
    namespace: str
    source_id: UUID
    selection_generation: int
    requester: str
    connection_id: UUID
    connection_generation: int
    profile_digest: str
    approval_generation: int
    resource_id: str
    enrollment_generation: int = 0
    source_owner: ReaderConnection | None = None


@dataclass(frozen=True, repr=False)
class _Issued:
    token: str
    request_id: UUID
    issued_at: int
    expires_at: int


def _issue(
    scope: WorkerReadScope,
    operation: WorkerOperation,
    *,
    issuer: str,
    signing_key: Ed25519PrivateKey,
    lifetime_seconds: int,
) -> _Issued:
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 30:
        raise ValueError("invalid source worker grant lifetime")
    request_id = uuid4()
    issued_at = int(time.time())
    expires_at = issued_at + lifetime_seconds
    binding = asdict(scope)
    # Loom rechecks these local authority generations before delivery.
    del binding["approval_generation"], binding["enrollment_generation"], binding["source_owner"]
    for field in ("worker_id", "source_id", "connection_id"):
        binding[field] = str(binding[field])
    token = jwt.encode(
        {
            "iss": issuer,
            "aud": f"urn:loom:source-worker:{scope.worker_id}",
            "iat": issued_at,
            "exp": expires_at,
            "grant": {
                "version": 1,
                "operation": operation,
                "request_id": str(request_id),
                "binding": binding,
            },
        },
        signing_key,
        algorithm="EdDSA",
    )
    if len(token) > 24_576:
        raise WorkerReadRefused("denied")
    return _Issued(token, request_id, issued_at, expires_at)


class WorkerDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    resource_id: Annotated[str, StringConstraints(min_length=1, max_length=2048)] = Field(
        repr=False
    )
    title: Annotated[str, StringConstraints(max_length=4096)] = Field(repr=False)
    text: Annotated[str, StringConstraints(min_length=1, max_length=4_000_000)] = Field(repr=False)
    revision: Annotated[str, StringConstraints(max_length=2048)] | None = Field(repr=False)
    source_link: Annotated[str, StringConstraints(max_length=2048)] | None = Field(repr=False)
    mime_type: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class WorkerReading(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    status: Literal["ok"]
    request_id: UUID
    fetched_at: int
    content_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] = Field(repr=False)
    document: WorkerDocument | None = Field(repr=False)


def _validate_reading(
    payload: bytearray, scope: WorkerReadScope, issued: _Issued, operation: WorkerOperation
) -> WorkerReading:
    try:
        reading = WorkerReading.model_validate_json(payload)
        if reading.request_id != issued.request_id or not (
            issued.issued_at <= reading.fetched_at < issued.expires_at
        ):
            raise WorkerReadRefused("denied")
        if operation == "check_access":
            if reading.document is not None:
                raise WorkerReadRefused("denied")
            return reading
        if reading.document is None or reading.document.resource_id != scope.resource_id:
            raise WorkerReadRefused("denied")
        content = reading.document.text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        if len(content) > 4_000_000 or digest != reading.content_sha256:
            raise WorkerReadRefused("denied")
        return reading
    except (ValidationError, UnicodeError):
        raise WorkerReadRefused("denied") from None


class WorkerReadChannel:
    def __init__(
        self,
        *,
        worker_id: UUID,
        origin: str,
        issuer: str,
        signing_key: Ed25519PrivateKey,
        grant_seconds: int,
        max_response_bytes: int,
        max_inflight: int = 8,
        tls: ssl.SSLContext | None = None,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = httpx.URL(origin)
        if (
            not origin.isascii()
            or url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.raw_path != b"/"
            or url.fragment
        ):
            raise ValueError("worker must have an approved TLS origin")
        if (
            type(grant_seconds) is not int
            or not 1 <= grant_seconds <= 30
            or type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= 25_000_000
            or type(max_inflight) is not int
            or not 1 <= max_inflight <= 256
        ):
            raise ValueError("invalid source worker channel limits")
        if tls is not None and (tls.verify_mode != ssl.CERT_REQUIRED or not tls.check_hostname):
            raise ValueError("worker channel TLS verification is required")
        self._worker_id = worker_id
        self._origin = url
        self._issuer = issuer
        self._key = signing_key
        self._seconds = grant_seconds
        self._maximum = max_response_bytes
        self._max_inflight = max_inflight
        self._inflight = 0
        self._enrolling = 0
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            transport=_transport,
            timeout=grant_seconds,
            verify=tls or True,
            limits=httpx.Limits(
                max_connections=max_inflight + _ENROLLMENT_CAPACITY,
                max_keepalive_connections=max_inflight,
            ),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(
        self, path: str, token: str, *, maximum: int, json_object: bool = False
    ) -> bytearray:
        url = self._origin.copy_with(path=path)
        headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}
        body = b"{}" if json_object else None
        if json_object:
            headers["Content-Type"] = "application/json"
        data = bytearray()
        async with self._client.stream("POST", url, headers=headers, content=body) as response:
            if response.status_code != 200:
                raise WorkerReadRefused(_Statuses.get(response.status_code, "denied"))
            if (
                response.headers.get("content-encoding", "identity") != "identity"
                or response.headers.get("content-type", "").partition(";")[0] != "application/json"
            ):
                raise WorkerReadRefused("denied")
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > maximum:
                    raise WorkerReadRefused("unsupported")
                data.extend(chunk)
        return data

    async def request_enrollment(self, worker_id: UUID, token: str) -> bytearray:
        if worker_id != self._worker_id or not token.isascii() or not 1 <= len(token) <= 8192:
            raise WorkerReadRefused("denied")
        if self._enrolling >= _ENROLLMENT_CAPACITY:
            raise WorkerReadRefused("overloaded")
        self._enrolling += 1
        try:
            with private_work():
                async with asyncio.timeout(self._seconds):
                    return await self._post(
                        _ENROLLMENT_PATH, token, maximum=_MAX_ENROLLMENT_BODY, json_object=True
                    )
        except (TimeoutError, httpx.TimeoutException):
            raise WorkerReadRefused("timeout") from None
        except httpx.HTTPError:
            raise WorkerReadRefused("unavailable") from None
        finally:
            self._enrolling -= 1

    async def run(
        self,
        scope: WorkerReadScope,
        operation: WorkerOperation,
        *,
        require_current: Callable[[], Awaitable[WorkerReadScope]],
    ) -> WorkerReading:
        if self._inflight >= self._max_inflight:
            raise WorkerReadRefused("overloaded")
        self._inflight += 1
        try:
            with private_work():
                async with asyncio.timeout(self._seconds):
                    if scope.worker_id != self._worker_id or await require_current() != scope:
                        raise WorkerReadRefused("denied")
                    issued = _issue(
                        scope,
                        operation,
                        issuer=self._issuer,
                        signing_key=self._key,
                        lifetime_seconds=self._seconds,
                    )
                    payload = await self._post(
                        _Paths[operation], issued.token, maximum=self._maximum
                    )
                    reading = _validate_reading(payload, scope, issued, operation)
                    if await require_current() != scope:
                        raise WorkerReadRefused("denied")
                    if time.time() >= issued.expires_at:
                        raise WorkerReadRefused("timeout")
                    return reading
        except (TimeoutError, httpx.TimeoutException):
            raise WorkerReadRefused("timeout") from None
        except httpx.HTTPError:
            raise WorkerReadRefused("unavailable") from None
        finally:
            self._inflight -= 1
