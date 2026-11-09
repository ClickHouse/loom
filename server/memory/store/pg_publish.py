"""Commit loom.memory rows to the Postgres authority, then publish them to ClickHouse.

The commit is the write: once it returns, the revision, its hash-only audit row
and what it owes ClickHouse are durable. Publication goes through the process's
batch (`pg_batch`), from the rows as committed, under the delivery lease the commit
took (`COMMIT_TOKEN`). A publication cut off by a crash or a failed insert leaves the
delivery for the delivery worker, which publishes the same rows again: loom.memory
and loom.memory_embeddings are ReplacingMergeTree(version), so a second copy of a
version collapses into the first, and an audit row already stored is skipped by
its hash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from server.core import pg_lease, rmt_carry
from server.core.ch_writes import batched_insert_settings
from server.core.error_summary import error_summary
from server.core.hashing import hash_text
from server.core.repo_base import _CHClient, _default_client, query_all_sliced
from server.memory.store import pg_memory
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.pg_memory import (
    AUDIT_ROW,
    MEMORY_ROW,
    Admission,
    Committed,
)
from server.memory.store.records import tombstone_decision_records
from server.memory.store.rows import identity_key, next_version
from server.memory.store.sql import _SEQUENTIAL

log = logging.getLogger(__name__)


class WriterNotWired(RuntimeError):
    """A commit in a process that never ran `server.wiring.wire`: refused, rather than an
    audit row that names the wrong writer."""


# Who the request writes as (`server.memory.app.provenance.writer_actor`): the store
# cannot read the request's token, so `server.wiring.wire` installs it (`use_writer`).
_writer: Callable[[], str] | None = None


def use_writer(writer: Callable[[], str]) -> None:
    """Hand in the request's writer (`server.wiring.wire`)."""
    global _writer
    _writer = writer


def audit_row_for(row: Mapping[str, Any], *, request_id: str, writer: str) -> dict[str, Any]:
    """The hash-only audit row of one committed revision, naming `writer` (else `-` in a
    request and `process:loom` outside one, Loom's own jobs), never the value."""
    from server.observability.audit import audit_row

    return audit_row(
        "memory.revision",
        str(row["org"]),
        str(row["namespace"]),
        writer or ("-" if request_id else "process:loom"),
        {
            "memory_key": str(row["memory_key"]),
            "memory_id": str(row["memory_id"]),
            "version": int(row["version"]),
            "content_sha256": str(row.get("content_sha256") or ""),
            "source_class": str(row.get("source_class") or ""),
            "status": str(row.get("status") or "active"),
            "pinned": bool(row.get("pinned")),
            "tombstoned": row.get("tombstoned_at") is not None,
        },
        request_id=request_id,
        ts=datetime.now(UTC),
    )


def _request_id() -> str:
    from server.core.logging_config import request_id_var

    return request_id_var.get() or ""


async def commit(
    rows: Sequence[Mapping[str, Any]],
    *,
    targets: int | Sequence[int] = MEMORY_ROW | AUDIT_ROW,
    admission: Admission | None = None,
    idempotent: bool = False,
    publisher_lease: bool = True,
) -> Committed:
    """The commit alone; `publish` sends it. `idempotent`: these are the op's own rows,
    which the request's idempotency key records as committed (another write the request
    makes is not the op's answer).
    `publisher_lease` False: nobody publishes it here, so the delivery worker may at once."""
    from server.core import idempotency

    if _writer is None:
        raise WriterNotWired("no audit writer is wired into this process")
    rid, writer = _request_id(), _writer()
    return await pg_memory.commit_versions(
        rows,
        # the caller, never the earlier writer a copy carries
        [audit_row_for(r, request_id=rid, writer=writer) for r in rows],
        targets=targets,
        admission=admission,
        request_id=rid,
        idempotency=idempotency.bound_key() if idempotent else None,
        publisher_lease=publisher_lease,
    )


