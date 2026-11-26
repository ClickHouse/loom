"""Authorize the supporting source before explaining a source-dependent memory claim."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from server.auth.authz import row_in_scope
from server.core.hashing import served_row_value
from server.identity.app.request_scope import request_narrowed
from server.knowledge.app.document_reads import ordinary_source_uris
from server.memory.api.models_procedure import ProcedureStatus
from server.memory.domain.decision_schema import SOURCE_CHANGED_TAG

log = logging.getLogger(__name__)


def _changed_document(row: Mapping[str, Any]) -> tuple[str, str] | None:
    if SOURCE_CHANGED_TAG not in (row.get("tags") or ()):
        return None
    resource = row.get("source_resource_id")
    if not isinstance(resource, str) or not resource.startswith("knowledge:"):
        return None
    namespace, _, citation = resource.removeprefix("knowledge:").partition("/")
    # A '#' inside a URI or heading makes this citation ambiguous without another read.
    if not namespace or citation.count("#") > 1:
        return None
    uri, _, _section = citation.partition("#")
    return (namespace, uri) if uri else None


async def _readable_documents(org: str, namespace: str, uris: set[str]) -> set[tuple[str, str]]:
    """A current ordinary document must exist in a namespace the caller may read."""
    try:
        if not await row_in_scope(org, namespace, hides_answer=False):
            return set()
        return {(namespace, uri) for uri in await ordinary_source_uris(org, namespace, uris)}
    except Exception as exc:  # noqa: BLE001 — optional source evidence cannot fail a memory read
        log.debug("Guidance source read unavailable: %s", type(exc).__name__)
        return set()


async def guidance_source_states(org: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The recorded change marker applies only to a currently readable source."""
    references = [_changed_document(row) for row in rows]
    by_ns: dict[str, set[str]] = defaultdict(set)
    if not request_narrowed():
        for ref in references:
            if ref:
                by_ns[ref[0]].add(ref[1])
    visible: set[tuple[str, str]] = set()
    if by_ns:
        reads = (_readable_documents(org, ns, uris) for ns, uris in by_ns.items())
        for checked in await asyncio.gather(*reads):
            visible.update(checked)
    # Readability confirms a change marker may be shown. It does not prove a revision matches.
    return ["changed" if ref and ref in visible else "unknown" for ref in references]


def procedure_status_for_row(
    row: dict[str, Any],
) -> ProcedureStatus | Literal["unknown", "not_applicable"]:
    from server.memory.app.procedure import _procedure_status

    if row.get("memory_type") != "procedural":
        return "not_applicable"
    value = served_row_value(row) if not row.get("value_truncated") else None
    return _procedure_status(value) if isinstance(value, dict) else "unknown"
