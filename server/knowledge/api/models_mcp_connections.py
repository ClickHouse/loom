"""Owner-only MCP connection controls; credentials are input-only secrets."""

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from server.knowledge.api.models_private_sources import PrivateSourceInput


class MCPConnectInput(PrivateSourceInput):
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


class MCPListInput(PrivateSourceInput):
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


class MCPInspectInput(PrivateSourceInput):
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


class MCPSlotInput(PrivateSourceInput):
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


class MCPBrowseInput(MCPSlotInput):
    mode: Literal["resources", "search"] = Field(description="The explicit metadata operation.")
    query: str = Field(
        default="",
        max_length=1000,
        repr=False,
        description="The approved mapped-search query; empty for resources, default empty.",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=100,
        strict=True,
        description="Maximum metadata items in the first window, from 1 to 100; default 100.",
    )


class MCPBrowseItem(BaseModel):
    id: str = Field(repr=False)
    title: str = Field(repr=False)
    uri: str = Field(repr=False, description="A stable display hint that grants no read authority.")


class MCPBrowseOutput(BaseModel):
    slot: str
    generation: str
    mode: Literal["resources", "search"]
    supported: bool = False
    resources: bool | None
    mapped_read: bool
    mapped_search: bool
    capabilities_checked: bool = False
    items: list[MCPBrowseItem] = Field(default_factory=list)
    complete: bool = Field(
        default=False, description="The declared page fits this response window."
    )
    truncated: bool = False
    reason: str | None = None
    limitations: list[str]
    manual_resource_ids_supported: Literal[True] = True
    remote_identity_verified: Literal[False] = False
