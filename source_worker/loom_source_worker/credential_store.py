"""Read connections from a worker-owned KV v2 secret mount."""

from __future__ import annotations

import math
import re
import ssl
import time
from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .protocol import ConnectionBinding
from .read_service import ReadFailure

_RESPONSE_BYTES = 32_768
_MAX_TOKEN_BYTES = 8192


class CredentialRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, strict=True)

    binding: ConnectionBinding
    state: Literal["active", "revoked"]
    auth: Literal["anonymous", "bearer"]
    audience: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    token: SecretStr | None = None
    expires_at: Annotated[int, Field(ge=1, le=2**63 - 1)] | None = None


class _Metadata(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    version: Annotated[int, Field(ge=1, le=2**63 - 1)]
    destroyed: bool
    deletion_time: str


class _Data(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    data: CredentialRecord
    metadata: _Metadata


class _Reply(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    data: _Data


def _secret_token(value: str) -> bool:
    return 0 < len(value) <= _MAX_TOKEN_BYTES and all(33 <= ord(char) <= 126 for char in value)


class VaultCredentialStore:
    def __init__(
        self,
        *,
        origin: str,
        mount: str,
        worker_id: UUID,
        token: Callable[[], SecretStr],
        timeout_seconds: float,
        tls: ssl.SSLContext | None = None,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        target = httpx.URL(origin)
        if (
            not origin.isascii()
            or target.scheme != "https"
            or not target.host
            or target.userinfo
            or target.raw_path != b"/"
            or target.fragment
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", mount)
        ):
            raise ValueError("invalid credential backend origin or mount")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 10:
            raise ValueError("invalid credential backend deadline")
        if tls is not None and (tls.verify_mode != ssl.CERT_REQUIRED or not tls.check_hostname):
            raise ValueError("credential backend TLS verification is required")
        self._url = target.copy_with(path=f"/v1/{mount}/data/{worker_id}/")
        self._worker_id = worker_id
        self._token = token
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            verify=tls or True,
            transport=_transport,
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _download(self, url: httpx.URL) -> bytearray:
        try:
            token = self._token().get_secret_value()
        except OSError:
            raise ReadFailure("unavailable") from None
        if not _secret_token(token):
            raise ReadFailure("unavailable")
        headers = {
            "X-Vault-Token": token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        output = bytearray()
        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    raise ReadFailure("reconnect")
                if response.status_code in {401, 403}:
                    raise ReadFailure("denied")
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ReadFailure("unavailable")
                async for chunk in response.aiter_raw():
                    if len(output) + len(chunk) > _RESPONSE_BYTES:
                        raise ReadFailure("unavailable")
                    output.extend(chunk)
        except httpx.TimeoutException:
            raise ReadFailure("timeout") from None
        except httpx.HTTPError:
            raise ReadFailure("unavailable") from None
        return output

    async def get(self, connection_id: UUID) -> CredentialRecord:
        payload = await self._download(self._url.copy_with(path=f"{self._url.path}{connection_id}"))
        try:
            envelope = _Reply.model_validate_json(payload).data
        except ValidationError:
            raise ReadFailure("unavailable") from None
        record, metadata = envelope.data, envelope.metadata
        if (
            record.binding.worker_id != self._worker_id
            or record.binding.connection_id != connection_id
            or record.binding.generation != metadata.version
        ):
            raise ReadFailure("denied")
        if record.state != "active" or metadata.destroyed or metadata.deletion_time:
            raise ReadFailure("denied")
        if record.auth == "anonymous":
            attributes = (record.token, record.audience, record.expires_at)
            if any(value is not None for value in attributes):
                raise ReadFailure("denied")
        elif (
            record.token is None
            or not _secret_token(record.token.get_secret_value())
            or record.audience is None
            or record.expires_at is None
            or record.expires_at <= time.time()
        ):
            raise ReadFailure("reconnect")
        return record
