"""Checked list families, bounded selection and a final permission barrier."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.core import pg, request_work
from server.core.ch import sequential_reads, set_query_tag
from server.core.datetimes import to_naive_utc_second
from server.core.diagnostics import private_work
from server.knowledge.api.models_knowledge import KnowledgeListInput
from server.knowledge.app.source_access import Admitted, ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_list_proofs import ListProofs
from server.knowledge.domain.access_list import candidate_keys
from server.knowledge.domain.document_access import public_tags
from server.knowledge.store import source_documents as store

if TYPE_CHECKING:
    from server.knowledge.store.documents import DocumentListRow

type Position = tuple[datetime, str, str]
type Family = tuple[str, str]


@dataclass(frozen=True)
class Listing:
    rows: list[DocumentListRow]
    proofs: ListProofs


def _family(item: Admitted) -> Family:
    return item.namespace, item.candidate["source_uri"].rsplit("#", 1)[0]


def _position(item: Admitted) -> Position:
    return to_naive_utc_second(item.document["ingested_at"]), *_family(item)


def _same(before: Admitted, after: Admitted) -> bool:
    return (
        before.namespace == after.namespace
        and (before.candidate["namespace"], before.candidate["source_uri"])
        == (after.candidate["namespace"], after.candidate["source_uri"])
        and all(
            before.document[k] == after.document[k] for k in ("version", "revision", "ingested_at")
        )
        and before.serving == after.serving
    )


def _matches(item: Admitted, input: KnowledgeListInput, cursor: Position | None) -> bool:
    if cursor and _position(item) >= (to_naive_utc_second(cursor[0]), *cursor[1:]):
        return False
    uri = _family(item)[1]
    prefixes = [prefix for prefix in input.uri_prefix if prefix]
    if prefixes and not any(uri.startswith(prefix) for prefix in prefixes):
        return False
    asked = {tag.lower() for tag in input.tags if tag}
    tags = {tag.lower() for tag in public_tags(item.document["tags"])}
    return not asked or (asked <= tags if input.tags_every else bool(asked & tags))


@dataclass
class _Selection:
    plan: ReadPlan
    input: KnowledgeListInput
    cursor: Position | None
    sources: dict[tuple[str, str], str]
    source_id: str | None = None
    heap: list[tuple[Position, Admitted]] = field(default_factory=list)
    pending: list[Admitted] = field(default_factory=list)
    family: Family | None = None
    winner: Admitted | None = None
    previous: tuple[str, ...] = ()

    async def stage(self, item: Admitted | None) -> None:
        try:
            if item is not None and _matches(item, self.input, self.cursor):
                self.pending.append(item)
        except Exception:  # noqa: BLE001 — invalid admitted metadata is a conditional leg failure
            self.plan.failed = True
        if len(self.pending) == 64:
            await self.flush()

    async def flush(self) -> None:
        batch, self.pending = self.pending, []
        if not batch or self.plan.failed or self.plan.unread:
            return
        try:
            shadows = (
                await store.ordinary_families(
                    self.plan.org,
                    [_family(item) for item in batch],
                    include_superseded=self.input.include_superseded,
                )
                if self.source_id is None
                else set()
            )
            for item in batch:
                if _family(item) not in shadows:
                    heapq.heappush(self.heap, (_position(item), item))
                    if len(self.heap) > self.input.limit + 1:
                        heapq.heappop(self.heap)
        except Exception:  # noqa: BLE001 — conditional shadow failure invalidates the mirror leg
            self.plan.failed = True

    async def take(self, batch: list[dict[str, Any]]) -> None:
        admitted = {}
        if not self.plan.failed and not self.plan.unread:
            try:
                selected = await self.plan.check(batch)
                wanted = {(row["namespace"], row["source_uri"]) for row in batch}
                for proof in selected:
                    key = (proof.candidate["namespace"], proof.candidate["source_uri"])
                    if key not in wanted or key in admitted:
                        raise ValueError("unexpected list admission identity")
                    admitted[key] = proof
            except Exception:  # noqa: BLE001 — candidate checks cannot change unconditional discovery
                self.plan.failed = True
        for row in batch:
            namespace, uri = row["namespace"], row["source_uri"]
            base, _separator, source = uri.rpartition("#")
            family = (self.sources.get((namespace, source), ""), base)
            position = (*family, namespace, uri)
            if (
                not family[0]
                or not base
                or family != (row["public_namespace"], row["public_uri"])
                or position <= self.previous
            ):
                raise ValueError("invalid list discovery family order")
            self.previous = position
            if family != self.family:
                await self.stage(self.winner)
                self.family, self.winner = family, None
            item = admitted.get((namespace, uri))
            try:
                if item is not None:
                    if _family(item) != family:
                        self.plan.failed = True
                    elif self.winner is None or (_position(item), namespace, uri) > (
                        _position(self.winner),
                        self.winner.candidate["namespace"],
                        self.winner.candidate["source_uri"],
                    ):
                        self.winner = item
            except Exception:  # noqa: BLE001 — invalid admitted metadata cannot affect unread status
                self.plan.failed = True


async def select(
    plan: ReadPlan,
    input: KnowledgeListInput,
    cursor: Position | None,
    *,
    namespaces: list[str],
    source_id: str | None = None,
) -> list[Admitted]:
    """Select metadata families; a source inspection selects only its own copies."""
    set_query_tag("knowledge.mirror.select")
    sources = {
        key: original for key, original in plan.discovery_sources.items() if original in namespaces
    }
    if source_id is not None:
        sources = {key: value for key, value in sources.items() if key[1] == source_id}
    if not sources:
        return []
    selected = _Selection(plan, input, cursor, sources, source_id=source_id)
    reader = plan.discovery_reader
    coarse = (
        None
        if pg.enabled()
        else candidate_keys(
            subject=reader.subject if reader else "",
            emails=reader.emails if reader else frozenset(),
            domains=reader.domains if reader else frozenset(),
        )
    )
    try:
        async with store.list_candidates(plan.org, sources, coarse) as stream:
            batch = []
            async for row in stream:
                batch.append(row)
                if len(batch) == 64:
                    await selected.take(batch)
                    batch = []
            if batch:
                await selected.take(batch)
        await selected.stage(selected.winner)
        await selected.flush()
    except Exception:  # noqa: BLE001 — fixed metadata discovery failure discloses no partial list
        plan.failed = plan.unread = True
        request_work.mark_mirrored(unread=True)
    return (
        []
        if plan.failed or plan.unread
        else [item for _, item in sorted(selected.heap, reverse=True)]
    )


async def renew(plan: ReadPlan, items: list[Admitted]) -> list[Admitted] | None:
    set_query_tag("knowledge.mirror.renew")
    refreshed = []
    for start in range(0, len(items), 64):
        batch = items[start : start + 64]
        current = await plan.check([item.candidate for item in batch])
        by_key = {
            (item.candidate["namespace"], item.candidate["source_uri"]): item for item in current
        }
        if plan.failed or plan.unread or len(current) != len(batch) or len(by_key) != len(batch):
            return None
        for item in batch:
            now = by_key.get((item.candidate["namespace"], item.candidate["source_uri"]))
            if now is None or not _same(item, now):
                return None
            refreshed.append(now)
    return refreshed if all(plan.current(item) for item in refreshed) else None


async def finish(
    plan: ReadPlan,
    input: KnowledgeListInput,
    items: list[Admitted],
    *,
    source_id: str | None = None,
) -> list[Admitted] | None:
    """Refresh selected copies after all projection and catalog-shadow reads."""
    set_query_tag("knowledge.mirror.list_proof")
    if plan.failed or plan.unread:
        return None
    if source_id is None:
        for start in range(0, len(items), 64):
            if await store.ordinary_families(
                plan.org,
                [_family(item) for item in items[start : start + 64]],
                include_superseded=input.include_superseded,
            ):
                return None
    return await renew(plan, items)


async def _materialize(plan: ReadPlan, items: list[Admitted], input: KnowledgeListInput) -> Listing:
    out = []
    for item in items:
        fresh = await renew(plan, [item])
        if fresh is None:
            raise LostCorpus()
        item = fresh[0]
        namespace, uri = item.candidate["namespace"], item.candidate["source_uri"]
        version, revision = int(item.document["version"]), int(item.document["revision"])
        row = await store.list_projection(plan.org, namespace, uri, version, revision)
        if row is None or (
            row["org"],
            row["namespace"],
            row["source_uri"],
            row["version"],
            row["revision"],
            row["ingested_at"],
            row["superseded_at"],
        ) != (plan.org, namespace, uri, version, revision, item.document["ingested_at"], None):
            raise LostCorpus()
        row["chunk_count"] = await store.list_chunk_count(
            plan.org, namespace, uri, revision, item.serving
        )
        out.append(row)
    proofs = ListProofs(plan, input.include_superseded, tuple(items))
    await proofs.check()
    return Listing([_public(row, item) for row, item in zip(out, items, strict=True)], proofs)


def _public(row: dict[str, Any], item: Admitted) -> DocumentListRow:
    return {
        "namespace": item.namespace,
        "source_uri": _family(item)[1],
        "title": row["title"] or "",
        "description": row.get("description") or "",
        "tags": public_tags(row["tags"]),
        "ingested_at": row["ingested_at"],
        "superseded_at": None,
        "chunk_count": int(row["chunk_count"]),
        "blob_path": row.get("blob_path") or "",
        "blob_size": int(row.get("blob_size") or 0),
        "blob_content_type": row.get("blob_content_type") or "",
        "original_filename": row.get("original_filename") or "",
        "content_preview": row.get("content_preview") or "",
    }


async def collect(
    plan: ReadPlan, input: KnowledgeListInput, *, namespaces: list[str], cursor: Position | None
) -> Listing:
    """Return at most limit+1 complete admitted families, or an empty mirror leg."""
    set_query_tag("knowledge.mirror.list")
    with private_work(), sequential_reads():
        items = await select(plan, input, cursor, namespaces=namespaces)
        if not items or plan.failed or plan.unread:
            return Listing([], ListProofs(plan, input.include_superseded, ()))
        try:
            return await _materialize(plan, items, input)
        except LostCorpus:
            pass
        except Exception:  # noqa: BLE001 — selected fields, counts and final proofs fail closed together
            plan.failed = True
        return Listing([], ListProofs(plan, input.include_superseded, ()))
