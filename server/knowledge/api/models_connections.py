"""The caller's own connected accounts; credentials are never part of the wire shape."""

from typing import Literal

from pydantic import BaseModel, Field


class ConnectionListInput(BaseModel):
    """List the signed-in caller's own connected provider accounts."""

    org: str = Field(description="The caller's organization.")


class ConnectionEntry(BaseModel):
    kind: Literal["gdrive", "notion", "linear"]
    connected: bool
    connect_available: bool = Field(
        description="Whether this deployment is configured to start consent for the provider."
    )
    account_email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    connect_url: str


class ConnectionListOutput(BaseModel):
    connections: list[ConnectionEntry]


class ConnectionDisconnectInput(BaseModel):
    """Disconnect the caller's own provider account and stop its connected sources."""

    org: str = Field(description="The caller's organization.")
    kind: Literal["gdrive", "notion", "linear"] = Field(
        description="The connected provider to disconnect."
    )


class ConnectionDisconnectOutput(BaseModel):
    disconnected: bool
