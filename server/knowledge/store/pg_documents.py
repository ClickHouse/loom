"""Knowledge document heads under the Postgres authority (`loom.knowledge_heads`) and the
loom.documents rows each commit still owes ClickHouse (`loom.knowledge_deliveries`).

A commit replaces a source's head only over the version its writer read: two writes of
one source cannot both publish, so revisions stay one per version. ClickHouse's
loom.documents is a projection published from the committed rows; the chunks a commit
names were inserted before it, and the retirement of the source's older chunk versions
is a delivery target of its own. docs/architecture/storage-authority.md, "Knowledge
documents", describes the tables and the order.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping, Sequence, Set
from datetime import datetime
from typing import Any, NamedTuple

from server.core import pg, pg_lease
from server.core.datetimes import to_naive_utc_second
from server.core.errors import NotFoundError, PreconditionFailedError, ValidationError
from server.knowledge.domain.owed_originals import NEEDS_ORIGINAL, OPEN_JOB, OWED_SHAS
from server.knowledge.domain.serving import Serving
from server.settings import settings

# Delivery targets (bits of `targets`).
DOCUMENT_ROW = 1  # the loom.documents row
RETIRE_OLDER = 2  # the source's chunk versions older than this one, superseded
COMMIT_TOKEN = 1  # the lease token a commit's own publication holds

# loom.documents' DateTime columns, kept as ISO text: what ClickHouse stores and reads.
_DATETIME_COLS = frozenset({"superseded_at", "ingested_at", "blob_erased_at"})
# The order every locker of heads takes them in.
_KEY_ORDER = "ORDER BY org, namespace, source_uri"


class HeadMoved(PreconditionFailedError):
    """409 — the source's head is no longer the version this write read: another write
    of the source committed first. The write starts over from a fresh read."""

    def __init__(self, source_uri: str) -> None:
        super().__init__(f"{source_uri!r} was written by another request after this one read it")
        self.source_uri = source_uri


class DocumentErased(NotFoundError):
    """404 — an erasure removed the document this write was built from after it was
    read: the write is not committed, and the document stays erased."""

    def __init__(self) -> None:
        super().__init__("the document was erased while this write ran")


class Delivery(NamedTuple):
    """A committed row still owed to ClickHouse, as a claim holds it."""

    org: str
    namespace: str
    source_uri: str
    version: int
    row: dict[str, Any]
    targets: int
    token: int
    attempts: int


# The columns the heads and deliveries also keep as Postgres text (the keys a lock, an
# erasure and the census read): one holding text Postgres cannot store is refused.
_KEY_COLUMNS = ("org", "namespace", "source_uri", "user_id")
# A value the jsonb row holds in place of text Postgres cannot store (a NUL character, a
# lone surrogate): the text's UTF-8 bytes, surrogates kept, in base64.
_ENCODED = "$loom_base64"


def refuse_unstorable_keys(row: Mapping[str, Any]) -> None:
    """ValidationError (422) when a key column of `row` holds text Postgres cannot store."""
    for col in _KEY_COLUMNS:
        if pg.unstorable(row.get(col)):
            raise ValidationError(
                f"{col} holds a NUL character or a lone surrogate, which Postgres cannot store"
            )


def encode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A loom.documents row as JSON values (the pool's jsonb codec encodes them), every
    value kept exactly: text Postgres cannot store goes in base64 (`_ENCODED`), so any row
    ClickHouse holds can be written again. A key column holding such text is refused."""
    refuse_unstorable_keys(row)
    out: dict[str, Any] = {}
    for col, val in row.items():
        # Whole seconds, as ClickHouse's DateTime keeps them: the head serves what a read
        # of the published row would.
        out[col] = (
            to_naive_utc_second(val).isoformat() if isinstance(val, datetime) else keep_text(val)
        )
    return out


def decode_row(stored: Mapping[str, Any]) -> dict[str, Any]:
    """The insertable loom.documents row back from its JSON."""
    out = {col: restore_text(val) for col, val in stored.items()}
    for col in _DATETIME_COLS:
        if isinstance(out.get(col), str):
            out[col] = datetime.fromisoformat(out[col])
    return out


