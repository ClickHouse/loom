"""Owner-only MCP connection controls; credentials are input-only secrets."""

from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    SecretStr,
    model_validator,
)
from pydantic import (
    ValidationError as ModelValidationError,
)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    org: str = Field(description="The authenticated caller's organization.")

    @model_validator(mode="wrap")
    @classmethod
    def _redact_errors(cls, value: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        try:
            return handler(value)
        except ModelValidationError:
            # HTTP serializes error inputs; hiding only their text repr is insufficient.
            raise ModelValidationError.from_exception_data(
                cls.__name__,
                [
                    {
                        "type": "value_error",
                        "loc": (),
                        "input": "<redacted>",
                        "ctx": {"error": ValueError("MCP_CONNECTION_INPUT_INVALID")},
                    }
                ],
            ) from None


class MCPConnectInput(_Input):
    profile_id: str = Field(
        min_length=1, max_length=128, description="The operator-approved profile ID."
    )
    request_id: str = Field(
        max_length=36, description="A client-generated UUID retained across uncertain-save retries."
    )
    label: str | None = Field(
        default=None,
        max_length=256,
        repr=False,
        description="An optional display label; it verifies no remote identity.",
    )
    bearer: SecretStr | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="The preissued upstream bearer for a bearer profile; default none.",
    )


class MCPListInput(_Input):
    cursor: str | None = Field(
        default=None,
        max_length=2048,
        description="The signed continuation for the same listing arguments; default none.",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=100,
        strict=True,
        description="Maximum connections per page, from 1 to 100; default 100.",
    )


class MCPInspectInput(_Input):
    slot: str = Field(
        default="",
        max_length=36,
        description="An exact owned MCP slot, or empty when reconciling a request ID.",
    )
    request_id: str = Field(
        default="",
        max_length=36,
        description="The original connect UUID for save reconciliation; default empty.",
    )


class MCPSlotInput(_Input):
    slot: str = Field(max_length=36, description="The exact owned MCP connection slot.")


class MCPConnectionEntry(BaseModel):
    slot: str
    generation: str
    state: Literal["configured", "stopped", "unavailable", "approval_drift"]
    profile_id: str | None = None
    profile_sha256: str | None = None
    label: str | None = None
    compatibility: Literal["unchecked"] = "unchecked"
    remote_identity_verified: Literal[False] = False


class MCPConnectOutput(BaseModel):
    connection: MCPConnectionEntry


class MCPListOutput(BaseModel):
    connections: list[MCPConnectionEntry]
    next_cursor: str | None = None


class MCPInspectOutput(BaseModel):
    found: bool
    slot: str
    connection: MCPConnectionEntry | None = None
    reconciliation: Literal["absent", "present", "incomplete"]


class MCPDiscoverOutput(BaseModel):
    slot: str
    generation: str
    resources: bool
    mapped_read: bool
    mapped_search: bool
    supported_reads: bool
    limitations: list[str]
    remote_identity_verified: Literal[False] = False


class MCPDisconnectOutput(BaseModel):
    disconnected: bool
    upstream_revoked: Literal[False] = False
    upstream_action: Literal["revoke_credential_upstream"] = "revoke_credential_upstream"
