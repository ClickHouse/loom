"""Typed datasets from an ingested file's tables: their publication by the
ingest job, knowledge.dataset_get and knowledge.dataset_approve.

The ingest job publishes a dataset per table of the artifact before the
document row that serves them: its rows in batches under a new snapshot, then
its catalog row, with the document version the job's row takes. Once the row
lands, the source's older snapshots are deleted. A revision of a source whose
live dataset was approved keeps the approved types when its columns are the
same.

An approval that retypes a column types the rows again from the stored
artifact, under a new snapshot written before the source's claim is taken;
under the claim it checks that the document and the snapshot are still the
ones reviewed, then writes the catalog row that switches to it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.errors import (
    BackendUnavailableError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)
from server.core.leases import LeaseLost
from server.knowledge.api.models_knowledge import (
    DatasetClarificationOut,
    DatasetColumnOut,
    DatasetOut,
    DatasetRejectedOut,
    KnowledgeDatasetApproveInput,
    KnowledgeDatasetApproveOutput,
    KnowledgeDatasetGetInput,
    KnowledgeDatasetGetOutput,
)
from server.knowledge.domain import datasets as ds
from server.knowledge.domain.extract.artifact import Artifact, Table
from server.knowledge.store import datasets as datasets_repo

log = logging.getLogger(__name__)

# Rows typed and inserted per statement: a batch of the widest table is a few
# MB of values.
_BATCH = 5_000  # not a setting: how a snapshot is written
_JS_SAFE = 2**53  # past it, an integer goes out as text


async def _write(
    base: dict[str, Any],
    table: Table,
    schema: ds.Schema,
    lease: Any = None,
    alive: Callable[[], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Insert `table`'s rows typed by `schema` under `base`'s snapshot, a batch
    at a time (typed off the event loop); returns the catalog row naming them,
    not yet written. Before each batch, a lost `lease` stops it, and `alive`
    raises when the job is gone (an erasure deleted it)."""
    stored, rejected = 0, 0
    sample: list[ds.Rejected] = []
    now = naive_utc_now()
    for start in range(0, len(table.rows), _BATCH):
        if lease is not None and not lease.held:
            raise LeaseLost(f"dataset:{base['dataset_id']}")
        if alive is not None:
            await alive()
        typed = await asyncio.to_thread(ds.typed_rows, table, schema.columns, start, start + _BATCH)
        await datasets_repo.insert_rows([datasets_repo.slot_row(base, r, now) for r in typed.rows])
        stored += len(typed.rows)
        rejected += typed.rejected
        sample.extend(typed.sample[: ds.MAX_REJECTED_SAMPLE - len(sample)])
    return datasets_repo.dataset_row(
        base,
        schema=json.dumps([c.to_dict() for c in schema.columns]),
        clarifications=json.dumps([c.to_dict() for c in schema.clarifications]),
        row_count=stored,
        rejected_rows=rejected,
        rejected=json.dumps([r.to_dict() for r in sample]),
    )


def _state(schema: ds.Schema) -> str:
    return "needs_clarification" if schema.clarifications else "inferred"


@dataclass(frozen=True)
class Published:
    """What a publish read of the live revision's datasets: the version of each
    catalog row, so the publish can tell, under the source's claim, that no
    approval landed since (`check_unchanged`)."""

    prior_version: int = 0
    seen: dict[str, int] = field(default_factory=dict)


