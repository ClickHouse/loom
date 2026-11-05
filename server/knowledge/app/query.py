"""`knowledge.query`: a typed plan over the live snapshots of ingested
datasets, run as one read-only statement and returned with its receipt.

Each dataset is read at the snapshot its source's live document serves, which
the receipt names. A dataset whose schema needs a person's answer is refused
until a writer approves it, and a result over a dataset the extraction left
incomplete, or that rejected rows, says so. The statement runs under the
caller-shaped ceilings, as the read-only reader user: a plan that needs more
rows, time or memory fails rather than returning part of an aggregate. Before
the rows are released, each source must still serve the snapshot read, so a
snapshot replaced or erased while the statement ran is never returned.
"""

from __future__ import annotations

import json
from typing import Any

from server.core.ch import caller_shaped, own_write_reads
from server.core.errors import NotFoundError, PreconditionFailedError, ValidationError
from server.knowledge.api.models_query import (
    KnowledgeQueryInput,
    KnowledgeQueryOutput,
    QueryReceipt,
    QueryResultColumn,
    QuerySnapshot,
)
from server.knowledge.app.datasets import json_value
from server.knowledge.domain import query as plan_domain
from server.knowledge.store import dataset_query
from server.knowledge.store import datasets as datasets_repo
from server.knowledge.store import documents as documents_repo

_NARROW = "a filter, fewer datasets, a join on a key that does not repeat"


async def _live_versions(org: str, namespace: str, uris: list[str]) -> dict[str, Any]:
    """Each source's live document row, None for one with none."""
    return {uri: await documents_repo.get_one(org, namespace, uri) for uri in uris}


async def _catalogs(input: KnowledgeQueryInput, docs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each alias's catalog row at its source's live document version."""
    out: dict[str, dict[str, Any]] = {}
    for d in input.datasets:
        doc = docs[d.source_uri]
        rows = (
            await datasets_repo.at_version(
                input.org, input.namespace, d.source_uri, doc.version, dataset_id=d.dataset_id
            )
            if doc is not None
            else []
        )
        if not rows:
            raise NotFoundError(f"no live dataset {d.dataset_id!r} of {d.source_uri!r}")
        catalog = rows[0]
        if catalog["schema_state"] == "needs_clarification":
            asks = "; ".join(
                f"{c['code']}{' ' + c['column'] if c['column'] else ''}"
                for c in json.loads(catalog["clarifications"])[:5]
            )
            raise ValidationError(
                f"dataset {d.alias!r} needs its schema answered before it is calculated over"
                f" ({asks}): approve it with knowledge.dataset_approve"
            )
        out[d.alias] = catalog
    return out


async def _still_served(
    input: KnowledgeQueryInput, docs: dict[str, Any], catalogs: dict[str, dict[str, Any]]
) -> None:
    """Refuses unless every source still serves the version read and every
    dataset the snapshot read: a publish moves the document's version, a
    retyping approval the dataset's snapshot under the same version, and either
    deletes the rows read without waiting."""
    after = await _live_versions(input.org, input.namespace, sorted(docs))
    for d in input.datasets:
        now, then = after[d.source_uri], docs[d.source_uri]
        served = (
            await datasets_repo.at_version(
                input.org, input.namespace, d.source_uri, now.version, dataset_id=d.dataset_id
            )
            if now is not None and now.version == then.version
            else []
        )
        if not served or served[0]["snapshot_id"] != catalogs[d.alias]["snapshot_id"]:
            raise PreconditionFailedError(
                f"dataset {d.alias!r} changed while the query ran: the snapshot read no longer"
                " serves; run it again"
            )


def _snapshot(alias: str, c: dict[str, Any]) -> QuerySnapshot:
    return QuerySnapshot(
        alias=alias,
        source_uri=str(c["source_uri"]),
        dataset_id=str(c["dataset_id"]),
        revision=int(c["revision"]),
        snapshot_id=str(c["snapshot_id"]),
        row_count=int(c["row_count"]),
        rejected_rows=int(c["rejected_rows"]),
        complete=bool(c["complete"]),
        schema_state=str(c["schema_state"]),
    )


def _incomplete(s: QuerySnapshot) -> str:
    if not s.complete:
        return f"{s.alias}: the extraction left part of the table out (knowledge.dataset_get)"
    if s.rejected_rows:
        return f"{s.alias}: {s.rejected_rows} of the source's rows were rejected, so not counted"
    return ""


async def handle_knowledge_query(input: KnowledgeQueryInput) -> KnowledgeQueryOutput:
    uris = sorted({d.source_uri for d in input.datasets})
    with own_write_reads():  # a dataset an ingest or approval just published is read
        docs = await _live_versions(input.org, input.namespace, uris)
        catalogs = await _catalogs(input, docs)
    plan = input.model_dump(exclude={"org", "namespace"})
    try:
        resolved = plan_domain.resolve(plan, catalogs)
    except ValueError as e:
        raise ValidationError(str(e)) from None
    sql, params = dataset_query.compile_plan(resolved, input.org, input.namespace)
    with caller_shaped(narrow=_NARROW):
        rows = await dataset_query.run(sql, params)
    with own_write_reads():  # released only while every snapshot read still serves
        await _still_served(input, docs, catalogs)

    outputs = resolved.outputs
    truncated = len(rows) > input.limit
    rows = rows[: input.limit]
    snapshots = [_snapshot(d.alias, catalogs[d.alias]) for d in input.datasets]
    reasons = [r for r in (_incomplete(s) for s in snapshots) if r]
    return KnowledgeQueryOutput(
        columns=[QueryResultColumn(name=o.name, type=o.type) for o in outputs],
        rows=[[json_value(r[f"c{i}"]) for i in range(len(outputs))] for r in rows],
        row_ids=(
            [[int(r[f"r{i}"]) for i in range(len(snapshots))] for r in rows]
            if resolved.selected
            else []
        ),
        truncated=truncated,
        complete=not reasons,
        incomplete=reasons,
        receipt=QueryReceipt(
            snapshots=snapshots,
            plan=plan,
            sql=sql,
            params={
                k: json.dumps(v, default=str) if isinstance(v, list) else str(v)
                for k, v in params.items()
            },
        ),
    )
