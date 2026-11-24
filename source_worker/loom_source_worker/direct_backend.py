"""A worker backend for operator-approved direct MCP accounts."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Protocol
from uuid import UUID

import httpx

from .credential_store import CredentialRecord
from .mcp_client import MCPBearer, MCPSourceClient, MCPSourceConfig, MCPSourceError
from .protocol import ConnectionBinding, ReadBinding
from .read_service import BoundReader, CurrentAccess, Document, FailureCode, ReadFailure


@dataclass(frozen=True, repr=False)
class ApprovedProfile:
    config: MCPSourceConfig
    organizations: tuple[str, ...]
    link_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.config.approved_resource_ids or not self.organizations:
            raise ValueError("profile must scope the tenant without selecting resources")
        if self.config.read is not None and self.config.read.result_format == "text":
            raise ValueError("mapped read must return its selected resource identity")
        if any(_link_origin(value) != value for value in self.link_origins):
            raise ValueError("invalid approved link origin")

    @property
    def digest(self) -> str:
        payload = json.dumps(
            asdict(self), allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def _link_origin(value: str) -> str | None:
    if not value or not value.isascii() or any(not 33 <= ord(char) <= 126 for char in value):
        return None
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return None
    if url.scheme != "https" or not url.host or url.userinfo:
        return None
    return str(url.copy_with(path="/", query=None, fragment=None)).removesuffix("/")


class CredentialStore(Protocol):
    async def get(self, connection_id: UUID) -> CredentialRecord: ...


@dataclass(frozen=True, repr=False)
class _MCPReader:
    record: CredentialRecord
    profile: ApprovedProfile
    resource_id: str

    @property
    def binding(self) -> ConnectionBinding:
        return self.record.binding

    async def read_exact(self, resource_id: str, *, authorize: CurrentAccess) -> Document:
        if resource_id != self.resource_id:
            raise ReadFailure("denied")
        config = replace(self.profile.config, approved_resource_ids=(self.resource_id,))
        bearer = self._bearer()
        if bearer is not None and bearer.token in resource_id:
            raise ReadFailure("denied")
        refusal: ReadFailure | None = None

        async def require_current() -> None:
            nonlocal refusal
            try:
                await authorize()
            except ReadFailure as error:
                refusal = error
                raise MCPSourceError("denied") from None

        try:
            async with MCPSourceClient(config, bearer, validate_access=require_current) as client:
                await client.discover()
                item = await client.read(self.resource_id)
                receipt = item.receipt
                if (
                    receipt is None
                    or not receipt.identity_verified
                    or receipt.requested_id != self.resource_id
                    or receipt.config_sha256 != client.config_sha256
                ):
                    raise ReadFailure("denied")
        except MCPSourceError as error:
            if refusal is not None:
                raise refusal from None
            mapped: dict[str, FailureCode] = {
                "denied": "denied",
                "reconnect": "reconnect",
                "timeout": "timeout",
            }
            raise ReadFailure(mapped.get(error.code, "unsupported")) from None
        returned = (item.text, item.title, item.uri, item.mime_type, item.revision or "")
        if bearer is not None and any(bearer.token in value for value in returned):
            raise ReadFailure("unsupported")
        link = item.uri if _link_origin(item.uri) in self.profile.link_origins else None
        return Document(
            item.resource_id, item.title, item.text, item.revision, link, item.mime_type
        )

    def _bearer(self) -> MCPBearer | None:
        if self.record.auth == "anonymous":
            if self.profile.config.bearer_audience is not None:
                raise ReadFailure("denied")
            return None
        if self.record.token is None or self.record.audience != self.profile.config.bearer_audience:
            raise ReadFailure("denied")
        assert self.record.audience is not None
        return MCPBearer(self.record.token.get_secret_value(), self.record.audience)


class DirectBackend:
    def __init__(self, store: CredentialStore, profiles: tuple[ApprovedProfile, ...]) -> None:
        if not 1 <= len(profiles) <= 4096:
            raise ValueError("invalid worker profile count")
        self._store = store
        self._profiles = {profile.digest: profile for profile in copy.deepcopy(profiles)}
        if len(self._profiles) != len(profiles):
            raise ValueError("duplicate worker profile")

    async def _resolve(self, binding: ReadBinding) -> tuple[CredentialRecord, ApprovedProfile]:
        profile = self._profiles.get(binding.profile_digest)
        if profile is None or binding.org not in profile.organizations:
            raise ReadFailure("denied")
        record = await self._store.get(binding.connection_id)
        return record, profile

    async def resolve(self, binding: ReadBinding) -> BoundReader:
        record, profile = await self._resolve(binding)
        return _MCPReader(record, profile, binding.resource_id)

    async def current(self, binding: ReadBinding) -> ConnectionBinding:
        record, _ = await self._resolve(binding)
        return record.binding
