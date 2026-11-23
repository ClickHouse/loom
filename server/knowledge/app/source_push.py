"""Local retirement and resealing of retained source notification registrations."""

from __future__ import annotations

import dataclasses
from typing import Any

from server.core.ch import sequential_reads, set_query_tag
from server.core.lease_writes import fenced
from server.core.secrets_at_rest import resealed, unseal
from server.knowledge.app import sources
from server.knowledge.domain import source_push as domain
from server.knowledge.domain.source_push import PushRegistration
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import source_push as store
from server.memory.app.provenance import writer_actor


async def _put(reg: PushRegistration, *, ch: Any = None) -> None:
    async with fenced(domain.writer_lease_name(reg.org, reg.namespace), ch=ch) as uncertain:
        uncertain(True)
        await store.put(dataclasses.replace(reg, actor=writer_actor("process:source-push")), ch=ch)
        uncertain(False)


async def retire(src: KnowledgeSource) -> None:
    """Retire a stored notification binding only for the caller's explicit source action."""
    current = await store.get(src.org, src.namespace, src.source_id)
    if current and current.status != "retired":
        await _put(dataclasses.replace(current, status="retired", secret_enc=""))


async def reseal_registry(*, dry_run: bool, ch: Any = None) -> tuple[int, list[str]]:
    """Preserve retained registration state while rotating its sealed secret locally."""
    after = ("", "", "")
    count = 0
    lost: list[str] = []
    set_query_tag("knowledge.source.reseal")
    while page := await store.sealed_page(after, ch=ch):
        for observed in page:
            set_query_tag("knowledge.source.reseal")
            async with sources.sources_lease(observed.org, observed.namespace, ch=ch):
                with sequential_reads():
                    await sources._require_no_erasure(observed.org, observed.namespace, ch=ch)
                    current = await store.get(
                        observed.org, observed.namespace, observed.source_id, ch=ch
                    )
                    if current is None or not current.secret_enc:
                        continue
                    new = resealed(domain.SECRET_PURPOSE, current.secret_enc)
                    if new is None:
                        if not unseal(domain.SECRET_PURPOSE, current.secret_enc, warn=False):
                            lost.append(f"{current.org}/{current.namespace}/{current.source_id}")
                        continue
                    count += 1
                    if not dry_run:
                        await _put(dataclasses.replace(current, secret_enc=new), ch=ch)
        after = (page[-1].org, page[-1].namespace, page[-1].source_id)
    return count, lost
