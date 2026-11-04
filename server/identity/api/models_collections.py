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
    description: str = Field(
        default="",
        max_length=2000,
        description="What the collection holds, shown beside its name (at most 2000 "
        "characters); '' for none.",
    )


class CollectionCreateOutput(BaseModel):
    namespace: str
    parent: str = Field(description="The collection it sits in; '' for a top-level one.")
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
    parent: str = Field(description="The collection it now sits in; '' once detached.")
    moved: bool  # False when it already sat there


class CollectionListInput(BaseModel):
    """List the collections the caller can read, by name."""

    org: str
    limit: int = Field(default=500, ge=1, le=10_000, description="Collections per page.")
    cursor: str | None = Field(
        default=None, description="The `next_cursor` of the previous page; none for the first."
    )


class _CollectionEntry(BaseModel):
    namespace: str
    parent: str
    personal: bool
    role: str  # the caller's role there: viewer, writer or admin
    description: str = ""  # what it holds, as `collection.create` was given it


class CollectionListOutput(BaseModel):
    collections: list[_CollectionEntry] = Field(
        description="The collections the caller can read on this page, by name, with the "
        "caller's role and the collection's description."
    )
    next_cursor: str | None = Field(
        default=None, description="Pass as `cursor` for the next page; none on the last."
    )


class CollectionDeleteInput(BaseModel):
    """Delete a collection with what it holds: a purging erasure of the
    namespace (documents, chunks, originals, memory leave the disk), then it
    leaves the directory and the tree, and its name is never used again."""

    org: str
    namespace: str


class CollectionDeleteOutput(BaseModel):
    namespace: str
    deleted: bool  # False when it was already deleted
    erasure_id: str = Field(
        default="",
        description="The erasure of what the collection held; `erasure.status` follows it.",
    )
    erasure_status: str = Field(
        default="",
        description="That erasure's outcome: `completed`, or `failed` while the recovery "
        "worker retries it.",
    )
