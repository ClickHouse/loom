"""Registration of an owner's mirrored source with a checked connection."""

from __future__ import annotations

import dataclasses
import time
import uuid
from typing import Any

from server.auth import authz, machines
from server.auth.context import current_auth
from server.clients._checked_access import CheckedAccess
from server.core import request_work
from server.core.ch import get_client, set_query_tag
from server.core.diagnostics import private_work
from server.core.errors import OverloadedError, ValidationError
from server.knowledge.api.models_sources import (
    KnowledgeSourceAddInput,
    KnowledgeSourceAddOutput,
    KnowledgeSourceCheckOutput,
    KnowledgeSourceProbe,
    KnowledgeSourceUpdateInput,
    KnowledgeSourceUpdateOutput,
)
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import sources as source_ops
from server.knowledge.domain import sources as domain
from server.knowledge.domain.connected_accounts import KINDS
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import connected_accounts as account_store
from server.knowledge.store import sources as source_store
from server.operations.base import Role
from server.settings import settings

# The first binary that understands the source's owner and connection fence.
_CONNECTIONS_MIGRATION = 111


async def require_operator(source: KnowledgeSource) -> None:
    """Local sync/retirement controls admit the owner or the namespace administrator."""
    auth = current_auth()
    if source.credential_mode != "connected" or not request_work.mark_mirrored():
        raise accounts.unavailable()
    if await authz.row_in_scope(source.org, source.namespace, Role.ADMIN):
        if current_auth() is not auth:
            raise accounts.unavailable()
        return
    org, owner = await accounts.own_subject(auth)
    if (
        org != source.org
        or owner != source.owner_subject
        or not await authz.row_in_scope(org, source.namespace, Role.WRITER)
        or current_auth() is not auth
    ):
        raise accounts.unavailable()


async def add(
    input: KnowledgeSourceAddInput,
    *,
    name: str,
    kind: str,
    config: dict[str, Any],
    items: list[dict[str, Any]],
    sent: list[str],
) -> KnowledgeSourceAddOutput:
    if not request_work.mark_mirrored():
        raise accounts.unavailable()
    with private_work():
        return await _add(input, name=name, kind=kind, config=config, items=items, sent=sent)


async def _add(
    input: KnowledgeSourceAddInput,
    *,
    name: str,
    kind: str,
    config: dict[str, Any],
    items: list[dict[str, Any]],
    sent: list[str],
) -> KnowledgeSourceAddOutput:
    set_query_tag("knowledge.connected_sources.add")
    accounts.client(kind)  # An unconfigured provider refuses before credential access.
    if input.credential or kind not in KINDS:
        raise ValidationError(
            "a connected source takes its own account, without a supplied credential",
            code="CREDENTIAL_REFUSED",
        )
    if not await machines.fleet_at(get_client(), _CONNECTIONS_MIGRATION):
        raise OverloadedError(
            "not every process enforces connected-source ownership yet; retry after the rollout",
            retry_after_s=30,
        )
    org, owner = await accounts.own_subject(current_auth())
    if org != input.org:
        raise accounts.unavailable()
    await source_ops._require_source_namespace(org, input.namespace)
    await source_ops._require_no_erasure(org, input.namespace)
    await source_ops._require_free(org, input.namespace, name, owner_subject=owner)
    async with accounts.account_lease(org, owner, kind):
        account = await account_store.get(org, owner, kind)
        if account is None:
            raise accounts.unavailable()
        source = KnowledgeSource(
            org=org,
            namespace=input.namespace,
            name=name,
            kind=kind,
            source_id=str(uuid.uuid4()),
            config=config,
            items=items,
            access=domain.MIRRORED_ACCESS,
            credential_mode="connected",
            owner_subject=owner,
            connection_id=account.connection_id,
            created_by=owner,
        )
    deadline = time.monotonic() + settings.knowledge_source_probe_timeout_s
    access = await accounts.checked_for(source, deadline=deadline, pending=True)
    items = domain.unique_items(await source_ops._resolved(kind, items, access))
    source_ops._require_count(kind, items, sent)
    probe, items = await source_ops._probe_items(kind, items, config, access)
    source = dataclasses.replace(source, items=items)
    await access.check()
    out = KnowledgeSourceAddOutput(
        name=name,
        kind=kind,
        namespace=input.namespace,
        items=items,
        include=source_ops._answered_include(source),
        config=domain.id_list_config(source) or {},
        probe=KnowledgeSourceProbe(checked=probe.checked, sampled=probe.sampled),
        dry_run=input.dry_run,
    )
    if input.dry_run:
        return out
    async with accounts.account_lease(org, owner, kind):
        await access.check()
        async with source_ops.sources_lease(org, input.namespace):
            await access.check()
            await source_ops._require_free(org, input.namespace, name, owner_subject=owner)
            await source_store.write_state(org, input.namespace, name, state_value="", detail={})
            await source_store.insert_source(source)
    return out


