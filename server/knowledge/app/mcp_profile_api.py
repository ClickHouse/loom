"""Read approved profile declarations after the personal-connection identity check."""

from datetime import UTC, datetime

from server.auth.context import current_auth
from server.core.cursors import decode_bound_cursor, encode_bound_cursor
from server.core.errors import ValidationError
from server.knowledge.api.models_mcp_profiles import (
    MCPProfileEntry,
    MCPProfilesInput,
    MCPProfilesOutput,
)
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app.mcp_profiles import MCPProfileError, load_mcp_profile_catalog
from server.settings import settings

# Catalog IDs have no timestamp; the shared cursor carries this fixed keyset anchor.
_CURSOR_TIME = datetime(1970, 1, 1, tzinfo=UTC)


async def handle_mcp_profiles(input: MCPProfilesInput) -> MCPProfilesOutput:
    org, subject = await accounts.own_subject(current_auth())
    if org != input.org:
        raise accounts.unavailable()
    try:
        catalog = load_mcp_profile_catalog()
        scope = (
            "knowledge.source.mcp_profiles",
            org,
            subject,
            input.query,
            catalog.sha256,
            str(input.limit),
        )
        after = None
        if input.cursor is not None:
            anchor, after = decode_bound_cursor(input.cursor, scope)
            if anchor != _CURSOR_TIME:
                raise ValidationError("cursor is not one this op issued for this request")
        page = catalog.page(org, input.query, after, input.limit)
        next_cursor = (
            encode_bound_cursor(scope, _CURSOR_TIME, page.next_after)
            if page.next_after is not None
            else None
        )
        return MCPProfilesOutput(
            self_service_enabled=settings.knowledge_source_self_service_enabled,
            profiles=[
                MCPProfileEntry(
                    id=item.id,
                    label=item.label,
                    auth=item.auth,
                    endpoint_origin=item.endpoint_origin,
                    profile_sha256=item.profile_sha256,
                    approved_tools=list(item.approved_tools),
                    allow_collection_copy=item.allow_collection_copy,
                )
                for item in page.items
            ],
            next_cursor=next_cursor,
        )
    except MCPProfileError:
        raise ValidationError("MCP_PROFILE_INVALID") from None