def keep_text(val: Any) -> Any:
    """`val` as JSON Postgres can store: text it cannot goes in base64 (`_ENCODED`)."""
    if isinstance(val, str) and pg.unstorable(val):
        return {_ENCODED: base64.b64encode(val.encode("utf-8", "surrogatepass")).decode()}
    if isinstance(val, list):
        return [keep_text(v) for v in val]
    return val


def restore_text(val: Any) -> Any:
    """`keep_text`'s value back."""
    if isinstance(val, dict) and set(val) == {_ENCODED}:
        return base64.b64decode(val[_ENCODED]).decode("utf-8", "surrogatepass")
    if isinstance(val, list):
        return [restore_text(v) for v in val]
    return val


_LOCK_HEADS = (
    "SELECT source_uri, version FROM loom.knowledge_heads "
    "WHERE org = $1 AND namespace = $2 AND source_uri = ANY($3::text[]) "
    f"{_KEY_ORDER} FOR UPDATE"
)
# A row keeps the `doc_id` of the document its writer read (`build_document_row`), and a
# write carrying columns of a live one names it (`built_from`): one an erasure removed
# since, at any version, is refused. Evidence expires by age; the sweep only reclaims it.
_ERASED = (
    "SELECT EXISTS (SELECT 1 FROM loom.erased_documents WHERE org = $1 AND namespace = $2 "
    "AND doc_id = ANY($3::text[]) AND erased_at > now() - make_interval(hours => $4))"
)
# A source's first head: refused (no row back) when a concurrent commit inserted one, as a
# lock taken on a row that does not exist yet holds nothing.
_INSERT_HEAD = (
    "INSERT INTO loom.knowledge_heads "
    "(org, namespace, source_uri, version, revision, deleted, user_id, row, "
    "chunks_revision, chunks_version) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10) "
    "ON CONFLICT (org, namespace, source_uri) DO NOTHING RETURNING 1"
)
# A commit that inserted no chunks ($9 and $10 NULL) keeps the ones the head serves.
_UPDATE_HEAD = (
    "UPDATE loom.knowledge_heads SET version = $4, revision = $5, deleted = $6, "
    "user_id = $7, row = $8::jsonb, committed_at = now(), "
    "chunks_revision = COALESCE($9::integer, chunks_revision), "
    "chunks_version = COALESCE($10::bigint, chunks_version) "
    "WHERE org = $1 AND namespace = $2 AND source_uri = $3"
)
_INSERT_DELIVERY = (
    "INSERT INTO loom.knowledge_deliveries "
    "(org, namespace, source_uri, version, user_id, row, targets, lease_until) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, now() + make_interval(secs => $8))"
)


