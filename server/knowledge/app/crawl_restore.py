"""Offline restore invalidates approvals while preserving durable refusal identities."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from server.core.ch import get_client, set_query_tag
from server.erasure.app import crawl_person
from server.knowledge.app import crawl_exclusions
from server.knowledge.app import sources as source_ops
from server.knowledge.app.connected_restore import offline_refusal
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import crawl_exclusions as exclusions_store
from server.knowledge.store import sources as source_store
from server.settings import settings


async def _retire(source: KnowledgeSource, *, ch: Any) -> None:
    async with source_ops.sources_lease(source.org, source.namespace):
        pending = await source_ops._queue_retirement(source)
        current = await source_store.latest_source(source.org, source.namespace, source.name, ch=ch)
        if (
            current is not None
            and current.credential_mode == "crawl"
            and current.source_id == source.source_id
        ):
            await source_store.insert_source(
                dataclasses.replace(
                    current,
                    active=False,
                    approval_enc="",
                    crawl_generation="",
                    pending_delete=pending,
                ),
                ch=ch,
            )


async def restore_all(*, apply: bool, ch: Any) -> None:
    """No restored crawl approval survives; unverified historical refusal state blocks serving.

    This cannot recover missing post-backup person identity proofs. A mismatched
    refusal registry remains closed for verified repair, never reset to empty.
    """
    if ch is not get_client():
        raise RuntimeError("crawl_restore_target_mismatch")
    set_query_tag("knowledge.crawl.restore")
    if why := await offline_refusal(ch):
        raise RuntimeError(why)
    maximum = settings.knowledge_crawl_registry_scan_limit
    orgs = await exclusions_store.restore_orgs(maximum=maximum, ch=ch)
    async with asyncio.timeout(settings.knowledge_crawl_person_cleanup_timeout_s):
        for org in orgs:
            async with crawl_exclusions.org_lease(
                org,
                ttl_s=settings.knowledge_crawl_lease_ttl_s,
                wait_s=settings.knowledge_crawl_lease_wait_s,
                ch=ch,
            ):
                # Reads never initialize or repair a stale refusal key or pending manifest.
                index = await crawl_exclusions.Index.open(org, ch=ch)
                history = await source_store.crawl_history(org, maximum=maximum, ch=ch)
                if not apply:
                    continue
                if why := await offline_refusal(ch):
                    raise RuntimeError(why)
                seen = set()
                for source in history:
                    identity = source.namespace, source.source_id
                    if identity not in seen:
                        await _retire(source, ch=ch)
                        seen.add(identity)
                await crawl_person.invalidate_restored_approvals(org, ch=ch)
                await index.version()
