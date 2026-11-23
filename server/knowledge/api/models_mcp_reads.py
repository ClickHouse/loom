"""Selected live MCP reads use saved source authority, never a caller endpoint."""

from typing import Literal

from pydantic import BaseModel, Field

from server.knowledge.api.models_private_sources import PrivateSourceInput


class MCPReadInput(PrivateSourceInput):
    namespace: str = Field(min_length=1, max_length=255, description="The source collection.")
    name: str = Field(
        min_length=1, max_length=256, repr=False, description="The opaque saved source name."
    )
    resource_id: str = Field(
        min_length=1,
        max_length=2048,
        repr=False,
        description="An exact resource ID explicitly selected in the saved live source.",
    )


class MCPReadOutput(BaseModel):
    source_id: str
    resource_id: str = Field(repr=False)
    uri: str = Field(repr=False)
    title: str = Field(repr=False)
    text: str = Field(repr=False)
    mime_type: str
    resource_identity_verified: bool
    remote_identity_verified: Literal[False] = False
    upstream_acl_verified: Literal[False] = False
    limitations: list[str]
