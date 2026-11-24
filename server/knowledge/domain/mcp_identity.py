"""Stable document addresses for opaque selected identities; no fetching or authority."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from server.knowledge.domain.sources import mirror_uri


def _identifier(value: str) -> str:
    if (
        not value.strip()
        or len(value.encode("utf-8")) > 2048
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ValueError("invalid resource identifier")
    return value


class ResourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    type: Literal["resource"]
    id: Annotated[str, Field(min_length=1, max_length=2048), AfterValidator(_identifier)]


def selection_sha256(resources: tuple[ResourceIdentity, ...]) -> str:
    """Hash ordered exact resource IDs without URL or Unicode normalization."""
    payload = json.dumps(
        [resource.id for resource in resources], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MCPIdentityError(ValueError):
    """Invalid identity input is never included in the public error."""

    def __init__(self) -> None:
        super().__init__("MCP_RESOURCE_IDENTITY_INVALID")


def resource_digest(resource_id: str) -> str:
    """Hash exact UTF-8 identity bytes, without interpreting URLs or normalizing text."""
    try:
        identity = ResourceIdentity(type="resource", id=resource_id)
        return hashlib.sha256(identity.id.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError):
        raise MCPIdentityError() from None


def resource_uri(source_id: str, resource_id: str) -> str:
    """Use the existing source-copy suffix; the source registration binds the account.

    A connection reconfiguration must allocate a new source ID. This address grants
    no read access and is never an outbound fetch target or an upstream citation.
    """
    try:
        if not isinstance(source_id, str) or str(UUID(source_id)) != source_id:
            raise MCPIdentityError()
        return mirror_uri("mcp-resource:" + resource_digest(resource_id), source_id)
    except (ValueError, TypeError, AttributeError):
        raise MCPIdentityError() from None
