"""Stable document addresses for opaque selected identities; no fetching or authority."""

from __future__ import annotations

import hashlib
from uuid import UUID

from server.knowledge.domain.mcp_sources import ResourceIdentity
from server.knowledge.domain.sources import mirror_uri


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