async def commit(
    rows: Sequence[Mapping[str, Any]],
    *,
    over: Mapping[str, int],
    targets: Mapping[str, int],
    built_from: Sequence[str] = (),
    chunked: Set[str] = frozenset(),
) -> float:
    """Commit `rows` (loom.documents rows of one namespace) as their sources' heads, each
    only over the version `over[source_uri]` its writer read (0: none), and record what
    each owes ClickHouse (`targets[source_uri]`, bits of DOCUMENT_ROW and RETIRE_OLDER) under
    COMMIT_TOKEN for LOOM_PG_REQUEST_LEASE_SECONDS. HeadMoved when a head moved. For the
    sources in `chunked`, whose write inserted chunks at the row's version, the head
    serves those chunks from now on (`serves`); another commit keeps the ones it serves.

    A source with no head yet (a document written before its namespace had heads, or by
    a build that writes none) takes its first head from this commit, whatever its writer
    read from ClickHouse; two first commits of one source race, and the second is
    refused. A head older than the version the writer read is replaced the same way:
    the writer read a newer row such a build wrote. Returns the `time.monotonic()` past
    which the commit's own publication must send nothing more (`pg_lease.leased`)."""
    if not rows:
        return 0.0
    org, namespace = str(rows[0]["org"]), str(rows[0]["namespace"])
    if any((r["org"], r["namespace"]) != (org, namespace) for r in rows):
        raise ValueError("one commit holds one namespace's rows")
    by_uri = {str(r["source_uri"]): r for r in rows}
    if len(by_uri) != len(rows):  # every row is published, so each must be its source's head
        raise ValueError("one commit holds one row per source")
    uris = sorted(by_uri)
    encoded = [(by_uri[u], encode_row(by_uri[u])) for u in uris]  # refused before any lock
    lease_s = float(settings.pg_request_lease_seconds)
    started = time.monotonic()  # before the commit: the lease runs from its start
    async with pg.transaction() as conn:
        await conn.execute(f"SET LOCAL lock_timeout = {int(settings.pg_commit_lock_timeout_ms)}")
        # Shared with every commit of the namespace; an erasure of it holds it alone.
        await conn.execute(
            f"SELECT pg_advisory_xact_lock_shared({pg.KNOWLEDGE_LOCK_KEY})", org, namespace
        )
        held = {
            r["source_uri"]: int(r["version"])
            for r in await conn.fetch(_LOCK_HEADS, org, namespace, uris)
        }
        # Read after the heads' locks, which an erasure holds until it commits.
        if await conn.fetchval(
            _ERASED,
            org,
            namespace,
            [*(str(r["doc_id"]) for r, _ in encoded), *built_from],
            settings.pg_erasure_evidence_hours,
        ):
            raise DocumentErased()
        for uri in uris:
            # A head past what the writer read moved since; one behind it is older than
            # a row the writer read from ClickHouse (a build that commits no heads
            # wrote it), which this commit adopts.
            if uri in held and held[uri] > int(over.get(uri, 0)):
                raise HeadMoved(uri)
            # A row not above what it replaces would move the head back (a host whose
            # clock runs behind, a version taken before its read).
            if int(by_uri[uri]["version"]) <= max(held.get(uri, 0), int(over.get(uri, 0))):
                raise HeadMoved(uri)
        for row, stored in encoded:
            uri = str(row["source_uri"])
            revision, version = int(row.get("revision") or 0), int(row["version"])
            head = (
                org,
                namespace,
                uri,
                version,
                revision,
                row.get("superseded_at") is not None,
                str(row.get("user_id") or "-"),
                stored,
                *((revision, version) if uri in chunked else (None, None)),
            )
            if uri in held:
                await conn.execute(_UPDATE_HEAD, *head)
            elif await conn.fetchval(_INSERT_HEAD, *head) is None:
                raise HeadMoved(uri)
            await conn.execute(
                _INSERT_DELIVERY,
                org,
                namespace,
                uri,
                int(row["version"]),
                str(row.get("user_id") or "-"),
                stored,
                int(targets[uri]),
                lease_s,
            )
    return pg_lease.lease_deadline(lease_s, started)


async def complete(org: str, namespace: str, source_uri: str, version: int, *, token: int) -> None:
    """A delivery the worker took over is published, under the token that holds it: kept
    settled with its lease ending now. The request it replaced may still land an insert,
    which an erasure of its scope waits out from that lease (`sweep_settled` deletes it
    once no such insert can land). A commit's own publication deletes its deliveries
    instead (`complete_own`)."""
    await pg.execute(
        "UPDATE loom.knowledge_deliveries SET settled = true, lease_until = now() "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3 AND version = $4 "
        "AND lease_token = $5",
        org,
        namespace,
        source_uri,
        version,
        token,
    )


async def complete_own(rows: Sequence[Mapping[str, Any]]) -> None:
    """The deliveries a commit's own publication finished (under COMMIT_TOKEN), for each
    of one namespace's committed rows: deleted, in one statement. A delivery another
    holder took over is left to it, which settles it (`complete`)."""
    await pg.execute(
        "DELETE FROM loom.knowledge_deliveries d USING unnest($3::text[], $4::bigint[]) "
        "AS k(source_uri, version) WHERE d.org = $1 AND d.namespace = $2 "
        "AND (d.source_uri, d.version) = (k.source_uri, k.version) AND d.lease_token = $5",
        rows[0]["org"],
        rows[0]["namespace"],
        [str(r["source_uri"]) for r in rows],
        [int(r["version"]) for r in rows],
        COMMIT_TOKEN,
    )


class Head(NamedTuple):
    """A source's head as a read finds it: the loom.documents row, and whether a delivery
    of that version is still owed to ClickHouse."""

    row: dict[str, Any]
    owed: bool


