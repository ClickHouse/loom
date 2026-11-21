"""Checked title scalars without mirror diagnostics, previews or chunk counts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.ch import sequential_reads, set_query_tag
from server.core.diagnostics import private_work
from server.knowledge.api.models_knowledge import KnowledgeListInput
from server.knowledge.app import source_listing
from server.knowledge.app.source_access import Admitted, ReadPlan
from server.knowledge.app.source_list_proofs import ListProofs
from server.knowledge.store import source_documents as store


@dataclass(frozen=True)
class Title:
    source_uri: str
    title: str
    format: str
    source_scheme: str
    source_revision: str
    version: int
    revision: int
    ingested_at: datetime
    proof: ListProofs


def _title(
    row: dict[str, Any],
    plan: ReadPlan,
    item: Admitted,
    namespace: str,
    source_id: str | None,
) -> Title:
    if item.namespace != namespace or (
        row["org"],
        row["namespace"],
        row["source_uri"],
        row["version"],
        row["revision"],
        row["ingested_at"],
        row["superseded_at"],
    ) != (
        plan.org,
        item.candidate["namespace"],
        item.candidate["source_uri"],
        item.document["version"],
        item.document["revision"],
        item.document["ingested_at"],
        None,
    ):
        raise ValueError("unexpected title projection identity")
    if not all(
        isinstance(row[key], str)
        for key in ("title", "doc_format", "source_scheme", "source_revision")
    ):
        raise ValueError("invalid title projection scalars")
    return Title(
        source_uri=item.candidate["source_uri"].rsplit("#", 1)[0],
        title=row["title"],
        format=row["doc_format"],
        source_scheme=row["source_scheme"],
        source_revision=row["source_revision"],
        version=int(row["version"]),
        revision=int(row["revision"]),
        ingested_at=row["ingested_at"],
        proof=ListProofs(plan, False, (item,), source_id=source_id),
    )


async def collect(
    plan: ReadPlan, namespace: str, *, limit: int, source_id: str | None = None
) -> list[Title]:
    """Return a capped title sample after the last permission and identity renewal."""
    if limit <= 0:
        return []
    set_query_tag("knowledge.mirror.titles")
    input = KnowledgeListInput(org=plan.org, namespace=namespace, limit=limit)
    with private_work(), sequential_reads():
        items = (
            await source_listing.select(
                plan, input, None, namespaces=[namespace], source_id=source_id
            )
        )[:limit]
        if not items or plan.failed or plan.unread:
            return []
        try:
            titles = []
            for item in items:
                current = await source_listing.renew(plan, [item])
                if current is None:
                    return []
                item = current[0]
                row = await store.title_projection(
                    plan.org,
                    item.candidate["namespace"],
                    item.candidate["source_uri"],
                    int(item.document["version"]),
                    int(item.document["revision"]),
                )
                if row is None:
                    return []
                titles.append(_title(row, plan, item, namespace, source_id))
            if await source_listing.finish(plan, input, items, source_id=source_id) is None:
                return []
            return titles
        except Exception:  # noqa: BLE001 — a conditional title failure discloses no partial leg
            plan.failed = True
            return []
