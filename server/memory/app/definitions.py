"""memory.definitions.list: a namespace's shared definitions and whether each is governed."""

from __future__ import annotations

import json
from typing import Any

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.hashing import served_content_sha256
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_lifecycle import (
    DefinitionEntry,
    LostName,
    MemoryDefinitionsListInput,
    MemoryDefinitionsListOutput,
)
from server.memory.app.sensitivity_level import decoded_text, detect_level
from server.memory.domain.definition_check import (
    LapseReason,
    SharedDefinition,
    Source,
    copy_lapse,
    lost_names,
    sensitivity_lapse,
    source_lapse,
)
from server.memory.domain.definitions import written_names
from server.memory.store import shared as shared_repo
from server.memory.store.rows import MemoryRow
from server.settings import settings


async def handle_memory_definitions_list(
    input: MemoryDefinitionsListInput,
) -> MemoryDefinitionsListOutput:
    org, namespace = input.org, input.namespace
    ceiling = settings.memory_shared_definitions_max
    rows = await shared_repo.definition_rows(org, namespace, ceiling)
    listed = [_shared(r) for r in rows[:ceiling]]
    reasons = {d.memory_key: copy_lapse(d, org=org, namespace=namespace) for d in listed}
    to_check = [d for d in listed if reasons[d.memory_key] is None]
    heads = await memory_repo.get_many_by_keys(
        memory_repo.Identity(org=org, namespace=namespace, agent="-"),
        [d.memory_key for d in to_check],
        scope=WHOLE_NAMESPACE,
        strong=True,
    )
    by_key = {h.memory_key: h for h in heads}
    now = naive_utc_now()
    read_before = 0  # characters graded so far, so a long run yields to the loop
    for d in to_check:
        head = by_key.get(d.memory_key)
        source = _source(head) if head is not None else None
        reason = source_lapse(d, source, now=now)
        if reason is None and head is not None:  # a governed copy has a head
            # The value itself is graded, whatever its label: an import or a restore
            # leaves the label empty.
            text = await decoded_text(head.value)
            level = await detect_level(text, read_before=read_before)
            read_before += len(text)
            reason = sensitivity_lapse(head.sensitivity, level)
        reasons[d.memory_key] = reason
    lost = lost_names([d for d in listed if reasons[d.memory_key] is None])
    entries = [_entry(d, reasons[d.memory_key], lost) for d in listed]
    entries += [
        DefinitionEntry(memory_key=r["memory_key"], promoted_at=r["promoted_at"], capped=True)
        for r in rows[ceiling:]
    ]
    return MemoryDefinitionsListOutput(
        count=int(rows[0]["total"]) if rows else 0, ceiling=ceiling, definitions=entries
    )


def _shared(row: dict[str, Any]) -> SharedDefinition:
    return SharedDefinition(
        memory_key=row["memory_key"],
        value=json.loads(row["value"]),  # every shared value is written as JSON
        promoted_from=tuple(row["promoted_from"]),
        promoted_at=row["promoted_at"],
        promotion_event=str(row["promotion_event"]),
    )


def _source(head: MemoryRow) -> Source:
    return Source(
        status=head.status,
        invalid_at=head.invalid_at,
        expires_at=to_naive_utc(head.expires_at),
        valid_to=to_naive_utc(head.valid_to),
        served_sha256=served_content_sha256(head.value),
    )


def _entry(
    d: SharedDefinition, reason: LapseReason | None, lost: dict[str, list[tuple[str, str]]]
) -> DefinitionEntry:
    names = written_names(d.value)
    return DefinitionEntry(
        memory_key=d.memory_key,
        promoted_at=d.promoted_at,
        term=names[0] if names else None,
        aliases=names[1:],
        verdict="governed" if reason is None else "lapsed",
        lapse_reason=reason,
        lost=[LostName(name=n, winner=w) for n, w in lost.get(d.memory_key, [])],
    )
