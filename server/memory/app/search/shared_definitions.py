"""A shared definition is served only while its namespace-wide source exists and is not
labelled `medium` or `high`.

`loom.shared` carries no label of its own, so a definition's shared copy would stay in
every search after its source was relabelled or deleted. This check reads the sources of
the shared definition hits a search found, and only then: a search with no such hit reads
nothing more and serves what it did before.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import server.memory.store as memory_repo
from server.core.error_summary import error_summary
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.definition_check import sensitivity_lapse
from server.memory.domain.definitions import DEFINITION_KEY_PREFIX
from server.memory.domain.search_types import hit_row, is_shared_row
from server.observability import degradations

log = logging.getLogger(__name__)
_PROVIDER = "memory.search"
_EFFECT = "shared_definitions_withheld"
_WITHHELD = f"{_PROVIDER}: {_EFFECT}"


def _shared_definition(hit: Any, namespace: str) -> tuple[str, str] | None:
    """The (namespace, key) of a hit that is a shared definition; None for any other."""
    ns, key = hit_row(hit, namespace)
    return (ns, key) if is_shared_row(hit) and key.startswith(DEFINITION_KEY_PREFIX) else None


async def withheld_definition_flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
    """For each hit, whether it is a shared definition to withhold: its namespace-wide
    source is gone or labelled `medium` or `high`, or a source could not be read (fails
    closed, noted on the response's `degraded`)."""
    wanted = [_shared_definition(h, namespace) for h in hits]
    keys_by_ns: dict[str, set[str]] = {}
    for ns, key in filter(None, wanted):
        keys_by_ns.setdefault(ns, set()).add(key)
    if not keys_by_ns:
        return [False] * len(hits)
    try:
        heads = await asyncio.gather(
            *(
                memory_repo.get_many_by_keys(
                    memory_repo.Identity(org=org, namespace=ns, agent="-"),
                    sorted(keys),
                    scope=WHOLE_NAMESPACE,
                )
                for ns, keys in keys_by_ns.items()
            )
        )
    except Exception as e:  # noqa: BLE001 — an unread source withholds its definition
        log.warning(
            "shared definitions withheld for %s/%s, a source was not read: %s",
            org,
            namespace,
            error_summary(e),
        )
        # Once per request: a parked page's refill window reads its sources again.
        if not any(d.startswith(_WITHHELD) for d in degradations.current()):
            degradations.note(_PROVIDER, _EFFECT, e, health=False)
        return [w is not None for w in wanted]
    served = {
        (ns, head.memory_key)
        for ns, held in zip(keys_by_ns, heads, strict=True)
        for head in held
        # The label alone: grading every source would cost a search more than a read.
        if sensitivity_lapse(head.sensitivity, graded="") is None
    }
    return [w is not None and w not in served for w in wanted]


async def without_withheld_definitions[H](org: str, namespace: str, hits: list[H]) -> list[H]:
    """`hits` less the shared definitions `withheld_definition_flags` withholds; the same
    list when none is withheld."""
    flags = await withheld_definition_flags(org, namespace, hits)
    if not any(flags):
        return hits
    return [h for h, withheld in zip(hits, flags, strict=True) if not withheld]