async def heads(org: str, namespace: str, uris: Sequence[str]) -> dict[str, Head]:
    """The heads of these sources (deleted ones included). A key Postgres cannot store
    holds no head (`encode_row` refuses one), and is not sent."""
    uris = [u for u in uris if not pg.unstorable(u)]
    if not uris or pg.unstorable([org, namespace]):
        return {}
    got = await pg.fetch(
        "SELECT h.source_uri, h.row, EXISTS (SELECT 1 FROM loom.knowledge_deliveries d "
        "WHERE (d.org, d.namespace, d.source_uri, d.version) = "
        "(h.org, h.namespace, h.source_uri, h.version) AND NOT d.settled) AS owed "
        "FROM loom.knowledge_heads h "
        "WHERE h.org = $1 AND h.namespace = $2 AND h.source_uri = ANY($3::text[])",
        org,
        namespace,
        sorted(set(uris)),
    )
    return {str(r["source_uri"]): Head(decode_row(r["row"]), bool(r["owed"])) for r in got}


async def serving(org: str, sources: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Serving]:
    """What the heads of these (namespace, source_uri) pairs serve, in one read; a pair
    with no head (or a key Postgres cannot store, which holds none) is absent."""
    pairs = sorted({s for s in sources if not pg.unstorable(s)})
    if not pairs or pg.unstorable(org):
        return {}
    got = await pg.fetch(
        "SELECT h.namespace, h.source_uri, h.deleted, h.revision, h.chunks_revision, "
        "h.chunks_version FROM loom.knowledge_heads h "
        "JOIN unnest($2::text[], $3::text[]) AS s(namespace, source_uri) "
        "ON (h.namespace, h.source_uri) = (s.namespace, s.source_uri) WHERE h.org = $1",
        org,
        [ns for ns, _ in pairs],
        [uri for _, uri in pairs],
    )
    return {
        (str(r["namespace"]), str(r["source_uri"])): Serving(
            bool(r["deleted"]),
            int(r["revision"]),
            None if r["chunks_revision"] is None else int(r["chunks_revision"]),
            None if r["chunks_version"] is None else int(r["chunks_version"]),
        )
        for r in got
    }


async def drop_unbacked(org: str, namespace: str, source_uri: str, version: int) -> None:
    """Delete the source's head at `version` when it owes ClickHouse nothing: a head
    whose published row ClickHouse no longer holds was erased by a build that keeps no
    heads, and must neither serve nor hold back the source's next write."""
    await pg.execute(
        "DELETE FROM loom.knowledge_heads h WHERE h.org = $1 AND h.namespace = $2 "
        "AND h.source_uri = $3 AND h.version = $4 AND NOT EXISTS "
        "(SELECT 1 FROM loom.knowledge_deliveries d WHERE (d.org, d.namespace, d.source_uri) "
        "= (h.org, h.namespace, h.source_uri) AND NOT d.settled)",
        org,
        namespace,
        source_uri,
        version,
    )


async def committed_revision(
    org: str, namespace: str, source_uri: str, ingestion_id: str
) -> int | None:
    """The revision an ingestion job committed for the source, from its head or a
    delivery still owed: a commit the worker publishes counts as published."""
    if pg.unstorable([org, namespace, source_uri, ingestion_id]):
        return None  # no head or delivery holds what Postgres cannot store
    got = await pg.fetchval(
        "SELECT max((row->>'revision')::int) FROM ("
        "  SELECT row FROM loom.knowledge_heads "
        "  WHERE org = $1 AND namespace = $2 AND source_uri = $3 "
        "  UNION ALL SELECT row FROM loom.knowledge_deliveries "
        "  WHERE org = $1 AND namespace = $2 AND source_uri = $3) AS r "
        "WHERE row->>'ingestion_id' = $4",
        org,
        namespace,
        source_uri,
        ingestion_id,
    )
    return None if got is None else int(got)


