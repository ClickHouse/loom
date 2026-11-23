"""Pure MCP connection and selection codecs; lifecycle and authorization stay with callers."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)


class MCPSourceContractError(ValueError):
    """A public codec failure contains no supplied payload or credential."""

    def __init__(self) -> None:
        super().__init__("MCP_SOURCE_CONTRACT_INVALID")


def _identifier(value: str) -> str:
    if (
        not value.strip()
        or len(value.encode("utf-8")) > 2048
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ValueError("invalid identifier")
    return value


_Identifier = Annotated[str, Field(min_length=1, max_length=2048), AfterValidator(_identifier)]
_Slot = Annotated[_Identifier, Field(pattern=r"^mcp:[0-9a-f]{32}$")]
_Generation = Annotated[_Identifier, Field(pattern=r"^[0-9a-f]{32}/[01][0-9a-f]{31}$")]
_Digest = Annotated[_Identifier, Field(pattern=r"^[0-9a-f]{64}$")]
_Version = Annotated[int, Field(strict=True, ge=1, le=1)]
_SLOT_ADAPTER: TypeAdapter[str] = TypeAdapter(_Slot)


def is_mcp_slot(value: object) -> bool:
    """Recognize a lifecycle key without opening an envelope or granting read access."""
    try:
        _SLOT_ADAPTER.validate_python(value, strict=True)
    except (ValueError, TypeError, RecursionError):
        return False
    return True


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class ConnectionEnvelope(_ContractModel):
    """One slot's sealing plaintext; endpoint approval is a caller responsibility."""

    version: _Version
    org: _Identifier
    owner: _Identifier
    slot: _Slot
    generation: _Generation
    profile_id: _Identifier
    profile_sha256: _Digest
    endpoint: _Identifier
    audience: _Identifier | None
    auth: Literal["none", "bearer"]
    bearer: SecretStr | None = Field(default=None, exclude=True, repr=False)
    label: Annotated[_Identifier, Field(max_length=256)] | None = None

    @field_validator("bearer")
    @classmethod
    def _bearer_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            token = value.get_secret_value()
            if not 0 < len(token) <= 8192 or any(not 33 <= ord(char) <= 126 for char in token):
                raise ValueError("invalid bearer")
        return value

    @model_validator(mode="after")
    def _auth_shape(self) -> Self:
        if self.auth == "none":
            if self.bearer is not None or self.audience is not None:
                raise ValueError("invalid anonymous auth")
        elif self.bearer is None or self.audience is None:
            raise ValueError("invalid bearer auth")
        return self


class ResourceIdentity(_ContractModel):
    type: Literal["resource"]
    id: _Identifier


def selection_sha256(resources: tuple[ResourceIdentity, ...]) -> str:
    """Hash ordered exact resource IDs without URL or Unicode normalization."""
    payload = json.dumps([r.id for r in resources], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CopyConsent(_ContractModel):
    """Server-issued publication authority for one destination and exact selection."""

    version: _Version
    publisher: _Identifier
    namespace: _Identifier
    resources_sha256: _Digest


class SourceContract(_ContractModel):
    """Reader policy is declarative; grants, consent and publication are checked elsewhere.

    The outer row version and binding remain the caller's responsibility. Unknown raw
    contracts need lifecycle preservation, not execution through this codec.
    """

    version: _Version
    slot: _Slot
    generation: _Generation
    profile_id: _Identifier
    profile_sha256: _Digest
    mode: Literal["live", "snapshot", "selected_refresh"]
    reader_policy: Literal["owner_only", "collection_copy"]
    scope_revision: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)] = 1
    resources: Annotated[tuple[ResourceIdentity, ...], Field(min_length=1, max_length=1000)]
    copy_consent: CopyConsent | None = None
    paused: bool = False
    label: Annotated[_Identifier, Field(max_length=256)] | None = None

    @model_validator(mode="after")
    def _copy_authority(self) -> Self:
        if self.reader_policy == "owner_only":
            if self.copy_consent is not None:
                raise ValueError("unexpected copy consent")
        elif (
            self.mode == "live"
            or self.copy_consent is None
            or self.copy_consent.resources_sha256 != selection_sha256(self.resources)
        ):
            raise ValueError("invalid copy consent")
        return self

    @field_validator("resources")
    @classmethod
    def _unique_resources(cls, value: tuple[ResourceIdentity, ...]) -> tuple[ResourceIdentity, ...]:
        if len({resource.id for resource in value}) != len(value):
            raise ValueError("duplicate resource")
        return value


def _bounded_text(value: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise ValueError("invalid payload size")
    return value


def _encode(value: Any, maximum: int) -> str:
    return _bounded_text(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ),
        maximum,
    )


def encode_envelope(envelope: ConnectionEnvelope) -> str:
    """Reveal the bearer only into plaintext intended for the existing sealing helper."""
    try:
        envelope = ConnectionEnvelope.model_validate(envelope)
        payload = envelope.model_dump(mode="json")
        if envelope.bearer is not None:
            payload["bearer"] = envelope.bearer.get_secret_value()
        return _encode(payload, 32768)
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None


def decode_envelope(plaintext: str) -> ConnectionEnvelope:
    try:
        return ConnectionEnvelope.model_validate_json(_bounded_text(plaintext, 32768))
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None


def bind_envelope(
    envelope: ConnectionEnvelope,
    *,
    org: str,
    owner: str,
    slot: str,
    generation: str,
    profile_id: str,
    profile_sha256: str,
    endpoint: str,
    audience: str | None,
) -> None:
    """Compare the fetched row and current approved profile exactly, without guessing identity."""
    if (
        envelope.org,
        envelope.owner,
        envelope.slot,
        envelope.generation,
        envelope.profile_id,
        envelope.profile_sha256,
        envelope.endpoint,
        envelope.audience,
    ) != (org, owner, slot, generation, profile_id, profile_sha256, endpoint, audience):
        raise MCPSourceContractError()


def encode_source(contract: SourceContract) -> tuple[str, str]:
    """Return config_json and items_json; selection has exactly one stored representation."""
    try:
        contract = SourceContract.model_validate(contract)
        config = contract.model_dump(mode="json", exclude={"resources"})
        items = [resource.model_dump(mode="json") for resource in contract.resources]
        return _encode(config, 32768), _encode(items, 1000 * (2 * 2048 + 64) + 2)
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None


def decode_source(config_json: str, items_json: str) -> SourceContract:
    """Join the two stored columns; this codec admits only the known contract version."""
    try:
        config = json.loads(_bounded_text(config_json, 32768))
        items = json.loads(_bounded_text(items_json, 1000 * (2 * 2048 + 64) + 2))
        if not isinstance(config, dict) or not isinstance(items, list) or "resources" in config:
            raise ValueError("invalid source columns")
        return SourceContract.model_validate({**config, "resources": tuple(items)})
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None