async def publish(
    committed: Committed,
    *,
    ch: _CHClient | None = None,
    retried: bool = False,
    documents: Sequence[Mapping[str, Any]] = (),
    wait: bool = True,
) -> None:
    """Publish a commit through this process's batch (`pg_batch`): its rows, audit rows
    and the search `documents` its writer built, in one flush with every other commit
    queued, then the old vectors it retires and the decision records. The commit stands
    whatever fails there: a failed insert or vector retirement is PublicationPending
    (`committed`), never an error a retry without an idempotency key would write again,
    and leaves the rest to the delivery worker; a record left owed is the worker's
    without a word to the caller. `wait` False: return at once, at the commit; the
    flush's outcome is logged."""
    from server.memory.store import pg_batch

    await pg_batch.submit(committed, documents=documents, retried=retried, ch=ch, wait=wait)


async def publish_rows(
    rows: Sequence[Mapping[str, Any]],
    audits: Sequence[Mapping[str, Any]],
    seqs: Sequence[tuple[str, str, int]],
    *,
    tokens: Sequence[int] | int,
    deadline: float,
    retried: bool,
    ch: _CHClient | None = None,
) -> BaseException | None:
    """The memory rows and the audit rows of these deliveries into ClickHouse, then
    each target cleared under its holder's token. The audit rows are already durable
    in Postgres: an audit row ClickHouse refuses stays owed and does not fail a
    request's publish (the delivery worker sends it again); a memory row it refuses
    fails the call; the audit rows' error is returned, for the delivery worker to count
    once its search documents are in (LOOM_PG_DELIVERY_MAX_ATTEMPTS). `retried`: an
    earlier attempt may have stored some of the audit rows, which are not inserted
    again."""
    ch = ch or _default_client()
    with pg_lease.leased(deadline):
        memory_done, audit_done = await asyncio.gather(
            insert_memory_versions(rows, ch=ch, published=True),
            insert_audit_rows(audits, ch=ch, retried=retried),
            return_exceptions=True,
        )
    if isinstance(memory_done, BaseException):
        raise memory_done
    landed = MEMORY_ROW
    if isinstance(audit_done, BaseException):
        log.warning(
            "audit rows not published yet; the delivery worker retries: %s",
            error_summary(audit_done),
        )
    else:
        landed |= AUDIT_ROW
    await pg_memory.complete(seqs, landed, tokens=tokens)
    return audit_done if isinstance(audit_done, BaseException) else None


async def retire_old_vectors(
    rows: Sequence[Mapping[str, Any]],
    seqs: Sequence[tuple[str, str, int]],
    *,
    tokens: Sequence[int] | int,
    deadline: float,
    ch: _CHClient,
) -> None:
    """Retire the vectors below each row's version, which its delivery owes
    (`OLD_VECTORS`), under the lease ending at `deadline`, and clear the target once they
    are in. The tombstones hold no text (`retire_vectors_for_keys(bare=True)`): a
    redaction can purge the key while one is in flight."""
    from server.core.ch import sequential_reads
    from server.memory.store.embeddings import retire_vectors_for_keys

    # A retirement reads first: on a replica that has what landed before it.
    with pg_lease.leased(deadline), sequential_reads():
        await retire_vectors_for_keys(
            [(identity_key(r), int(r["version"])) for r in rows], ch=ch, bare=True
        )
    await pg_memory.complete(seqs, pg_memory.OLD_VECTORS, tokens=tokens)


class OwedRecord(NamedTuple):
    """A delivery that owes its deleted decision's record retirement."""

    row: Mapping[str, Any]
    delivery: tuple[str, str, int]  # (org, namespace, seq)
    token: int


