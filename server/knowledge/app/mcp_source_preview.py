"""Explain one owner's selected scope without granting access or importing it."""

from datetime import UTC, datetime
from typing import Literal

from server.auth import authz
from server.core.errors import AuthInsufficientError
from server.knowledge.api.models_mcp_sources import (
    MCPAccessRule,
    MCPSourcePreviewInput,
    MCPSourcePreviewOutput,
)
from server.knowledge.app import mcp_connections as connections
from server.knowledge.app import sources
from server.knowledge.app.mcp_contracts import validate_source_contract
from server.knowledge.domain.mcp_sources import ResourceIdentity, SourceContract
from server.knowledge.store import connected_accounts as account_store
from server.operations.base import Role
from server.settings import settings


def access_rule(
    mode: str, policy: str, *, copy_approved: bool, administrator: bool
) -> MCPAccessRule:
    """Collection membership cannot establish an upstream reader permission."""
    stores_copy = mode != "live"
    if mode not in {"live", "snapshot", "selected_refresh"} or policy not in {
        "owner_only",
        "collection_copy",
    }:
        return MCPAccessRule(
            stores_copy=stores_copy,
            access="unavailable",
            reason="unsupported_policy",
            explanation="This source mode or access policy is unsupported.",
        )
    if policy == "owner_only":
        return MCPAccessRule(
            stores_copy=stores_copy,
            access="owner_only",
            explanation=(
                "Only the source owner with current destination access may read this material. "
                "Adding collection members or changing its parent does not grant them access. "
                "Upstream document permissions are unavailable."
            ),
        )
    reason = "shared_live_unsupported" if mode == "live" else "publication_not_authorized"
    if mode == "live" or not copy_approved or not administrator:
        return MCPAccessRule(
            stores_copy=stores_copy,
            access="unavailable",
            reason=reason,
            explanation="The current source and destination policy do not authorize this audience.",
        )
    return MCPAccessRule(
        stores_copy=stores_copy,
        access="collection_wide",
        collection_readers_receive_copy=True,
        membership_changes_affect_copy=True,
        publication_confirmation_required=True,
        explanation=(
            "Permitted destination readers may read the stored copy, including readers admitted "
            "through inherited or future grants. Changing membership or its parent may broaden "
            "access. This publishes a copy and does not preserve upstream document permissions. "
            "Publication requires explicit authority over the selected material."
        ),
    )


async def handle_mcp_source_preview(input: MCPSourcePreviewInput) -> MCPSourcePreviewOutput:
    async with connections._errors():
        owner = await connections._owner(input.org)
        connections._check_slot(input.slot)
        if not await authz.row_in_scope(input.org, input.namespace, Role.WRITER):
            raise AuthInsufficientError("destination writer access is required")
        await sources._require_source_namespace(input.org, input.namespace)
        await sources._require_no_erasure(input.org, input.namespace)
        account = await account_store.get(input.org, owner, input.slot)
        if account is None:
            raise connections._Refused("MCP_CONNECTION_UNAVAILABLE")
        connections._row_scope(account, input.org, owner, input.slot)
        entry = await connections._entry(account)
        approved = False
        if entry.state == "configured":
            await connections._validate(account)
            profile, envelope = connections._binding(account)
            contract = SourceContract(
                version=1,
                slot=input.slot,
                generation=account.connection_id,
                profile_id=envelope.profile_id,
                profile_sha256=envelope.profile_sha256,
                mode=input.mode,
                reader_policy=input.reader_policy,
                resources=tuple(
                    ResourceIdentity(type="resource", id=r) for r in input.resource_ids
                ),
            )
            validate_source_contract(contract)
            approved = profile.metadata.allow_collection_copy
        rule = access_rule(
            input.mode,
            input.reader_policy,
            copy_approved=approved,
            administrator=await authz.row_in_scope(input.org, input.namespace, Role.ADMIN),
        )
        if entry.state != "configured":
            rule.access, rule.reason = "unavailable", "connection_" + entry.state
            rule.explanation = "The owner's connection is unavailable for this selection."
        await connections._owner(input.org, owner)
        if not await authz.row_in_scope(input.org, input.namespace, Role.WRITER):
            raise AuthInsufficientError("destination writer access is required")
        if entry.state == "configured":
            await connections._validate(account)
        refresh: Literal["not_applicable", "enabled", "disabled"] = "not_applicable"
        if input.mode == "selected_refresh":
            refresh = "enabled" if settings.knowledge_source_sync_enabled else "disabled"
        return MCPSourcePreviewOutput(
            namespace=input.namespace,
            mode=input.mode,
            rule=rule,
            connection_state=entry.state,
            refresh_state=refresh,
            selected_count=len(input.resource_ids),
            checked_at=datetime.now(UTC).isoformat(),
            limitations=[
                "Remote compatibility and selected-resource readability are unchecked.",
                "No upstream ACL, complete enumeration, deletion or group evidence is available.",
                "Only selected identities are included; future matching items are excluded.",
                "Preview grants no authority; mutation and every later read revalidate access.",
            ],
        )
