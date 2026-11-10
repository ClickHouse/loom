"""loom.datasets and loom.dataset_rows: the typed snapshots of an ingested
file's tables.

A catalog row is a ReplacingMergeTree(version) row per (source, dataset,
document version), read with FINAL; every new version carries every column
forward (`dataset_row`). Its rows are written once per snapshot, before the
catalog row that names them, so a reader never finds a catalog row whose rows
are still to come. A dataset is served while the source's live document row
has the version it was published with (`serving`).

Snapshots nothing serves any more go by lightweight DELETE: a later publish
deletes the source's older ones, an approval the snapshot it replaced, a
knowledge.delete those of the versions it retired. A request does not wait for
them.

Under the Postgres authority the catalog's authority is loom.knowledge_datasets
(`pg_datasets`): a catalog row commits there first and is then inserted here, best
effort; a read takes Postgres's rows, and those only ClickHouse holds (a build that
predates that table wrote them), the newer version of a dataset winning.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from server.core import pg
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client
from server.core.rmt_carry import register
from server.knowledge.domain.datasets import SLOT_COLUMNS
from server.knowledge.store import pg_datasets

log = logging.getLogger(__name__)

DATASETS = "loom.datasets"
ROWS = "loom.dataset_rows"

DATASET_COLUMNS = (
    "org",
    "namespace",
    "source_uri",
    "dataset_id",
    "doc_version",
    "snapshot_id",
    "revision",
    "table_id",
    "position",
    "name",
    "sheet",
    "row_start",
    "row_end",
    "schema",
    "schema_state",
    "clarifications",
    "row_count",
    "rejected_rows",
    "rejected",
    "complete",
    "artifact_sha256",
    "approved_by",
    "approved_at",
    "agent",
    "user_id",
    "session_id",
    "actor",
    "source_class",
    "created_at",
    "updated_at",
    "version",
)

ROW_COLUMNS = (
    "org",
    "namespace",
    "dataset_id",
    "snapshot_id",
    "row_id",
    "source_uri",
    "doc_version",
    "agent",
    "user_id",
    "session_id",
    "actor",
    "source_class",
    *SLOT_COLUMNS,
    "created_at",
)

AUTHOR = ("agent", "user_id", "session_id", "actor", "source_class")

register(DATASETS, key=("org", "namespace", "source_uri", "dataset_id", "doc_version"),
         columns=DATASET_COLUMNS)  # fmt: skip

# Requests delete without waiting: nothing reads a snapshot once its catalog
# row stops serving it.
_NO_WAIT = {"lightweight_deletes_sync": 0}


def dataset_row(existing: dict[str, Any] | None = None, /, **fields: Any) -> dict[str, Any]:
    """The one way to build a loom.datasets row: every column of `existing`,
    then `fields`, with a fresh `version` and `updated_at`."""
    unknown = set(fields) - set(DATASET_COLUMNS)
    if unknown:
        raise ValueError(f"dataset_row: not a column: {sorted(unknown)}")
    now = naive_utc_now()
    row: dict[str, Any] = {"created_at": now, "approved_by": "", "approved_at": None}
    if existing is not None:
        row.update({c: existing[c] for c in DATASET_COLUMNS if c in existing})
    row.update(fields)
    row["updated_at"] = now
    row["version"] = max(time.time_ns(), int(row.get("version") or 0) + 1)
    missing = [c for c in DATASET_COLUMNS if c not in row]
    if missing:
        raise ValueError(f"dataset_row: missing {missing}")
    # To the millisecond, as the DateTime64(3) columns keep them: a row read from
    # Postgres (`pg_datasets`) compares equal to its ClickHouse copy.
    for column in pg_datasets.TIME_COLUMNS:
        at = row[column]
        if isinstance(at, datetime):
            row[column] = at.replace(microsecond=at.microsecond // 1000 * 1000)
    return {c: row[c] for c in DATASET_COLUMNS}


def slot_row(catalog: dict[str, Any], values: dict[str, Any], now: datetime) -> dict[str, Any]:
    """A loom.dataset_rows row of `catalog`'s snapshot: its slot `values` (and
    `row_id`), every other slot null."""
    row: dict[str, Any] = dict.fromkeys(SLOT_COLUMNS)
    row.update(values)
    for c in ("org", "namespace", "dataset_id", "snapshot_id", "source_uri", "doc_version"):
        row[c] = catalog[c]
    row.update({c: catalog[c] for c in AUTHOR})
    row["created_at"] = now
    return row


async def insert_catalog(
    rows: list[dict[str, Any]],
    *,
    job: tuple[str, str, str] | None = None,
    ch: _CHClient | None = None,
) -> None:
    """Write catalog rows: under the Postgres authority committed there, then projected.
    `job` is the ingest publishing them, when Postgres holds it (`pg_datasets.upsert`).
    A row Postgres kept out (its key holds a newer version) is not projected."""
    held: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for r in rows:
        (held if pg.enabled() and pg_datasets.storable(r) else rest).append(r)
    if held:
        written = await pg_datasets.upsert(held, job=job)
        if written:
            await project(written, ch=ch)
    if rest:  # under Postgres, a key it cannot store: ClickHouse alone, as without it
        ch = ch or _default_client()
        await ch.insert(DATASETS, rest, settings={"async_insert": 0})


async def project(rows: list[dict[str, Any]], *, ch: _CHClient | None = None) -> bool:
    """Committed catalog rows into loom.datasets, best effort: Postgres is the answer.
    Whether they landed."""
    ch = ch or _default_client()
    try:
        await ch.insert(DATASETS, rows, settings={"async_insert": 0})
    except Exception as e:  # noqa: BLE001 — the rows are committed; the next write repairs it
        log.warning(
            "loom.datasets: %d committed rows not projected: %s", len(rows), type(e).__name__
        )
        return False
    return True


async def insert_rows(rows: list[dict[str, Any]], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(ROWS, rows, settings={"async_insert": 0})


_SELECT = (
    "SELECT org, namespace, source_uri, dataset_id, doc_version, snapshot_id, revision,"
    " table_id, position, name, sheet, row_start, row_end, schema,"
    " toString(schema_state) AS schema_state,"
    " clarifications, row_count, rejected_rows, rejected, complete, artifact_sha256,"
    " approved_by, approved_at, agent, user_id, session_id, actor,"
    " toString(source_class) AS source_class, created_at, updated_at, version"
    f" FROM {DATASETS} FINAL"
)


async def at_version(
    org: str,
    namespace: str,
    source_uri: str,
    doc_version: int,
    *,
    dataset_id: str = "",
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The source's datasets published with document version `doc_version`
    (one of them with `dataset_id`), in the artifact's table order."""
    ch = ch or _default_client()
    if not pg.enabled():
        return await _at_version_ch(org, namespace, source_uri, doc_version, dataset_id, ch)
    held = await pg_datasets.at_version(
        org, namespace, source_uri, doc_version, dataset_id=dataset_id
    )
    # Not sequentially consistent: a row an older build wrote that this replica has not
    # caught up with yet is served on its next read, and an approval over it still
    # outranks it (its version is past the one read).
    projected = await _at_version_ch(org, namespace, source_uri, doc_version, dataset_id, ch)
    newest = {str(r["dataset_id"]): r for r in held}
    for r in projected:
        mine = newest.get(str(r["dataset_id"]))
        if mine is None or int(r["version"]) > int(mine["version"]):
            newest[str(r["dataset_id"])] = r
    return sorted(newest.values(), key=lambda r: (int(r["position"]), str(r["dataset_id"])))


