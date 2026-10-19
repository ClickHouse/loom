"""Wire models for the `collection` operations."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CollectionCreateInput(BaseModel):
    """Create a collection: a namespace read and written only through grants."""

    org: str
    namespace: str = Field(
        description="The collection's name, also its namespace: 'col-' then lowercase letters, "
        "digits and '-'."
    )
    parent: str = Field(
        default="",
        description="The collection it sits in and inherits grants from; '' for a top-level one.",
    )
    description: str = ""


class CollectionCreateOutput(BaseModel):
    namespace: str
    parent: str
    created: bool  # False when the caller's collection already existed there


class CollectionMoveInput(BaseModel):
    """Put a collection under another one, or detach it."""

    org: str
    namespace: str
    parent: str = Field(
        default="",
        description="The new parent collection; '' detaches it, keeping only its own grants.",
    )


class CollectionMoveOutput(BaseModel):
    namespace: str
    parent: str
    moved: bool  # False when it already sat there


class CollectionListInput(BaseModel):
    """List the collections the caller can read."""

    org: str
    namespace: str = Field(
        default="-", description="Unused; collections are listed for the whole org."
    )


class _CollectionEntry(BaseModel):
    namespace: str
    parent: str
    personal: bool
    role: str  # the caller's role there: viewer, writer or admin


class CollectionListOutput(BaseModel):
    collections: list[_CollectionEntry]
