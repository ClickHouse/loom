"""Read a selected resource now through its approved worker."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_ScopeName = Annotated[str, StringConstraints(min_length=1, max_length=255)]
_ResourceId = Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class KnowledgeSourceLiveInput(BaseModel):
    """Read one selected resource through the caller's current worker connection."""

    model_config = ConfigDict(hide_input_in_errors=True)

    org: _ScopeName = Field(description="Organization that owns the collection.")
    namespace: _ScopeName = Field(description="Collection that owns the source.")
    source: _ScopeName = Field(description="Name of the registered source in the collection.")
    resource_id: _ResourceId = Field(
        repr=False, description="Exact selected resource identifier returned by source setup."
    )


class KnowledgeSourceLiveDocument(BaseModel):
    """The current selected document. It is not written to the index."""

    model_config = ConfigDict(frozen=True)

    source_id: UUID
    resource_id: str = Field(repr=False)
    uri: str = Field(repr=False)
    source_link: str | None = Field(repr=False)
    fetched_at: int
    title: str = Field(repr=False)
    text: str = Field(repr=False)
    mime_type: str


class KnowledgeSourceLiveOutput(BaseModel):
    status: Literal[
        "ok", "denied", "reconnect", "overloaded", "timeout", "unavailable", "unsupported"
    ]
    document: KnowledgeSourceLiveDocument | None = Field(default=None, repr=False)
