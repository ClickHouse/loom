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
from decimal import Decimal
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


async def _catalogs(
    input: KnowledgeQueryInput, versions: dict[str, int]
) -> dict[str, dict[str, Any]]:
    """Each alias's catalog row at its source's live document version."""
    out: dict[str, dict[str, Any]] = {}
    for d in input.datasets:
        missing = NotFoundError(f"no live dataset {d.dataset_id!r} of {d.source_uri!r}")
        version = versions.get(d.source_uri)
        if version is None:
            raise missing
        rows = await datasets_repo.at_version(
            input.org, input.namespace, d.source_uri, version, dataset_id=d.dataset_id
        )
        if not rows:
            # A publish of the source writes its document's new version, then deletes
            # the catalog rows of the one read: that is a change to retry, not a 404.
            now = await documents_repo.live_versions(input.org, input.namespace, [d.source_uri])
            if now.get(d.source_uri) not in (None, version):
                raise PreconditionFailedError(
                    f"{d.source_uri!r} was published again as the query read it; run it again"
                )
            raise missing
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


async def _still_served(input: KnowledgeQueryInput, catalogs: dict[str, dict[str, Any]]) -> None:
    """Refuses unless every dataset is still served by the snapshot read: a
    publish or a retyping approval serves another and deletes the rows read
    without waiting, an erasure serves none. A metadata edit moves the
    document's version but keeps the snapshot, so it is not a change."""
    uris = sorted({d.source_uri for d in input.datasets})
    after = await documents_repo.live_versions(input.org, input.namespace, uris)
    for d in input.datasets:
        now = after.get(d.source_uri)
        served = (
            await datasets_repo.at_version(
                input.org, input.namespace, d.source_uri, now, dataset_id=d.dataset_id
            )
            if now is not None
            else []
        )
        if not served or served[0]["snapshot_id"] != catalogs[d.alias]["snapshot_id"]:
            raise PreconditionFailedError(
                f"dataset {d.alias!r} changed while the query ran: the snapshot read no longer"
                " serves; run it again"
            )


def _param_text(v: Any) -> str:
    """A parameter as the receipt shows it: a decimal in plain digits (not
    `0E-9`), a list as a JSON array of such text."""
    if isinstance(v, list):
        return json.dumps([_param_text(x) for x in v])
    return format(v, "f") if isinstance(v, Decimal) else str(v)


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
        versions = await documents_repo.live_versions(input.org, input.namespace, uris)
        catalogs = await _catalogs(input, versions)
    plan = input.model_dump(exclude={"org", "namespace"})
    try:
        resolved = plan_domain.resolve(plan, catalogs)
    except ValueError as e:
        raise ValidationError(str(e)) from None
    sql, params = dataset_query.compile_plan(resolved, input.org, input.namespace)
    # The rows a publish just wrote, read where the catalog row naming them was.
    with own_write_reads(), caller_shaped(narrow=_NARROW):
        rows = await dataset_query.run(sql, params)
    with own_write_reads():  # released only while every snapshot read still serves
        await _still_served(input, catalogs)

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
            params={k: _param_text(v) for k, v in params.items()},
        ),
    )
