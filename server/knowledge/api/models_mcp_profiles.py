"""Operator approval metadata; no endpoint, credential or publication authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MCPProfilesInput(BaseModel):
    """List approved profile declarations for the signed-in caller's organization."""

    model_config = ConfigDict(extra="forbid")

    org: str = Field(description="The caller's authenticated organization.")
    query: str = Field(
        default="",
        max_length=256,
        description="Case-insensitive ID or label filter, at most 256 UTF-8 bytes; default empty.",
    )
    cursor: str | None = Field(
        default=None,
        max_length=2048,
        description="Continuation from this listing with the same arguments; default none.",
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=100,
        strict=True,
        description="Maximum profile declarations per page, from 1 to 100; default 100.",
    )


class MCPProfileEntry(BaseModel):
    """An operator declaration, without remote identity or compatibility verification."""

    id: str
    label: str
    auth: Literal["none", "bearer"]
    profile_sha256: str
    approved_tools: list[str]
    allow_collection_copy: bool = Field(
        description="Operator read-policy approval only; copying still requires publication grants."
    )


class MCPProfilesOutput(BaseModel):
    profiles: list[MCPProfileEntry]
    next_cursor: str | None = None
    self_service_enabled: bool = Field(
        description="Whether the operator permits self-service source and credential registration."
    )