async def publish(
    job: dict[str, Any],
    artifact: Artifact,
    *,
    doc_version: int,
    revision: int,
    artifact_sha256: str,
    prior_version: int,
    lease: Any,
    alive: Callable[[], Awaitable[Any]] | None = None,
) -> Published:
    """Write a dataset per table of `artifact` for document version
    `doc_version`. `prior_version` is the version of the source's live
    document row (0 for none), whose approved datasets carry. Each catalog row
    is written first, empty, so a run that stops midway can find the rows it
    wrote (`datasets_repo.forget`); nothing serves it before the document row.
    `alive` is awaited before each statement: it raises once the job is gone."""
    if not artifact.tables:
        return Published()
    org, ns, uri = job["org"], job["namespace"], job["source_uri"]
    prior_rows = (
        await datasets_repo.at_version(org, ns, uri, prior_version) if prior_version else []
    )
    published = Published(
        prior_version, {str(r["dataset_id"]): int(r["version"]) for r in prior_rows}
    )
    approved = {str(r["dataset_id"]): r for r in prior_rows if r["schema_state"] == "approved"}
    for position, (table, dataset_id) in enumerate(
        zip(artifact.tables, ds.dataset_ids(uri, artifact.tables), strict=True)
    ):
        prior = approved.get(dataset_id)
        carried = (
            ds.carried(table, ds.columns_from_json(json.loads(prior["schema"])))
            if prior is not None
            else None
        )
        schema = await asyncio.to_thread(ds.infer, table, carried)  # a table may be wide
        approval = prior if carried is not None else None  # an approval this revision keeps
        base = datasets_repo.dataset_row(
            None,
            org=org,
            namespace=ns,
            source_uri=uri,
            dataset_id=dataset_id,
            doc_version=doc_version,
            snapshot_id=time.time_ns(),
            revision=revision,
            table_id=table.id,
            position=position,
            name=table.name,
            sheet=table.locator.sheet,
            row_start=table.locator.row_start or 0,
            row_end=table.locator.row_end or 0,
            schema="[]",
            schema_state="approved" if carried is not None else _state(schema),
            clarifications="[]",
            row_count=0,
            rejected_rows=0,
            rejected="[]",
            complete=ds.table_complete(artifact, table),
            artifact_sha256=artifact_sha256,
            approved_by=str(approval["approved_by"]) if approval else "",
            approved_at=approval["approved_at"] if approval else None,
            **{c: str(job[c]) for c in datasets_repo.AUTHOR},
        )
        if alive is not None:
            await alive()
        await datasets_repo.insert_catalog([base])
        new = await _write(base, table, schema, lease, alive)
        if alive is not None:
            await alive()
        await datasets_repo.insert_catalog([new])
    return published


class ApprovalLanded(Exception):
    """An approval of the live revision's datasets landed after the publish read
    them: the job runs again, without counting the attempt, and carries it."""


async def check_unchanged(job: dict[str, Any], published: Published) -> None:
    """Under the source's claim: ApprovalLanded when an approval of the live
    revision's datasets landed after the publish read them."""
    if not published.prior_version:
        return
    rows = await datasets_repo.at_version(
        job["org"], job["namespace"], job["source_uri"], published.prior_version
    )
    if {str(r["dataset_id"]): int(r["version"]) for r in rows} != published.seen:
        raise ApprovalLanded("a dataset of the source was approved while it was published")


async def carry(
    org: str, namespace: str, source_uri: str, from_version: int, to_version: int
) -> None:
    """The datasets served at document version `from_version`, served at
    `to_version` too: a catalog row each, the same snapshot. A metadata edit
    writes them before its document row, under the source's claim."""
    rows = await datasets_repo.at_version(org, namespace, source_uri, from_version)
    if rows:
        await datasets_repo.insert_catalog(
            [datasets_repo.dataset_row(r, doc_version=to_version) for r in rows]
        )


def json_value(v: Any) -> Any:
    """A stored value as JSON carries it: a number a JSON reader would round
    (an integer past 2^53, a fraction a double cannot hold) as text."""
    if isinstance(v, Decimal):
        if v == v.to_integral_value():
            n = int(v)
            return n if abs(n) < _JS_SAFE else str(n)
        f = float(v)
        if Decimal(repr(f)) == v:
            return f
        # Its exact digits, trailing zeros dropped: normalize() would round to 28.
        return format(v, "f").rstrip("0")  # a fraction: a digit is left past the point
    return v