async def claim_due(limit: int) -> tuple[list[Delivery], float]:
    """Deliveries whose lease ran out, taken under the next token for another lease:
    publications a request did not finish, and the `time.monotonic()` past which their
    publication must send nothing more (`pg_lease.leased`). `attempts` counts the
    publishes ClickHouse refused before (`failed`)."""
    lease_s = float(settings.pg_delivery_lease_seconds)  # the worker's, as memory's
    started = time.monotonic()  # before the claim: the lease runs from its start
    got = await pg.fetch(
        "UPDATE loom.knowledge_deliveries d SET lease_token = d.lease_token + 1, "
        "lease_until = now() + make_interval(secs => $2) "
        "FROM (SELECT org, namespace, source_uri, version FROM loom.knowledge_deliveries "
        "WHERE lease_until < now() AND NOT settled ORDER BY lease_until LIMIT $1 "
        "FOR UPDATE SKIP LOCKED) due "
        "WHERE (d.org, d.namespace, d.source_uri, d.version) = "
        "(due.org, due.namespace, due.source_uri, due.version) "
        "RETURNING d.org, d.namespace, d.source_uri, d.version, d.row, d.targets, "
        "d.lease_token, d.attempts",
        limit,
        lease_s,
    )
    claims = [
        Delivery(
            str(r["org"]),
            str(r["namespace"]),
            str(r["source_uri"]),
            int(r["version"]),
            decode_row(r["row"]),
            int(r["targets"]),
            int(r["lease_token"]),
            int(r["attempts"]),
        )
        for r in got
    ]
    return claims, pg_lease.lease_deadline(lease_s, started)


async def failed(delivery: Delivery, error: str, *, park: bool) -> None:
    """ClickHouse refused a publish of `delivery`: one more attempt counted and its error
    kept; `park`: past the attempts allowed, its lease made endless so no claim takes it
    again until an operator acts, and the failure's time kept in `fenced_until`."""
    await pg.execute(
        "UPDATE loom.knowledge_deliveries SET attempts = attempts + 1, last_error = $6, "
        # Parked: the send that failed started before now, so the insert bound from now
        # covers it (`pg.LEASE_LEFT_S`).
        "fenced_until = CASE WHEN $7 THEN LEAST(lease_until, now()) ELSE fenced_until END, "
        "lease_until = CASE WHEN $7 THEN 'infinity'::timestamptz ELSE lease_until END "
        "WHERE org = $1 AND namespace = $2 AND source_uri = $3 AND version = $4 "
        "AND lease_token = $5",
        delivery.org,
        delivery.namespace,
        delivery.source_uri,
        delivery.version,
        delivery.token,
        error[:2000],
        park,
    )


async def release(deliveries: Sequence[Delivery]) -> None:
    """Claims a pass did not publish, ClickHouse refusing nothing: due again at once,
    with no attempt counted."""
    if not deliveries:
        return
    await pg.execute(
        "UPDATE loom.knowledge_deliveries d SET lease_until = now() "
        "FROM unnest($1::text[], $2::text[], $3::text[], $4::bigint[], $5::bigint[]) "
        "AS k(org, namespace, source_uri, version, token) "
        "WHERE (d.org, d.namespace, d.source_uri, d.version, d.lease_token) "
        "= (k.org, k.namespace, k.source_uri, k.version, k.token)",
        [d.org for d in deliveries],
        [d.namespace for d in deliveries],
        [d.source_uri for d in deliveries],
        [d.version for d in deliveries],
        [d.token for d in deliveries],
    )


async def owed_originals(org: str, namespace: str) -> set[str]:
    """The originals and extraction artifacts the live documents of the namespace's
    unpublished commits name (owed or parked deliveries; a tombstone or an erased
    original needs none), and the originals its open ingestion jobs read: their
    ClickHouse rows may not have landed, so the originals sweep, which reads ClickHouse,
    may not see them named."""
    if pg.unstorable([org, namespace]):
        return set()  # no delivery or job holds what Postgres cannot store
    rows = await pg.fetch(
        f"SELECT sha FROM loom.knowledge_deliveries, {OWED_SHAS} AS sha "
        f"WHERE org = $1 AND namespace = $2 AND {NEEDS_ORIGINAL} "
        "UNION SELECT row->>'sha256' FROM loom.knowledge_jobs "
        f"WHERE org = $1 AND namespace = $2 AND {OPEN_JOB}",
        org,
        namespace,
    )
    return {str(r["sha"]) for r in rows}


