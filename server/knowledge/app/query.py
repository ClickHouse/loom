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

from server.core.ch import caller_shaped
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
    docs = await _live_versions(input.org, input.namespace, uris)
    catalogs = await _catalogs(input, docs)
    plan = input.model_dump(exclude={"org", "namespace"})
    try:
        resolved = plan_domain.resolve(plan, catalogs)
    except ValueError as e:
        raise ValidationError(str(e)) from None
    sql, params = dataset_query.compile_plan(resolved, input.org, input.namespace)
    with caller_shaped():
        rows = await dataset_query.run(sql, params)
    # Released only while every source still serves the snapshot read.
    after = await _live_versions(input.org, input.namespace, uris)
    for uri in uris:
        now, then = after[uri], docs[uri]
        if now is None or now.version != then.version:
            raise PreconditionFailedError(
                f"{uri!r} changed while the query ran: its snapshot no longer serves; run it again"
            )

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
