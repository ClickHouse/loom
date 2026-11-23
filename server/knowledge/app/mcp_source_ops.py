"""Owner-bound fixed MCP selections on the existing source lifecycle and stores."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from server.auth import authz, scim
from server.core import request_work
from server.core.ch import own_write_reads
from server.core.cursors import decode_bound_cursor, encode_bound_cursor
from server.core.diagnostics import private_work
from server.core.errors import ValidationError
from server.knowledge.api.models_mcp_connections import MCPSlotInput
from server.knowledge.api.models_mcp_sources import (
    MCPSourceAddInput,
    MCPSourceEntry,
    MCPSourceInput,
    MCPSourceInspectInput,
    MCPSourceInspectOutput,
    MCPSourceListInput,
    MCPSourceListOutput,
    MCPSourceOutput,
    MCPSourceRemoveInput,
    MCPSourceSummary,
    MCPSourceUpdateInput,
)
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import mcp_connections as connections
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import source_push, sources
from server.knowledge.app.mcp_contracts import decode_source, encode_source
from server.knowledge.app.mcp_source_preview import access_rule
from server.knowledge.domain.mcp_sources import (
    CopyConsent,
    MCPSourceContractError,
    ResourceIdentity,
    SourceContract,
    selection_sha256,
)
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource
from server.knowledge.store import connected_accounts as account_store
from server.knowledge.store import sources as store
from server.operations.base import Role
from server.settings import settings


class _Refused(ValidationError):
    def __init__(self) -> None:
        super().__init__("MCP_SOURCE_UNAVAILABLE", code="MCP_SOURCE_UNAVAILABLE")


def source_audit_subject(_input: object, _response: object) -> dict[str, str]:
    """Namespace audit readers do not inherit private source metadata access."""
    return {"kind": "mcp"}


@asynccontextmanager
async def _private() -> AsyncIterator[None]:
    try:
        if not request_work.mark_mirrored():
            raise _Refused()
        with private_work(), own_write_reads():
            yield
    except _Refused:
        raise
    except Exception as e:  # noqa: BLE001 — source errors must not disclose private metadata
        del e
        raise _Refused() from None


async def _caller(org: str, namespace: str, expected: str | None = None) -> str:
    owner = await connections._owner(org, expected)
    if not await authz.row_in_scope(org, namespace, Role.WRITER):
        raise _Refused()
    return owner


async def _destination(org: str, namespace: str, owner: str) -> None:
    await _caller(org, namespace, owner)
    await sources._require_source_namespace(org, namespace)
    await sources._require_no_erasure(org, namespace)


@asynccontextmanager
async def _locks(org: str, namespace: str, owner: str, slot: str) -> AsyncIterator[None]:
    connections._check_slot(slot)
    async with scim.subject_leases([owner]):
        async with accounts.account_lease(org, owner, slot):
            async with sources.sources_lease(org, namespace):
                await _destination(org, namespace, owner)
                yield


def _identity(org: str, namespace: str, owner: str, slot: str, request_id: str) -> tuple[str, str]:
    nonce = str(UUID(request_id))
    key = json.dumps(
        [org, namespace, owner, slot, nonce],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    source_id = uuid5(NAMESPACE_URL, key)
    return "mcp-" + source_id.hex, str(source_id)


def _owned(src: KnowledgeSource | None, org: str, namespace: str, owner: str) -> bool:
    return src is not None and (
        src.org,
        src.namespace,
        src.owner_subject,
        src.kind,
        src.credential_mode,
    ) == (org, namespace, owner, "mcp", "connected")


async def _get(input: MCPSourceInput, owner: str) -> KnowledgeSource:
    src = await store.latest_source(input.org, input.namespace, input.name)
    if not _owned(src, input.org, input.namespace, owner):
        raise _Refused()
    assert src is not None
    return src


def _contract(src: KnowledgeSource) -> SourceContract:
    if src.source_contract_version != 1 or src.config or src.items:
        raise MCPSourceContractError()
    contract = decode_source(src.generic_config_json, src.generic_items_json)
    if (src.connection_slot, src.connection_id) != (contract.slot, contract.generation):
        raise MCPSourceContractError()
    return contract


def _with_contract(src: KnowledgeSource, contract: SourceContract) -> KnowledgeSource:
    config, items = encode_source(contract)
    return replace(
        src,
        generic_config_json=config,
        generic_items_json=items,
        access=MIRRORED_ACCESS,
    )


async def _selection(
    input: MCPSourceAddInput | MCPSourceUpdateInput,
    owner: str,
    binding: SourceContract,
) -> SourceContract:
    resources = tuple(ResourceIdentity(type="resource", id=r) for r in input.resource_ids)
    consent = None
    policy = input.reader_policy if isinstance(input, MCPSourceAddInput) else binding.reader_policy
    if policy == "collection_copy":
        if not input.confirm_copy_authority or input.mode == "live":
            raise _Refused()
        if not await authz.row_in_scope(input.org, input.namespace, Role.ADMIN):
            raise _Refused()
        bound = await authority.validate_connection(input.org, owner, binding)
        if not bound.profile.metadata.allow_collection_copy:
            raise _Refused()
        consent = CopyConsent(
            version=1,
            publisher=owner,
            namespace=input.namespace,
            resources_sha256=selection_sha256(resources),
        )
    return SourceContract(
        version=1,
        slot=binding.slot,
        generation=binding.generation,
        profile_id=binding.profile_id,
        profile_sha256=binding.profile_sha256,
        resources=resources,
        mode=input.mode,
        reader_policy=policy,
        scope_revision=binding.scope_revision,
        copy_consent=consent,
        label=input.label,
    )


async def _proposed(input: MCPSourceAddInput, owner: str) -> KnowledgeSource:
    connections._check_slot(input.slot)
    account = await account_store.get(input.org, owner, input.slot)
    if account is None:
        raise _Refused()
    connections._row_scope(account, input.org, owner, input.slot)
    await connections._validate(account)
    _, envelope = connections._binding(account)
    binding = SourceContract(
        version=1,
        slot=input.slot,
        generation=account.connection_id,
        profile_id=envelope.profile_id,
        profile_sha256=envelope.profile_sha256,
        mode="live",
        reader_policy="owner_only",
        resources=(ResourceIdentity(type="resource", id=input.resource_ids[0]),),
    )
    contract = await _selection(input, owner, binding)
    name, source_id = _identity(input.org, input.namespace, owner, input.slot, input.request_id)
    return _with_contract(
        KnowledgeSource(
            org=input.org,
            namespace=input.namespace,
            name=name,
            kind="mcp",
            source_id=source_id,
            credential_mode="connected",
            owner_subject=owner,
            created_by=owner,
            connection_slot=input.slot,
            connection_id=account.connection_id,
            source_contract_version=1,
        ),
        contract,
    )


async def _discover(src: KnowledgeSource) -> None:
    result = await connections.handle_mcp_discover(
        MCPSlotInput(org=src.org, slot=src.connection_slot)
    )
    if not result.supported_reads or result.generation != src.connection_id:
        raise _Refused()


def _entry(src: KnowledgeSource) -> MCPSourceEntry:
    entry = MCPSourceEntry(
        name=src.name,
        source_id=src.source_id,
        slot=src.connection_slot,
        namespace=src.namespace,
        state="unsupported" if src.active else "retired",
        sync_enabled=bool(settings.knowledge_source_sync_enabled),
        read_evidence_stale_seconds=settings.knowledge_source_acl_stale_seconds,
        refresh_state="unsupported" if src.active else "retired",
        pending_erasure=src.pending_delete,
    )
    try:
        contract = _contract(src)
    except (ValueError, TypeError, RecursionError):
        return entry
    entry.state = "retired" if not src.active else "paused" if contract.paused else "registered"
    entry.mode, entry.reader_policy, entry.label, entry.scope_revision = (
        contract.mode,
        contract.reader_policy,
        contract.label,
        contract.scope_revision,
    )
    entry.resource_ids = [r.id for r in contract.resources]
    entry.selected_count = len(contract.resources)
    entry.refresh_state = (
        "retired"
        if not src.active
        else "paused"
        if contract.paused
        else "not_applicable"
        if contract.mode == "live"
        else "enabled"
        if entry.sync_enabled
        else "disabled"
    )
    entry.rule = access_rule(
        contract.mode,
        contract.reader_policy,
        copy_approved=contract.copy_consent is not None,
        administrator=True,
    )
    if entry.state in {"paused", "retired"}:
        entry.rule.access = "unavailable"
        entry.rule.reason = "source_" + entry.state
        entry.rule.explanation = (
            "This source is stopped; retained content has no current read grant."
        )
    return entry


def _output(src: KnowledgeSource, *, requested: bool = False) -> MCPSourceOutput:
    return MCPSourceOutput(
        source=_entry(src),
        refresh_requested=requested,
    )


async def _checked_entry(src: KnowledgeSource) -> MCPSourceEntry:
    entry = _entry(src)
    if entry.state != "registered":
        return entry
    try:
        await authority.validate_source(src)
    except authority.MCPAuthorityError:
        entry.local_authority = "unavailable"
        if entry.rule is not None:
            entry.rule.access, entry.rule.reason = "unavailable", "source_authority_unavailable"
            entry.rule.explanation = (
                "Current source authority is unavailable; stored policy grants no access."
            )
    else:
        entry.local_authority = "current"
    return entry


async def handle_mcp_source_add(input: MCPSourceAddInput) -> MCPSourceOutput:
    async with _private():
        owner = await _caller(input.org, input.namespace)
        connections._policy()
        await _destination(input.org, input.namespace, owner)
        proposed = await _proposed(input, owner)
        await _discover(proposed)
        async with _locks(input.org, input.namespace, owner, input.slot):
            connections._policy()
            current = await store.latest_source(input.org, input.namespace, proposed.name)
            if current is not None:
                if (
                    not _owned(current, input.org, input.namespace, owner)
                    or not current.active
                    or current.pending_delete
                    or _contract(current).paused
                    or replace(current, created_at=None, updated_at=None, mirror="") != proposed
                ):
                    raise _Refused()
                await authority.validate_registration(current)
                return _output(current)
            await sources._require_free(
                input.org,
                input.namespace,
                proposed.name,
                owner_subject=owner,
            )
            await _destination(input.org, input.namespace, owner)
            connections._policy()
            await authority.validate_registration(proposed)
            requested = await _request(proposed, only_if_changed=True)
            await _destination(input.org, input.namespace, owner)
            connections._policy()
            await authority.validate_registration(proposed)
            await store.insert_source(proposed)
            return _output(proposed, requested=requested)


async def handle_mcp_source_list(input: MCPSourceListInput) -> MCPSourceListOutput:
    async with _private():
        owner = await _caller(input.org, input.namespace)
        scope = (
            "knowledge.source.mcp_list",
            input.org,
            input.namespace,
            owner,
            str(input.limit),
            str(input.include_retired),
        )
        anchor = datetime(1970, 1, 1, tzinfo=UTC)
        after = ""
        if input.cursor is not None:
            observed, after = decode_bound_cursor(input.cursor, scope)
            if observed != anchor:
                raise _Refused()
        rows = await store.mcp_sources_page(
            input.org,
            input.namespace,
            owner,
            after=after,
            include_retired=input.include_retired,
            limit=input.limit + 1,
        )
        entries = [
            MCPSourceSummary.model_validate(
                (await _checked_entry(src)).model_dump(exclude={"resource_ids"})
            )
            for src in rows[: input.limit]
            if _owned(src, input.org, input.namespace, owner)
        ]
        await _caller(input.org, input.namespace, owner)
        return MCPSourceListOutput(
            sources=entries,
            next_cursor=encode_bound_cursor(scope, anchor, rows[input.limit - 1].name)
            if len(rows) > input.limit
            else None,
        )


async def handle_mcp_source_inspect(input: MCPSourceInspectInput) -> MCPSourceInspectOutput:
    async with _private():
        owner = await _caller(input.org, input.namespace)
        name = input.name
        expected_id = ""
        if name:
            if input.slot or input.request_id:
                raise _Refused()
        else:
            connections._check_slot(input.slot)
            name, expected_id = _identity(
                input.org, input.namespace, owner, input.slot, input.request_id
            )
        src = await store.latest_source(input.org, input.namespace, name)
        if (
            src
            and expected_id
            and (src.source_id != expected_id or src.connection_slot != input.slot)
        ):
            raise _Refused()
        entry = (
            await _checked_entry(src)
            if _owned(src, input.org, input.namespace, owner) and src
            else None
        )
        await _caller(input.org, input.namespace, owner)
        return MCPSourceInspectOutput(found=entry is not None, source=entry)


def _require_running(src: KnowledgeSource, contract: SourceContract) -> None:
    if not src.active or src.pending_delete or contract.paused:
        raise _Refused()


async def _request(src: KnowledgeSource, *, only_if_changed: bool = False) -> bool:
    contract = _contract(src)
    if contract.mode == "live":
        return False
    state = await sources._read_state(src)
    marker = authority.source_scope_sha256(src, contract)
    detail = json.loads(state.get("detail") or "{}")
    if only_if_changed and detail.get("mcp_selection") == marker:
        return False
    await sources._update_state(
        src,
        state,
        {"requested_at": sources._now_iso(), "full": True, "mcp_selection": marker},
    )
    return True


async def handle_mcp_source_update(input: MCPSourceUpdateInput) -> MCPSourceOutput:
    async with _private():
        owner = await _caller(input.org, input.namespace)
        original = await _get(input, owner)
        async with _locks(input.org, input.namespace, owner, original.connection_slot):
            src = await _get(input, owner)
            if src != original:
                raise _Refused()
            contract = _contract(src)
            _require_running(src, contract)
            if (
                input.if_scope_revision is not None
                and input.if_scope_revision != contract.scope_revision
            ):
                raise _Refused()
            chosen = await _selection(input, owner, contract)
            if chosen.model_dump(exclude={"label"}) != contract.model_dump(exclude={"label"}):
                chosen = SourceContract.model_validate(
                    {
                        **chosen.model_dump(),
                        "scope_revision": contract.scope_revision + 1,
                    }
                )
            updated = _with_contract(src, chosen)
            await authority.validate_registration(src)
            await authority.validate_registration(updated)
            if updated == src:
                requested = await _request(src, only_if_changed=True)
                return _output(src, requested=requested)
            await source_push.retire(src)
            requested = await _request(updated, only_if_changed=True)
            await _destination(input.org, input.namespace, owner)
            await authority.validate_registration(updated)
            await store.insert_source(updated)
            return _output(updated, requested=requested)


async def _pause(input: MCPSourceInput, *, paused: bool) -> MCPSourceOutput:
    async with _private():
        owner = await _caller(input.org, input.namespace)
        original = await _get(input, owner)
        async with _locks(input.org, input.namespace, owner, original.connection_slot):
            src = await _get(input, owner)
            if src != original or not src.active or src.pending_delete:
                raise _Refused()
            contract = _contract(src)
            unpaused = _with_contract(src, contract.model_copy(update={"paused": False}))
            await authority.validate_registration(unpaused)
            updated = _with_contract(src, contract.model_copy(update={"paused": paused}))
            await source_push.retire(src)
            await _destination(input.org, input.namespace, owner)
            await authority.validate_registration(unpaused)
            await store.insert_source(updated)
            return _output(updated)


async def handle_mcp_source_pause(input: MCPSourceInput) -> MCPSourceOutput:
    return await _pause(input, paused=True)


async def handle_mcp_source_resume(input: MCPSourceInput) -> MCPSourceOutput:
    return await _pause(input, paused=False)


async def handle_mcp_source_refresh(input: MCPSourceInput) -> MCPSourceOutput:
    async with _private():
        owner = await _caller(input.org, input.namespace)
        original = await _get(input, owner)
        async with _locks(input.org, input.namespace, owner, original.connection_slot):
            src = await _get(input, owner)
            if src != original:
                raise _Refused()
            contract = _contract(src)
            _require_running(src, contract)
            if contract.mode == "live":
                raise _Refused()
            await authority.validate_registration(src)
            await _request(src)
            return _output(src, requested=True)


async def handle_mcp_source_remove(input: MCPSourceRemoveInput) -> MCPSourceOutput:
    async with _private():
        owner = await _caller(input.org, input.namespace)
        original = await _get(input, owner)
        if input.confirm != input.name:
            raise _Refused()
        async with _locks(input.org, input.namespace, owner, original.connection_slot):
            src = await _get(input, owner)
            if src != original:
                raise _Refused()
            pending = src.pending_delete
            if input.delete_documents:
                pending = True
                if src.access == MIRRORED_ACCESS:
                    pending = await sources._queue_retirement(src) or pending
            retired = replace(src, active=False, pending_delete=pending)
            await source_push.retire(src)
            await _destination(input.org, input.namespace, owner)
            await store.insert_source(retired)
            result = _output(retired)
            if pending:
                result.copies = "erasure_requested"
            return result
