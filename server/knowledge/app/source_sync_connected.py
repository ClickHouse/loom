"""Personal page sources reuse their native turn inside the checked mirror boundary."""

from __future__ import annotations

import dataclasses
import functools
import json
from datetime import UTC, datetime

from server.knowledge.app import (
    connected_accounts,
    mirrors,
    private_sync,
    source_sync_linear,
    source_sync_notion,
)
from server.knowledge.app import source_sync as sync
from server.knowledge.app import sources as source_ops
from server.knowledge.domain.sources import KnowledgeSource, json_object, mirror_uri_suffix
from server.knowledge.store import connected_accounts as account_store
from server.knowledge.store import documents
from server.knowledge.store import mirrors as mirror_store
from server.knowledge.store import sources as source_store
from server.workers import pass_context
from server.workers.status import set_worker_identity


async def ensure_current(src: KnowledgeSource) -> None:
    """Repair a missed lifecycle hook without stopping another namespace's source."""
    try:
        await sync.require_current(src)
        return
    except connected_accounts.OwnerUnavailable:
        pass
    async with connected_accounts.account_lease(src.org, src.owner_subject, src.kind):
        async with source_ops.sources_lease(src.org, src.namespace):
            current = await source_store.latest_source(src.org, src.namespace, src.name)
            if (
                current is None
                or not current.active
                or current.source_id != src.source_id
                or current.connection_id != src.connection_id
                or current.owner_subject != src.owner_subject
                or current.credential_mode != "connected"
                or current.kind != src.kind
                or current.items != src.items
                or current.config != src.config
            ):
                raise sync.SourceGone(sync._cursor_of(src))
            account = await account_store.get(src.org, src.owner_subject, src.kind)
            if account is None:
                raise connected_accounts.unavailable()
            try:
                await connected_accounts._source_standing(current, account)
                return
            except connected_accounts.OwnerUnavailable:
                pass
            pending = await source_ops._queue_retirement(current)
            from server.knowledge.app.source_push import retire

            await retire(current)
            await source_store.insert_source(
                dataclasses.replace(
                    current,
                    active=False,
                    credential_enc="",
                    secret_enc="",
                    pending_delete=pending or current.pending_delete,
                )
            )
            await source_store.write_state(
                current.org,
                current.namespace,
                current.name,
                state_value="",
                detail={"status": "owner_inactive"},
            )
    raise sync.SourceGone(sync._cursor_of(src))


async def turn(src: KnowledgeSource, state: dict[str, str], run: sync.Pass) -> sync.TurnResult:
    """Only a complete full listing with current authority vouches for these copies."""
    await sync.require_current(src)
    mirror = await mirrors.ensure_mirror(
        src.org,
        src.namespace,
        still_current=functools.partial(sync.require_current, src),
    )
    own = await sync.mirror_state(src, mirror)
    detail = json_object(own.get("detail", ""))
    asked = json_object(state.get("detail", ""))
    detail.update({key: asked[key] for key in ("requested_at", "full") if key in asked})
    src = dataclasses.replace(src, mirror=mirror)
    started = datetime.now(UTC)
    async with private_sync.mirror_costs(src.org, mirror, sync.WORKER):
        set_worker_identity(sync.WORKER, org=src.org, namespace=mirror)
        # A personal mirror always lists all items. Linear refreshes team ACLs even
        # for unchanged text; Notion's owner entry is fixed by its source generation.
        native_turn = source_sync_notion.turn if src.kind == "notion" else source_sync_linear.turn
        result = await native_turn(src, {"detail": json.dumps(detail), "state_value": ""}, run)
        result.mirror = mirror
        if (
            result.complete
            and result.status == "ok"
            and result.full
            and not run.budget.exhausted()
            and not await documents.publications_owed(
                src.org, mirror, mirror_uri_suffix(src.source_id)
            )
        ):
            pass_context.check_stop()
            async with sync.publication_guard(src):
                await mirror_store.record_listing(src.org, mirror, src.source_id, src.name, started)
    return result