async def retire_decision_records(
    owing: Sequence[OwedRecord], *, deadline: float, ch: _CHClient
) -> BaseException | None:
    """Retire the records of the deleted decisions these deliveries owe, a namespace at a
    time, and clear each namespace's target as it lands. Here the delete is committed,
    whatever its writer's answer was; the retirement keeps a record whose writer the
    authority holds live, or written since. A namespace that fails stays owed and the
    rest go on; the first error is returned for the delivery worker to count."""
    by_ns: dict[tuple[str, str], list[OwedRecord]] = {}
    for owed in owing:
        org, namespace, _ = owed.delivery
        by_ns.setdefault((org, namespace), []).append(owed)
    first: BaseException | None = None
    for (org, namespace), group in by_ns.items():
        keys = sorted({str(o.row["memory_key"]) for o in group})
        try:
            # Drawn before the live read: a record written after it is above it, within
            # the hosts' clock skew, as every `next_version` is.
            version = next_version()
            live = await pg_memory.live_decision_writers(org, namespace, keys)
            with pg_lease.leased(deadline):
                await tombstone_decision_records(
                    org, namespace, keys, version=version, ch=ch, live=live
                )
            await pg_memory.complete(
                [o.delivery for o in group],
                pg_memory.DECISION_RECORD,
                tokens=[o.token for o in group],
            )
        except Exception as e:  # noqa: BLE001 — the rows stand; the record stays owed
            log.warning(
                "decision record retirement still owed in %s/%s: %s",
                org,
                namespace,
                error_summary(e),
            )
            first = first or e
    return first


class ClaimExpired(TimeoutError):
    """The audit rows' claim ran out before their insert: another claimer may hold them."""


async def insert_audit_rows(
    audits: Sequence[Mapping[str, Any]],
    *,
    ch: _CHClient,
    retried: bool,
    start_by: float | None = None,
) -> None:
    """`start_by` (monotonic): nothing is read or sent after it (`ClaimExpired`), and
    the insert starts on the server within the transit allowance of it."""
    rows = [dict(a) for a in audits]
    if start_by is not None and time.monotonic() > start_by:
        raise ClaimExpired
    if retried and rows:
        # Bounded by the rows' own namespaces and times, so the read stays a few granules;
        # what another publisher landed, read from a replica that has it.
        have = await ch.query_all(
            "SELECT row_hash FROM loom.audit_log "
            "WHERE org IN {orgs:Array(String)} AND namespace IN {nss:Array(String)} "
            "AND event_type = 'memory.revision' "
            "AND ts BETWEEN {first:DateTime64(3)} AND {last:DateTime64(3)} "
            "AND row_hash IN {hashes:Array(String)} " + _SEQUENTIAL,
            params={
                "orgs": sorted({str(r["org"]) for r in rows}),
                "nss": sorted({str(r["namespace"]) for r in rows}),
                "first": min(r["ts"] for r in rows),
                "last": max(r["ts"] for r in rows),
                "hashes": [r["row_hash"] for r in rows],
            },
        )
        stored = {hash_text(h["row_hash"]) for h in have}
        rows = [r for r in rows if r["row_hash"] not in stored]
    if rows and start_by is not None and time.monotonic() > start_by:
        raise ClaimExpired
    if not rows:
        return
    # Durable in Postgres already: ClickHouse's copy shares a flush with other writes,
    # except one sent under a lease or a claim, which starts and ends within it.
    claim = rmt_carry.under_lease(start_by) if start_by is not None else contextlib.nullcontext()
    with claim:
        await ch.insert("loom.audit_log", rows, settings=batched_insert_settings())


def document_id(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """A search document's id as `held_documents` matches it: its row's writer, key and
    version."""
    return (
        str(row["agent"]),
        str(row["user_id"]),
        str(row["session_id"]),
        str(row["memory_key"]),
        int(row["version"]),
    )


async def held_documents(
    org: str, namespace: str, doc_ids: Sequence[tuple[Any, ...]], *, ch: _CHClient
) -> set[tuple[Any, ...]]:
    """Which of these search documents, each (agent, user_id, session_id, memory_key,
    version), ClickHouse holds at that version: read from a replica that has them."""
    if not doc_ids:
        return set()
    rows = await query_all_sliced(
        ch,
        "SELECT agent, user_id, session_id, memory_key, version FROM loom.memory_embeddings "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND (agent, user_id, session_id, memory_key, version) IN "
        "{ids:Array(Tuple(String, String, String, String, UInt64))} " + _SEQUENTIAL,
        {"org": org, "ns": namespace, "ids": list(doc_ids)},
        "ids",
    )
    return {document_id(r) for r in rows}


async def commit_and_publish(
    rows: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None
) -> Committed:
    """`insert_memory_versions` under the Postgres authority."""
    committed = await commit(rows)
    await publish(committed, ch=ch)
    return committed