async def _at_version_ch(
    org: str, namespace: str, source_uri: str, doc_version: int, dataset_id: str, ch: _CHClient
) -> list[dict[str, Any]]:
    one = " AND dataset_id = {ds:String}" if dataset_id else ""
    return await ch.query_all(
        f"{_SELECT} WHERE org = {{org:String}} AND namespace = {{ns:String}}"
        " AND source_uri = {uri:String} AND doc_version = {v:UInt64}"
        f"{one} ORDER BY position LIMIT 10000",
        params={"org": org, "ns": namespace, "uri": source_uri, "v": doc_version, "ds": dataset_id},
    )


# The datasets whose first rows one read returns: a range of the key each.
MAX_SAMPLED = 20  # not a setting: bounds dataset_get's one read


async def sample(
    catalogs: Sequence[dict[str, Any]],
    slots: Sequence[str],
    limit: int,
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The first `limit` rows of each of the first MAX_SAMPLED `catalogs`'
    snapshots (one namespace's), the `slots` named, in one read, each a range
    of the key (`_first_ids`), so the read takes only the granules that hold
    the range."""
    ch = ch or _default_client()
    picked_catalogs = list(catalogs)[:MAX_SAMPLED]
    if not picked_catalogs:
        return []
    wanted = [s for s in SLOT_COLUMNS if s in set(slots)]  # names from a schema, held to the slots
    picked = ", ".join(["dataset_id", "snapshot_id", "row_id", *wanted])
    params: dict[str, Any] = {
        "org": picked_catalogs[0]["org"],
        "ns": picked_catalogs[0]["namespace"],
        "n": limit,
    }
    ranges = []
    for i, c in enumerate(picked_catalogs):
        params[f"ds{i}"], params[f"snap{i}"] = c["dataset_id"], int(c["snapshot_id"])
        params[f"to{i}"] = _first_ids(c, limit)
        ranges.append(
            f"(dataset_id = {{ds{i}:String}} AND snapshot_id = {{snap{i}:UInt64}}"
            f" AND row_id < {{to{i}:UInt64}})"
        )
    return await ch.query_all(
        f"SELECT {picked} FROM {ROWS} WHERE org = {{org:String}} AND namespace = {{ns:String}}"
        f" AND ({' OR '.join(ranges)})"
        # At most `n` rows of each of the datasets named, so no plain LIMIT is needed.
        " ORDER BY dataset_id, snapshot_id, row_id LIMIT {n:UInt32} BY dataset_id",
        params=params,
    )


def _first_ids(catalog: dict[str, Any], limit: int) -> int:
    """The row ids under which the snapshot's first `limit` stored rows lie: a
    rejected row leaves a gap in them. The catalog row names the first
    MAX_REJECTED_SAMPLE rejected rows in row order, so those it does not name
    lie past the last it does: the bound the named gaps give is exact unless it
    reaches past them, and then each unnamed one may lie below it."""
    named = sorted(
        int(r["row"]) - 1 for r in json.loads(catalog["rejected"])
    )  # `row` counts from 1
    bound = limit
    for row_id in named:
        if row_id < bound:
            bound += 1  # a gap below the bound: one more id to take
    unnamed = int(catalog["rejected_rows"]) - len(named)
    if unnamed and bound > named[-1] + 1:  # any rejected row is named first, so named is not empty
        bound += unnamed
    return bound


async def _ids(org: str, namespace: str, source_uris: Sequence[str], ch: _CHClient) -> list[str]:
    rows = await ch.query_all(
        f"SELECT DISTINCT dataset_id FROM {DATASETS} WHERE org = {{org:String}}"
        " AND namespace = {ns:String} AND source_uri IN {uris:Array(String)} LIMIT 100000",
        params={"org": org, "ns": namespace, "uris": list(source_uris)},
    )
    return [str(r["dataset_id"]) for r in rows]


async def forget(
    org: str,
    namespace: str,
    source_uris: Sequence[str],
    *,
    before: int = 0,
    only: int = 0,
    ch: _CHClient | None = None,
) -> None:
    """Delete the sources' snapshots of document versions older than `before`,
    or of version `only`: one of the two, never all of them. A source with no
    dataset costs one catalog read."""
    if bool(before) == bool(only):
        raise ValueError("forget: give before or only")
    # Under the Postgres authority its catalog first, so the datasets it named are deleted
    # even where no projection landed.
    named = (
        await pg_datasets.forget(org, namespace, source_uris, before=before, only=only)
        if pg.enabled()
        else set()
    )
    ch = ch or _default_client()
    ids = sorted(set(await _ids(org, namespace, source_uris, ch)) | named)
    if not ids:
        return
    params: dict[str, Any] = {"org": org, "ns": namespace, "ids": ids, "uris": list(source_uris)}
    which = " AND doc_version < {v:UInt64}" if before else " AND doc_version = {v:UInt64}"
    params["v"] = before or only
    scope = "org = {org:String} AND namespace = {ns:String}"
    await ch.execute(
        f"DELETE FROM {ROWS} WHERE {scope} AND dataset_id IN {{ids:Array(String)}}"
        f" AND source_uri IN {{uris:Array(String)}}{which}",
        params=params,
        settings=_NO_WAIT,
    )
    await ch.execute(
        f"DELETE FROM {DATASETS} WHERE {scope} AND source_uri IN {{uris:Array(String)}}{which}",
        params=params,
        settings=_NO_WAIT,
    )


async def forget_published(
    org: str, namespace: str, through: dict[str, int], *, ch: _CHClient | None = None
) -> None:
    """Delete each source's snapshots of its document versions up to
    `through[source_uri]`, the versions a delete retired: an ingest running
    meanwhile writes a later version, which this leaves to it. A row keeps the
    version it was written at while a metadata edit carries its snapshot to a
    later one (`app.datasets.carry`), so the rows of a snapshot a catalog row
    past the bound still serves stay with it. That holds for an edit whose
    carry landed before the catalog read here. One that lands between it and
    the delete (a delete takes no claim) may revive the document over this
    delete, when its version is the later one: its chunks are tombstoned and
    its datasets serve with their rows deleted, the race a claim-free delete
    has with an edit; a delete of the source settles both."""
    if not through:  # no source has a document row: no catalog read
        return
    # Under the Postgres authority its catalog rows go first, and the snapshots its later
    # rows serve stay with them.
    named, held = (
        await pg_datasets.forget_published(org, namespace, through)
        if pg.enabled()
        else (set(), set())
    )
    ch = ch or _default_client()
    uris = sorted(through)
    ids = sorted(set(await _ids(org, namespace, uris, ch)) | named)
    if not ids:
        return
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "ids": ids,
        "uris": uris,
        "through": [through[u] for u in uris],
    }
    scope = "org = {org:String} AND namespace = {ns:String} AND source_uri IN {uris:Array(String)}"
    upto = "doc_version <= {through:Array(UInt64)}[indexOf({uris:Array(String)}, source_uri)]"
    kept = await ch.query_all(
        f"SELECT DISTINCT snapshot_id FROM {DATASETS} WHERE {scope} AND NOT ({upto}) LIMIT 100000",
        params=params,
    )
    params["keep"] = sorted({int(r["snapshot_id"]) for r in kept} | set(held))
    await ch.execute(
        f"DELETE FROM {ROWS} WHERE {scope} AND dataset_id IN {{ids:Array(String)}} AND {upto}"
        " AND snapshot_id NOT IN {keep:Array(UInt64)}",
        params=params,
        settings=_NO_WAIT,
    )
    await ch.execute(
        f"DELETE FROM {DATASETS} WHERE {scope} AND {upto}", params=params, settings=_NO_WAIT
    )


async def forget_snapshot(catalog: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """Delete the rows of `catalog`'s snapshot: one an approval replaced, or
    wrote and then did not switch to."""
    ch = ch or _default_client()
    await ch.execute(
        f"DELETE FROM {ROWS} WHERE org = {{org:String}} AND namespace = {{ns:String}}"
        " AND dataset_id = {ds:String} AND snapshot_id = {snap:UInt64}",
        params={
            "org": catalog["org"],
            "ns": catalog["namespace"],
            "ds": catalog["dataset_id"],
            "snap": catalog["snapshot_id"],
        },
        settings=_NO_WAIT,
    )
