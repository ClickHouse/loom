"""Wire models for the `collection` operations."""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, Field, field_validator, model_validator

from server.identity.domain.collection_names import SHAPING

# Format characters a title keeps: the joiners emoji and some scripts need, the soft
# hyphen, and the tag characters of subdivision flags. When titles are compared the soft
# hyphen is ignored and the joiners and tags count (collection_names.title_key).
_KEPT_FORMAT = SHAPING | {"\u00ad"}
# Characters outside the format category that draw as nothing or break a line.
_HIDDEN = frozenset("\u034f\u3164\u115f\u1160\uffa0\u2800\u2028\u2029")


def _hidden(ch: str) -> bool:
    """A character that would let two titles look alike, or reorder how one reads."""
    cat = unicodedata.category(ch)
    return cat == "Cc" or (cat == "Cf" and ch not in _KEPT_FORMAT) or ch in _HIDDEN


class CollectionCreateInput(BaseModel):
    """Create a collection: a namespace read and written only through grants.
    Name it, or give it a title and the server derives the name."""

    org: str
    namespace: str = Field(
        default="",
        description="The collection's name, also its namespace ('col-' then lowercase "
        "letters, digits and '-'), ignored with `derive_name`.",
    )
    title: str = Field(
        default="",
        max_length=200,
        description="What the collection is called on screen (at most 200 characters, no "
        "control or invisible ones; titles repeat across the org); '' for none.",
    )
    derive_name: bool = Field(
        default=False,
        description="Derive the name from `title`, not `namespace`; a title used under the "
        "parent (top level: the caller's own) answers that one. Default false.",
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

    # Stripped before the length is checked: the limit is on the title kept.
    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        # One spelling for one text: an accent typed as one character or as two is the same.
        v = unicodedata.normalize("NFC", v.strip())
        if any(_hidden(ch) for ch in v):
            raise ValueError("a title holds no control or invisible characters")
        return v

    @model_validator(mode="after")
    def _named(self) -> CollectionCreateInput:
        if self.derive_name and not self.title:
            raise ValueError("derive_name needs a title")
        if not self.derive_name and not self.namespace:
            raise ValueError("give the collection a name, or a title with derive_name")
        return self


class CollectionCreateOutput(BaseModel):
    namespace: str
    parent: str = Field(description="The collection it sits in; '' for a top-level one.")
    # False when the collection already existed: the caller's own by that name, or the one
    # the title already names there.
    created: bool
    title: str = Field(default="", description="The collection's title; '' for none.")


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
    title: str = ""  # what it is called on screen; '' for none (then its name)


class CollectionListOutput(BaseModel):
    collections: list[_CollectionEntry] = Field(
        description="The collections the caller can read on this page, by name, with the "
        "caller's role and the collection's title and description."
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
