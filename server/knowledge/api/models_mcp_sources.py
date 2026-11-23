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
    read_evidence_stale_seconds: int
    preview_grants_authority: Literal[False] = False
    limitations: list[str]


class MCPSourceAddInput(MCPSourcePreviewInput):
    request_id: str = Field(
        max_length=36, description="A client UUID retained across uncertain registration retries."
    )
    label: str | None = Field(
        default=None,
        max_length=256,
        repr=False,
        description="Optional source display label; default none; conveys no remote identity.",
    )
    confirm_copy_authority: bool = Field(
        default=False,
        strict=True,
        description=(
            "Confirm authority to publish this selection through current and future "
            "collection grants; default false."
        ),
    )


class MCPSourceScopeInput(_Input):
    namespace: str = Field(description="The ordinary destination collection or namespace.")


class MCPSourceListInput(MCPSourceScopeInput):
    cursor: str | None = Field(
        default=None,
        max_length=2048,
        description="Continuation for this exact listing; default none.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=20,
        strict=True,
        description="Source summaries per page, from 1 to 20; default 20.",
    )
    include_retired: bool = Field(
        default=False,
        strict=True,
        description="Include this owner's retired registrations; default false.",
    )


class MCPSourceInput(MCPSourceScopeInput):
    name: str = Field(max_length=64, description="The opaque source registry name returned by add.")


class MCPSourceInspectInput(MCPSourceScopeInput):
    name: str = Field(
        default="",
        max_length=64,
        description="The exact source name, or empty for request reconciliation.",
    )
    slot: str = Field(
        default="",
        max_length=36,
        description="The original owned connection slot for request reconciliation.",
    )
    request_id: str = Field(
        default="", max_length=36, description="The original add UUID, with slot instead of name."
    )


class MCPSourceUpdateInput(MCPSourceInput):
    if_scope_revision: int | None = Field(
        default=None,
        ge=1,
        le=2**63 - 1,
        strict=True,
        description=(
            "Require this current scope revision before changing selection; default unconditional."
        ),
    )
    resource_ids: list[str] = Field(
        min_length=1,
        max_length=1000,
        repr=False,
        description="The complete ordered fixed selection replacing the current selected IDs.",
    )
    mode: Literal["live", "snapshot", "selected_refresh"] = Field(
        description="Live lookup, a selected snapshot, or periodic refresh of this fixed selection."
    )
    label: str | None = Field(
        default=None,
        max_length=256,
        repr=False,
        description="Replacement display label, or none to clear it; default none.",
    )
    confirm_copy_authority: bool = Field(
        default=False,
        strict=True,
        description="Confirm publication authority for this replacement selection; default false.",
    )


class MCPSourceRemoveInput(MCPSourceInput):
    confirm: str = Field(
        max_length=64, description="Repeat the exact opaque source name to retire."
    )
    delete_documents: bool = Field(
        default=False,
        strict=True,
        description="Request asynchronous erasure of stored copies; default false retains them.",
    )


class MCPSourceSummary(BaseModel):
    name: str
    source_id: str
    slot: str
    namespace: str
    state: Literal["registered", "paused", "retired", "unsupported"]
    mode: Literal["live", "snapshot", "selected_refresh"] | None = None
    reader_policy: Literal["owner_only", "collection_copy"] | None = None
    scope_revision: int | None = None
    label: str | None = None
    selected_count: int | None = None
    sync_enabled: bool
    read_evidence_stale_seconds: int
    refresh_state: Literal[
        "not_applicable", "enabled", "disabled", "paused", "retired", "unsupported"
    ]
    compatibility: Literal["unchecked"] = "unchecked"
    upstream_permissions: Literal["unsupported"] = "unsupported"
    complete_enumeration: Literal[False] = False
    upstream_deletion_tracking: Literal[False] = False
    future_items_included: Literal[False] = False
    current_access_attested: Literal[False] = False
    local_authority: Literal["current", "unavailable", "unchecked"] = Field(
        default="unchecked",
        description="Current local source and account authority; no upstream ACL evidence.",
    )
    pending_erasure: bool = False
    rule: MCPAccessRule | None = None


class MCPSourceEntry(MCPSourceSummary):
    resource_ids: list[str] = Field(default_factory=list, repr=False)


class MCPSourceOutput(BaseModel):
    source: MCPSourceEntry
    copies: Literal["retained", "erasure_requested"] = Field(
        default="retained", description="Disposition of any copies, including earlier source modes."
    )
    refresh_requested: bool = False


class MCPSourceListOutput(BaseModel):
    sources: list[MCPSourceSummary]
    next_cursor: str | None = None


class MCPSourceInspectOutput(BaseModel):
    found: bool
    source: MCPSourceEntry | None = None
