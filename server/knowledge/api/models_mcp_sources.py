"""Selected MCP source scope and explanatory access, without credentials."""

from typing import Literal

from pydantic import BaseModel, Field

from server.knowledge.api.models_mcp_connections import _Input


class MCPSourcePreviewInput(_Input):
    org: str = Field(description="The authenticated organization.")
    namespace: str = Field(description="The ordinary destination collection or namespace.")
    slot: str = Field(max_length=36, description="The signed-in owner's MCP connection slot.")
    mode: Literal["live", "snapshot", "selected_refresh"] = Field(
        description="Live lookup, a selected snapshot, or periodic refresh of the fixed selection."
    )
    reader_policy: Literal["owner_only", "collection_copy"] = Field(
        default="owner_only", description="Requested copy audience; default owner-only."
    )
    resource_ids: list[str] = Field(
        min_length=1,
        max_length=1000,
        repr=False,
        description="Between 1 and 1000 explicit resource identities, without recursion.",
    )


class MCPAccessRule(BaseModel):
    access: Literal["owner_only", "collection_wide", "unavailable"]
    reason: str = ""
    stores_copy: bool
    future_items_included: Literal[False] = False
    upstream_permissions: Literal["unsupported"] = "unsupported"
    reader_list_complete: Literal[False] = False
    collection_readers_receive_copy: bool = False
    inherited_grants_apply: Literal[True] = True
    membership_changes_affect_copy: bool = False
    publication_confirmation_required: bool = False
    explanation: str


class MCPSourcePreviewOutput(BaseModel):
    namespace: str
    mode: Literal["live", "snapshot", "selected_refresh"]
    rule: MCPAccessRule
    connection_state: Literal["configured", "stopped", "unavailable", "approval_drift"]
    compatibility: Literal["unchecked"] = "unchecked"
    permission_evidence: Literal["unsupported"] = "unsupported"
    refresh_state: Literal["not_applicable", "enabled", "disabled"]
    selected_count: int
    checked_at: str
    preview_grants_authority: Literal[False] = False
    limitations: list[str]
