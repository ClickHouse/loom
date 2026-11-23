"""Pure MCP connection and selection codecs; lifecycle and authorization stay with callers."""

from __future__ import annotations

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

MAX_ENVELOPE_BYTES = 32 * 1024
MAX_RESOURCES = 1000
MAX_RESOURCE_BYTES = 2048
_MAX_ITEMS_BYTES = MAX_RESOURCES * (2 * MAX_RESOURCE_BYTES + 64) + 2


class MCPSourceContractError(ValueError):
    """A public codec failure contains no supplied payload or credential."""

    def __init__(self) -> None:
        super().__init__("MCP_SOURCE_CONTRACT_INVALID")


def _identifier(value: str) -> str:
    if (
        not value.strip()
        or len(value.encode("utf-8")) > MAX_RESOURCE_BYTES
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
    resources: Annotated[
        tuple[ResourceIdentity, ...], Field(min_length=1, max_length=MAX_RESOURCES)
    ]

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
        return _encode(payload, MAX_ENVELOPE_BYTES)
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None


def decode_envelope(plaintext: str) -> ConnectionEnvelope:
    try:
        return ConnectionEnvelope.model_validate_json(_bounded_text(plaintext, MAX_ENVELOPE_BYTES))
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
        return _encode(config, MAX_ENVELOPE_BYTES), _encode(items, _MAX_ITEMS_BYTES)
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None


def decode_source(config_json: str, items_json: str) -> SourceContract:
    """Join the two stored columns; this codec admits only the known contract version."""
    try:
        config = json.loads(_bounded_text(config_json, MAX_ENVELOPE_BYTES))
        items = json.loads(_bounded_text(items_json, _MAX_ITEMS_BYTES))
        if not isinstance(config, dict) or not isinstance(items, list) or "resources" in config:
            raise ValueError("invalid source columns")
        return SourceContract.model_validate({**config, "resources": tuple(items)})
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None