def _out(row: dict[str, Any], sample: list[dict[str, Any]] | None = None) -> DatasetOut:
    columns = ds.columns_from_json(json.loads(row["schema"]))
    return DatasetOut(
        dataset_id=str(row["dataset_id"]),
        source_uri=str(row["source_uri"]),
        name=str(row["name"]),
        sheet=str(row["sheet"]),
        row_start=int(row["row_start"]),
        row_end=int(row["row_end"]),
        revision=int(row["revision"]),
        snapshot_id=str(row["snapshot_id"]),
        schema_state=str(row["schema_state"]),
        columns=[
            DatasetColumnOut(name=c.name, type=c.type, inferred=c.inferred, stored=bool(c.slot))
            for c in columns
        ],
        clarifications=[DatasetClarificationOut(**c) for c in json.loads(row["clarifications"])],
        row_count=int(row["row_count"]),
        rejected_rows=int(row["rejected_rows"]),
        rejected=[DatasetRejectedOut(**r) for r in json.loads(row["rejected"])],
        complete=bool(row["complete"]),
        approved_by=str(row["approved_by"]),
        approved_at=row["approved_at"],
        created_at=row["created_at"],
        rows=[{c.name: json_value(r[c.slot]) for c in columns if c.slot} for r in sample or []],
    )


async def _samples(rows: list[dict[str, Any]], n: int) -> dict[str, list[dict[str, Any]]]:
    """Each dataset's first `n` rows, by dataset id, in one read."""
    if not n or not rows:
        return {}
    slots = {c.slot for r in rows for c in ds.columns_from_json(json.loads(r["schema"])) if c.slot}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await datasets_repo.sample(rows, sorted(slots), n):  # in key order
        out.setdefault(str(r["dataset_id"]), []).append(r)
    return out


async def handle_knowledge_dataset_get(
    input: KnowledgeDatasetGetInput,
) -> KnowledgeDatasetGetOutput:
    from server.core.ch import own_write_reads
    from server.knowledge.store import documents as documents_repo

    with own_write_reads():
        doc = await documents_repo.get_one(input.org, input.namespace, input.source_uri)
        if doc is None:
            return KnowledgeDatasetGetOutput(found=False)
        rows = await datasets_repo.at_version(
            input.org, input.namespace, input.source_uri, doc.version, dataset_id=input.dataset_id
        )
        samples = await _samples(rows, input.sample_rows)
    out = [_out(r, samples.get(str(r["dataset_id"]))) for r in rows]
    return KnowledgeDatasetGetOutput(found=True, revision=int(doc.revision), datasets=out)


async def _table(row: dict[str, Any], fmt: str) -> Table:
    """The table `row` was published from, out of the stored artifact."""
    from server.knowledge.domain.extract.validate import parse_artifact
    from server.knowledge.store import blobs

    org, ns, sha = row["org"], row["namespace"], str(row["artifact_sha256"])
    stored = await blobs.locate(org, ns, sha) if sha else None
    data = await blobs.read(org, ns, sha, stored=stored) if stored is not None else None
    if data is None:
        raise NotFoundError("the dataset's stored extraction is gone; ingest the file again")
    # The server wrote it (ingest_run._parse) after checking it under the bound.
    artifact = await asyncio.to_thread(parse_artifact, data, fmt=fmt, max_bytes=len(data))
    table = next((t for t in artifact.tables if t.id == row["table_id"]), None)
    if table is None:
        raise NotFoundError("the dataset's table is not in its stored extraction")
    return table