async def check(source: KnowledgeSource) -> KnowledgeSourceCheckOutput:
    """A probe admits the owner or an administrator while retaining that caller's standing."""
    from server.knowledge.app import source_probes

    if not request_work.mark_mirrored():
        raise accounts.unavailable()
    with private_work():
        deadline = time.monotonic() + settings.knowledge_source_probe_timeout_s
        authority = await accounts.resolve_probe(source, deadline=deadline)
        checked = source_probes.sample(source.items)
        if authority.requester != source.owner_subject:
            # An administrator may check source-level credential standing. Reading
            # the owner's selected items would make item failures an existence oracle.
            await authority.validate()
            return KnowledgeSourceCheckOutput(
                name=source.name,
                reachable=True,
                summary="the connected credential was accepted",
                checked_at=source_ops._now_iso(),
            )

        async def probe(access: CheckedAccess) -> source_probes.Probe:
            return await source_probes.probe(source.kind, source.config, access, items=checked)

        checked_at = source_ops._now_iso()
        try:
            await authority.run(probe)
        except source_probes.CredentialRefused:
            return KnowledgeSourceCheckOutput(
                name=source.name,
                reachable=False,
                summary="the connected credential was refused",
                checked_at=checked_at,
            )
        except source_probes.SourceUnreachable:
            return KnowledgeSourceCheckOutput(
                name=source.name,
                reachable=False,
                summary="the connected source is unavailable",
                checked_at=checked_at,
            )
        return KnowledgeSourceCheckOutput(
            name=source.name,
            reachable=True,
            checked=len(checked),
            sampled=len(checked) < len(source.items),
            summary="the connected source was reached",
            checked_at=checked_at,
        )


async def update(
    source: KnowledgeSource, input: KnowledgeSourceUpdateInput
) -> KnowledgeSourceUpdateOutput:
    """Only the owner chooses new reads with their connection; provider waits hold no lease."""
    if not request_work.mark_mirrored():
        raise accounts.unavailable()
    with private_work():
        deadline = time.monotonic() + settings.knowledge_source_probe_timeout_s
        authority = await accounts.resolve(
            source.org,
            source.namespace,
            source.name,
            deadline=deadline,
            expected_source_id=source.source_id,
        )

        async def prepare(access: CheckedAccess) -> source_ops._UpdatePlan | None:
            return await source_ops._update_plan(source, input, credential=access)

        plan = await authority.run(prepare)
        if plan is None:
            return KnowledgeSourceUpdateOutput(
                name=source.name,
                items=source.items,
                include=source_ops._answered_include(source),
                config=domain.id_list_config(source) or {},
            )
        change = source_ops._updated(source, plan.added, plan.removed, plan.include)
        async with accounts.publication_guard(source):
            # The guard retains exactly the registration used by the probe, including
            # its items and config; a replacement cannot inherit that result.
            await authority.validate()
            from server.knowledge.app.source_push import retire

            await retire(source)
            await source_store.insert_source(change.source)
            state = await source_ops._read_state(change.source)
            await source_ops._update_state(
                change.source, state, {"requested_at": source_ops._now_iso(), "full": True}
            )
        return KnowledgeSourceUpdateOutput(
            name=source.name,
            items=change.source.items,
            include=source_ops._answered_include(change.source),
            config=domain.id_list_config(change.source) or {},
            added=change.added,
            removed=change.removed,
            probe=KnowledgeSourceProbe(checked=plan.probe.checked, sampled=plan.probe.sampled),
        )
