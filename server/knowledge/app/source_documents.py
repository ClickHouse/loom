"""Checked exact-document reads, kept apart from ordinary mining helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.core import pg, request_work
from server.core.ch import sequential_reads, set_query_tag
from server.core.datetimes import to_naive_utc_second
from server.core.diagnostics import private_work
from server.knowledge.app import source_search
from server.knowledge.app.source_access import Admitted, ReadPlan
from server.knowledge.app.source_search import Hydrated, _chunk_key
from server.knowledge.store import source_access as evidence
from server.knowledge.store import source_documents as store
from server.knowledge.store.documents import DocumentRow


@dataclass(frozen=True)
class CheckedDocument:
    admitted: Admitted
    row: DocumentRow


async def _discover(plan: ReadPlan, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out = []
    set_query_tag("knowledge.mirror.document_discovery")
    for start in range(0, len(pairs), 64):
        batch = pairs[start : start + 64]
        try:
            rows = (
                [head.row for head in (await evidence.heads(plan.org, batch)).values()]
                if pg.enabled()
                else await evidence.metadata(plan.org, batch)
            )
            wanted = set(batch)
            for row in rows:
                key = (row["namespace"], row["source_uri"])
                if key not in wanted:
                    raise ValueError("unexpected document discovery row")
                wanted.remove(key)
                out.append({"namespace": key[0], "source_uri": key[1]})
        except Exception:  # noqa: BLE001 — every selected discovery read must fail closed uniformly
            plan.failed = plan.unread = True
            request_work.mark_mirrored(unread=True)
    return out


def _same(row: DocumentRow, plan: ReadPlan, item: Admitted) -> bool:
    return (row.org, row.namespace, row.source_uri) == (
        plan.org,
        item.candidate["namespace"],
        item.candidate["source_uri"],
    ) and (row.version, row.revision) == (
        int(item.document["version"]),
        int(item.document["revision"]),
    )


async def refresh(plan: ReadPlan, document: CheckedDocument) -> CheckedDocument | None:
    """Renew an open copy's permission proof without fetching its text again."""
    if plan.failed or plan.unread:
        return None
    try:
        admitted = await plan.check([document.admitted.candidate])
        if plan.failed or plan.unread or len(admitted) != 1:
            return None
        item = admitted[0]
        if _same(document.row, plan, item) and plan.current(item):
            return CheckedDocument(item, document.row)
    except Exception:  # noqa: BLE001 — conditional revalidation cannot authorize an open copy
        plan.failed = True
    return None


async def get(plan: ReadPlan, namespace: str, source_uri: str) -> CheckedDocument | None:
    """Admit metadata before text, then retain only an unchanged, freshly admitted copy."""
    pairs = sorted(
        (mirror, source_uri + "#" + source)
        for (mirror, source), original in plan.discovery_sources.items()
        if original == namespace
    )
    set_query_tag("knowledge.mirror.document")
    with private_work(), sequential_reads():
        candidates = await _discover(plan, pairs)
        if plan.failed or plan.unread:
            return None
        try:
            admitted = []
            for start in range(0, len(candidates), 64):
                admitted.extend(await plan.check(candidates[start : start + 64]))
            if plan.failed or plan.unread:
                return None
            for item in sorted(
                admitted,
                key=lambda i: (
                    to_naive_utc_second(i.document["ingested_at"]),
                    i.candidate["namespace"],
                    i.candidate["source_uri"],
                ),
                reverse=True,
            ):
                row = await store.contents(
                    plan.org,
                    item.candidate["namespace"],
                    item.candidate["source_uri"],
                    int(item.document["version"]),
                    int(item.document["revision"]),
                )
                if row is None:
                    continue
                if not _same(row, plan, item):
                    raise ValueError("unexpected document content identity")
                refreshed = await refresh(plan, CheckedDocument(item, row))
                if plan.failed or plan.unread:
                    return None
                if refreshed is not None:
                    return refreshed
        except Exception:  # noqa: BLE001 — conditional content or permission failure invalidates all copies
            plan.failed = True
    return None


async def _chunk_batch(plan: ReadPlan, batch: list[dict[str, Any]], model: str) -> list[Hydrated]:
    admitted = await plan.check(batch, chunks=True)
    hydrated = await source_search.hydrate(
        plan, {"semantic": admitted}, model=model, embedded_only=False
    )
    return hydrated.get("semantic", [])


async def chunks(plan: ReadPlan, document: CheckedDocument, *, model: str) -> list[Hydrated] | None:
    """Read an admitted copy's chunks; None distinguishes failure from an empty document."""
    if plan.failed or plan.unread:
        return None
    set_query_tag("knowledge.mirror.document_chunks")
    try:
        with private_work(), sequential_reads():
            row = document.row
            retained: dict[int, Hydrated] = {}
            async with store.chunk_candidates(
                plan.org, row.namespace, row.source_uri, model
            ) as stream:
                batch = []
                async for candidate in stream:
                    if (candidate["namespace"], candidate["source_uri"]) != (
                        row.namespace,
                        row.source_uri,
                    ):
                        raise ValueError("unexpected document chunk source")
                    batch.append(candidate)
                    if len(batch) == 64:
                        for item in await _chunk_batch(plan, batch, model):
                            retained.setdefault(int(item.admitted.candidate["chunk_idx"]), item)
                        batch = []
                if batch:
                    for item in await _chunk_batch(plan, batch, model):
                        retained.setdefault(int(item.admitted.candidate["chunk_idx"]), item)
            refreshed = await source_search.recheck(
                plan, {"semantic": [item.admitted for item in retained.values()]}
            )
            proof = {_chunk_key(item.candidate): item for item in refreshed.get("semantic", [])}
            if await refresh(plan, document) is None:
                return None
            return [
                Hydrated(proof[_chunk_key(item.admitted.candidate)], item.row)
                for _idx, item in sorted(retained.items())
                if _chunk_key(item.admitted.candidate) in proof
                and plan.current(proof[_chunk_key(item.admitted.candidate)])
            ]
    except Exception:  # noqa: BLE001 — conditional chunk failure invalidates the entire document
        plan.failed = True
        return None