async def handle_knowledge_dataset_approve(
    input: KnowledgeDatasetApproveInput,
) -> KnowledgeDatasetApproveOutput:
    from server.core.ch import own_write_reads
    from server.knowledge.store import documents as documents_repo
    from server.memory.app.provenance import writer_actor
    from server.memory.domain.provenance import source_class_for

    org, ns, uri = input.org, input.namespace, input.source_uri
    with own_write_reads():
        doc = await documents_repo.get_one(org, ns, uri)
        found = (
            await datasets_repo.at_version(org, ns, uri, doc.version, dataset_id=input.dataset_id)
            if doc is not None
            else []
        )
        if doc is None or not found:
            return KnowledgeDatasetApproveOutput(found=False)
        row = found[0]
        if str(row["snapshot_id"]) != input.snapshot_id:
            raise PreconditionFailedError(
                f"snapshot {input.snapshot_id} no longer serves the dataset; read it again"
            )
        columns = ds.columns_from_json(json.loads(row["schema"]))
        unknown = sorted(set(input.columns) - {c.name for c in columns})
        if unknown:
            raise ValidationError(f"not a column of the dataset: {', '.join(unknown)[:200]}")
        types = {c.name: c.type for c in columns} | dict(input.columns)
        retyped = any(types[c.name] != c.type for c in columns)
        # The approver writes the catalog row; the rows stay the uploader's
        # (`user_id`), as erasure reads them.
        approval = {
            "schema_state": "approved",
            "approved_by": writer_actor(),
            "approved_at": naive_utc_now(),
            "actor": writer_actor(),
            "source_class": source_class_for(default="asserted", requested=None),
        }
        if not retyped:
            asks = [c for c in json.loads(row["clarifications"]) if c["code"] == "past_capacity"]
            new = datasets_repo.dataset_row(row, clarifications=json.dumps(asks), **approval)
            return await _switch(input, doc.version, row, new, retyped=False)
        # The rows copy their authorship from the base: the uploader's, as the live
        # document row the ingest wrote holds it (the catalog row may be an earlier
        # approver's write); `_switch` checks that row is still the live one.
        uploader = {c: str(getattr(doc, c)) for c in datasets_repo.AUTHOR}
        base = datasets_repo.dataset_row(row, snapshot_id=time.time_ns(), **uploader)
        try:
            table = await _table(row, str(doc.format))
            written = await _write(base, table, await asyncio.to_thread(ds.infer, table, types))
            new = datasets_repo.dataset_row(written, **approval)
        except BaseException:
            await _forget_quietly(base)  # the rows it wrote serve nothing
            raise
        return await _switch(input, doc.version, row, new, retyped=True)


async def _forget_quietly(catalog: dict[str, Any]) -> None:
    try:
        await datasets_repo.forget_snapshot(catalog)
    except Exception as e:  # noqa: BLE001 — the approval's own outcome must reach the caller
        log.warning(
            "knowledge.dataset_approve: snapshot %s left unserved (an erasure deletes it): %s",
            catalog["snapshot_id"],
            type(e).__name__,
        )


async def _switch(
    input: KnowledgeDatasetApproveInput,
    doc_version: int,
    row: dict[str, Any],
    new: dict[str, Any],
    *,
    retyped: bool,
) -> KnowledgeDatasetApproveOutput:
    """Under the source's claim: write `new` over `row` when the document and
    the snapshot are still the ones read; found=False when the source moved.
    A retype's rows go when its catalog row was never sent (refused, failed or
    cut off before it); once sent, the row may have landed though the insert
    failed, so they stay for the source's next ingest or delete."""
    from server.knowledge.app.ingest_run import claim_source
    from server.knowledge.store import documents as documents_repo

    org, ns, uri = input.org, input.namespace, input.source_uri
    sent = False
    try:
        async with claim_source(org, ns, uri) as held:
            if not held:
                raise BackendUnavailableError(
                    "another write of this source holds it", operation="knowledge.dataset_approve"
                )
            # Under the claim no publish moves the source, so these hold to the write.
            live = await documents_repo.get_one(org, ns, uri)
            again = await datasets_repo.at_version(
                org, ns, uri, doc_version, dataset_id=input.dataset_id
            )
            if live is None:
                return KnowledgeDatasetApproveOutput(found=False)
            if (
                live.version != doc_version
                or not again
                or (int(again[0]["snapshot_id"]) != int(row["snapshot_id"]))
            ):  # a later revision, or another approval: what was reviewed no longer serves
                raise PreconditionFailedError(
                    f"snapshot {input.snapshot_id} no longer serves the dataset; read it again"
                )
            # Stamped now, past the row it replaces: one written while this call
            # typed its rows or waited for the claim must not outrank it in FINAL.
            new = datasets_repo.dataset_row(new, version=int(again[0]["version"]))
            sent = True
            await datasets_repo.insert_catalog([new])
    finally:
        if retyped and not sent:
            await _forget_quietly(new)
    if retyped:
        await _forget_quietly(row)  # the snapshot it replaced
    return KnowledgeDatasetApproveOutput(found=True, dataset=_out(new), retyped=retyped)