async def unpublished(org: str, namespace: str) -> tuple[int, int]:
    """The namespace's commits whose publication has not finished: (owed, parked), owed
    ones still within a publisher's lease or due for the delivery worker, parked ones past
    LOOM_PG_DELIVERY_MAX_ATTEMPTS refusals. A delivery whose row landed but whose older
    chunk versions are not retired yet counts too."""
    if pg.unstorable([org, namespace]):
        return 0, 0  # no delivery holds what Postgres cannot store
    row = await pg.fetchrow(
        "SELECT count(*) FILTER (WHERE lease_until <> 'infinity') AS owed, "
        "count(*) FILTER (WHERE lease_until = 'infinity') AS parked "
        "FROM loom.knowledge_deliveries WHERE org = $1 AND namespace = $2 AND NOT settled",
        org,
        namespace,
    )
    return int(row["owed"]), int(row["parked"])


async def parked_count() -> int:
    """Deliveries past LOOM_PG_DELIVERY_MAX_ATTEMPTS: documents committed in Postgres
    that ClickHouse keeps refusing; an operator reads `last_error`."""
    return int(
        await pg.fetchval(
            "SELECT count(*) FROM loom.knowledge_deliveries "
            "WHERE lease_until = 'infinity' AND NOT settled"  # the queue's index
        )
    )


async def sweep_erasure_evidence(limit: int) -> int:
    """Delete up to `limit` erased documents' ids older than LOOM_PG_ERASURE_EVIDENCE_HOURS."""
    n = await pg.fetchval(
        "WITH gone AS (DELETE FROM loom.erased_documents WHERE ctid IN ("
        "  SELECT ctid FROM loom.erased_documents "
        "  WHERE erased_at < now() - make_interval(hours => $1) "
        "  ORDER BY erased_at LIMIT $2 FOR UPDATE SKIP LOCKED) RETURNING 1) "
        "SELECT count(*) FROM gone",
        settings.pg_erasure_evidence_hours,
        limit,
    )
    return int(n)


async def sweep_settled(limit: int) -> int:
    """Delete up to `limit` settled deliveries (`complete`) whose lease is older than the
    insert bound: no request they replaced can still land a row."""
    n = await pg.fetchval(
        "WITH gone AS (DELETE FROM loom.knowledge_deliveries WHERE ctid IN ("
        "  SELECT ctid FROM loom.knowledge_deliveries "
        "  WHERE settled AND lease_until < now() - make_interval(secs => $1) "
        "  ORDER BY lease_until LIMIT $2 FOR UPDATE SKIP LOCKED) RETURNING 1) "
        "SELECT count(*) FROM gone",
        pg.insert_bound_s(),
        limit,
    )
    return int(n)


async def drop_version(org: str, namespace: str, source_uri: str, version: int) -> list[float]:
    """Delete the source's head while it is `version`, and that version's delivery: a
    commit its writer takes back. Returns the seconds left on the deleted delivery's
    lease (`pg.LEASE_LEFT_S`), which the writer waits out before it cleans ClickHouse:
    a holder of it may still land the row."""
    async with pg.transaction() as conn:
        await conn.execute(
            "DELETE FROM loom.knowledge_heads WHERE org = $1 AND namespace = $2 "
            "AND source_uri = $3 AND version = $4",
            org,
            namespace,
            source_uri,
            version,
        )
        left = await conn.fetch(
            "DELETE FROM loom.knowledge_deliveries d WHERE org = $1 AND namespace = $2 "
            f"AND source_uri = $3 AND version = $4 RETURNING {pg.LEASE_LEFT_S} AS left_s",
            org,
            namespace,
            source_uri,
            version,
        )
    return [float(r["left_s"]) for r in left]


async def publication_may_land(org: str, namespace: str, source_uri: str) -> bool:
    """Whether a publication of the source may still land in ClickHouse: a delivery is
    owed, or one the worker took over and settled is within the insert bound of its
    lease, so the request it replaced may still be sending."""
    return bool(
        await pg.fetchval(
            "SELECT EXISTS (SELECT 1 FROM loom.knowledge_deliveries WHERE org = $1 "
            "AND namespace = $2 AND source_uri = $3 AND (NOT settled "
            "OR lease_until > now() - make_interval(secs => $4)))",
            org,
            namespace,
            source_uri,
            pg.insert_bound_s(),
        )
    )
