"""The namespace publisher: turns memory writes into journal commits, in order.

Each namespace has one logical publisher. A write takes the namespace lock
(KeeperMap across processes and hosts; an in-process lock where one process
writes), reads what its keys serve, builds one commit per changed key, gives
each the next `commit_seq` and a recording time that rises with it, and inserts
them synchronously. That insert is the publication boundary: the loom.memory row
is written after it, as the current-state projection, and a write that cannot be
journaled fails before it.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from server.core import idempotency, request_work
from server.core.ch import current_query_tag, sequential_reads
from server.core.errors import (
    BackendUnavailableError,
    LoomServerError,
    PreconditionFailedError,
    TemporalUnavailableError,
    ValidationError,
)
from server.core.logging_config import request_id_var
from server.core.repo_base import _CHClient, _default_client
from server.core.schema.migrate_lease import default_holder
from server.erasure.app import fence
from server.memory.app.temporal_reads import columns_of, erased
from server.memory.app.temporal_rows import (
    Ident,
    Served,
    ident_of,
    projection_row,
    served_from_row,
)
from server.memory.domain import temporal_model as tm
from server.memory.domain.temporal_capture import (
    ASSESS_FIELDS,
    WriteIntent,
    amendment_for_row,
    assertion_from_row,
    assess_claims,
    current_intent,
    declared_fields,
    intent_for,
    note_left_out,
    removes,
    slot_of,
    write_intent,
)
from server.memory.store import temporal as store
from server.memory.store.key_write_lock import KeyWriteLocks
from server.observability import degradations
from server.settings import settings

log = logging.getLogger(__name__)

PUBLISHER_ID = default_holder()
_locks = KeyWriteLocks(wait_s=settings.memory_temporal_lock_wait_s, ttl_s=60)
_receipts: ContextVar[list[Receipt] | None] = ContextVar("loom_temporal_receipts", default=None)
# Set while the erasure fence takes rows back out: those rows are not fenced again.
_undoing: ContextVar[bool] = ContextVar("loom_temporal_undoing", default=False)

# A handler's change to one key, built from what the key serves when it is planned.
Build = Callable[[list[tm.Assertion]], tm.Amendment]


@dataclass(frozen=True)
class Receipt:
    namespace: str
    ident: Ident
    commit_id: str
    commit_seq: int
    recorded_at: datetime
    change_kind: str
    # The committed change (a retry's is read back from the earlier commit).
    amendment: tm.Amendment | None = field(default=None, compare=False, repr=False)
    # A retry answered from an earlier commit that a later one has since replaced:
    # its row is not written again.
    moved_on: bool = False
    # A retry's: the memory id its commit recorded, which its row is written under.
    retried_memory_id: str = ""
    # Late evidence that filled the past: what applies now is unchanged, so its row
    # (not the current state) is not written.
    past_only: bool = False
    # A retry's: the loom.memory version its commit's row was written at.
    row_version: int = 0
    # A retry's: the request entry its commit answered (`request_slot`; -1: none).
    request_slot: int = -1
    # Why the row was neither journaled nor written: superseded (a later-drawn
    # version committed first), locked (a change the key's lock in force refuses), restated
    # (nothing changed and the value is not what applies now).
    left_out: str = ""
    # The version row this receipt answers for (a row-writing change's): a batch can
    # carry several rows of one key, each with its own outcome.
    row: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @property
    def memory_key(self) -> str:
        return self.ident[3]


def _bag() -> list[Receipt] | None:
    """Where this context's receipts go: an open `collect_receipts` block, else the
    request's own list (every op's writes report what the journal left out)."""
    bag = _receipts.get()
    if bag is not None:
        return bag
    work = request_work.current()
    return work.temporal_receipts if work is not None else None


@contextmanager
def collect_receipts() -> Iterator[list[Receipt]]:
    """Receipts of every commit published inside the block (a handler reads them
    into its response's read token)."""
    outer = _bag()
    bag: list[Receipt] = []
    token = _receipts.set(bag)
    try:
        yield bag
    finally:
        _receipts.reset(token)
        if outer is not None:  # a nested block's commits are the enclosing one's too
            outer.extend(bag)


def committed_memory_id(namespace: str, ident: Ident) -> str:
    """The memory id a retry of this identity in this request was committed under
    ('' when it was not a retry)."""
    bag = _bag() or []
    return next(
        (
            r.retried_memory_id
            for r in bag
            if r.retried_memory_id and (r.namespace, r.ident) == (namespace, ident)
        ),
        "",
    )


def receipt_of(row: Mapping[str, Any]) -> Receipt | None:
    """The receipt this request's publication gave this very row."""
    return next((r for r in reversed(_bag() or []) if r.row is row), None)


def replayed(namespace: str, ident: Ident) -> Receipt | None:
    """The receipt of this identity whose row was not written: a retry whose key
    has moved on, a write that only filled the past, or one left out."""
    bag = _bag() or []
    return next(
        (
            r
            for r in reversed(bag)
            if (r.moved_on or r.past_only or r.left_out)
            and (r.namespace, r.ident) == (namespace, ident)
        ),
        None,
    )


async def durable_commits(org: str, ns: str, idents: Sequence[Ident]) -> dict[Ident, list[Receipt]]:
    """Per identity, the receipts of this request's earlier commits under its client
    retry key, in order. A batch reads them before it filters anything out, so a
    retry answers as its commits did; the same key with another body is refused."""
    idem = idempotency.request_key_var.get()
    if not idem or not idents:
        return {}
    prior = await store.commits_for_idempotency_key(
        org, ns, idem, list(dict.fromkeys(idents)), erased=await erased(org, ns)
    )
    if any(
        str(c["request_sha256"]) != idempotency.request_body_var.get()
        for cs in prior.values()
        for c in cs
    ):
        raise PreconditionFailedError(
            "Idempotency-Key reused with a different request to this memory"
        )
    return {i: [_receipt(ns, i, c, moved_on=False) for c in cs] for i, cs in prior.items() if cs}


async def durable_replay(
    org: str, ns: str, ident: Ident, *, latest_too: bool = False
) -> Receipt | None:
    """The receipt of this request's earlier commit to `ident` under its client
    retry key (past the transport's 24 h cache) when the key has moved on since:
    a retry is answered from it before a guard meant for a new change (a pin or
    lock a later write set) refuses it. One that has not moved on goes the normal
    way and rewrites its row, unless `latest_too` (a handler that projects its own
    row): then its receipt answers too. The same key with another body is refused."""
    idem = idempotency.request_key_var.get()
    if not idem or (ns, ident) in _request_commits():  # this request's own commit is no retry
        return None
    erased_cut = await erased(org, ns)
    prior = (
        await store.commits_for_idempotency_key(org, ns, idem, [ident], erased=erased_cut)
    ).get(ident)
    if not prior:
        return None
    if str(prior[-1]["request_sha256"]) != idempotency.request_body_var.get():
        raise PreconditionFailedError(
            "Idempotency-Key reused with a different request to this memory"
        )
    last = (await store.last_commits(org, ns, [ident])).get(ident)
    latest = last in {str(c["commit_id"]) for c in prior}
    if latest and not latest_too:
        return None
    receipt = _receipt(ns, ident, prior[-1], moved_on=not latest)
    bag = _bag()
    if bag is not None:
        bag.append(receipt)
    return receipt


def row_assertion(amendment: tm.Amendment | None, value: str | None = None) -> tm.Assertion | None:
    """The assertion a commit took from its row: the reducer adds it last, after the
    segments a correction or transition keeps of older revisions. With the row's
    `value`, None when the commit added none with it (the claim was already served)."""
    added = amendment.added if amendment is not None else ()
    if value is not None:
        added = tuple(a for a in added if _same_value(a.value, value))
    return added[-1] if added else None


def _same_metadata(a: tm.Assertion, b: tm.Assertion) -> bool:
    return all(getattr(a, f) == getattr(b, f) for f in ASSESS_FIELDS)


def _same_value(a: str, b: str) -> bool:
    """The same stored value, however it was serialized: a row rebuilt from a read
    respaces JSON, and quotes a value stored as bare text."""
    return a == b or _canonical(a) == _canonical(b)


def _canonical(v: str) -> str:
    """Canonical JSON (bare text as a JSON string): 1, 1.0 and true stay distinct."""
    try:
        parsed = json.loads(v)
    except ValueError:
        parsed = v
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def read_token(receipts: Sequence[Receipt]) -> str:
    """The read-your-write token: the highest committed sequence per namespace."""
    from server.memory.domain.temporal_reads import encode_token

    seqs: dict[str, int] = {}
    for r in receipts:
        seqs[r.namespace] = max(seqs.get(r.namespace, 0), r.commit_seq)
    return encode_token(seqs)


def with_read_token[I, O](handler: Callable[[I], Awaitable[O]]) -> Callable[[I], Awaitable[O]]:
    """Wrap a write handler: the commits it publishes fill its output's
    `commit_seq` / `read_token`."""

    @functools.wraps(handler)
    async def wrapped(input: I) -> O:
        with collect_receipts() as bag:
            out = await handler(input)
        if bag and hasattr(out, "read_token"):
            out.read_token = read_token(bag)
            if hasattr(out, "commit_seq"):
                out.commit_seq = max(r.commit_seq for r in bag)
        return out

    return wrapped


async def publish_rows(
    rows: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None
) -> list[Mapping[str, Any]]:
    """Journal a batch of loom.memory version rows, then return the rows to insert:
    none for a moved-on retry or late evidence, a retry's as its commit recorded
    it. A batch that cannot be journaled fails the write."""
    intent = current_intent()
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in rows:
        if intent_for(r, intent).journaled:
            continue
        groups.setdefault((str(r.get("org", "")), str(r.get("namespace", ""))), []).append(r)
    # Per row (by object: a batch can carry several rows of one key), whether it is
    # written, and a retry's as its commit recorded it: the id, and the expiry its
    # assertions carry (the row's), not ones drawn anew.
    stale: set[int] = set()
    recorded: dict[int, dict[str, Any]] = {}
    for (org, ns), batch in groups.items():
        try:
            receipts = await publish(org, ns, [_RowChange(r) for r in batch], intent, ch=ch)
        except PreconditionFailedError:
            # The key's current row may lag the journal (a due plan): bring it in line,
            # so the re-read the refusal asks for sees what the journal holds.
            await refresh_current(org, ns, sorted({ident_of(r) for r in batch}))
            raise
        except LoomServerError:
            raise
        except Exception as e:  # re-raised as one named 503
            raise TemporalUnavailableError(f"temporal journal unavailable: {e}") from e
        stale |= {
            id(r.row)
            for r in receipts
            if r.row is not None and (r.moved_on or r.past_only or r.left_out)
        }
        for rc in receipts:
            if rc.retried_memory_id and rc.row is not None:
                own = row_assertion(rc.amendment, str(rc.row.get("value")))
                # A restatement's commit holds no assertion: its row is the one the
                # history mirror kept at the commit's version.
                was = (
                    await store.version_row(org, ns, rc.ident, rc.row_version, ch=ch)
                    if own is None and rc.row_version
                    else None
                )
                recorded[id(rc.row)] = (
                    {k: v for k, v in (was or {}).items() if k not in _NOT_RESTORED}
                    | {"memory_id": rc.retried_memory_id}
                    | ({"expires_at": own.expires_at} if own is not None else {})
                    | ({"version": rc.row_version} if rc.row_version else {})
                )
    out = []
    for r in rows:
        if id(r) in stale:
            continue
        if id(r) in recorded:  # in place, as a stamped version is: the caller's row is the commit's
            cast(dict[str, Any], r).update(recorded[id(r)])
        out.append(r)
    return out


# Columns a retry's row keeps from the request rather than the history mirror.
_NOT_RESTORED = frozenset({"org", "namespace", "agent", "user_id", "session_id", "memory_key"})


async def refence_rows(rows: Sequence[Mapping[str, Any]], *, ch: _CHClient | None = None) -> None:
    """Once a batch's current rows are in: an erasure of their scope requested since
    the write began takes them back out, vectors too, and refuses the write.
    A process lost between the insert and this check leaves the rows to
    the saga's census, or to an operator once the saga has completed."""
    import server.memory.store as memory_repo
    from server.core.datetimes import naive_utc_now

    if _undoing.get():
        return
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in rows:
        groups.setdefault((str(r["org"]), str(r["namespace"])), []).append(r)
    since = await _began(ch)
    for (org, ns), batch in groups.items():
        users = sorted({str(r["user_id"]) for r in batch})
        _, late = await fence.scope_erasures(org, ns, users, since - timedelta(seconds=1), ch=ch)
        if not late:
            continue
        now = naive_utc_now()
        undo = [
            {**r, "version": int(r["version"]) + 1, "tombstoned_at": now, "updated_at": now}
            for r in batch
        ]
        token = _undoing.set(True)
        try:
            with write_intent(WriteIntent(journaled=True)):
                await memory_repo.insert_memory_versions(undo, ch=ch)
        finally:
            _undoing.reset(token)
        await memory_repo.retire_vectors_for_keys(
            [(memory_repo.embedding_key(r), int(r["version"])) for r in undo], ch=ch
        )
        raise PreconditionFailedError(
            "an erasure of this namespace or user began during this write; re-read and retry"
        )


async def _began(ch: _CHClient | None, clock: datetime | None = None) -> datetime:
    """When this request (or worker pass) began, on ClickHouse's clock: the one an
    erasure's `requested_at` is stamped with. The host clock's offset is measured
    now, so the comparison is between times of one clock."""
    clock = clock or await store.server_now(ch=ch)
    bag = request_work.current()
    if bag is None:
        return clock
    return datetime.fromtimestamp(bag.began_at, tz=UTC) + (clock - datetime.now(UTC))


class _RowChange:
    """A version row for one key, or a handler's amendment builder."""

    def __init__(self, row: Mapping[str, Any], build: Build | None = None) -> None:
        self.row = row
        self.build = build
        # As the writer built it: a re-plan merges from this, never from a row an
        # earlier attempt already merged.
        self.given = dict(row)

    def reset(self) -> None:
        r = cast(dict[str, Any], self.row)
        r.clear()
        r.update(self.given)

    @property
    def ident(self) -> Ident:
        return ident_of(self.row)


async def publish_amendment(
    org: str,
    namespace: str,
    identity: Ident,
    memory_id: str,
    build: Build,
    *,
    operation: str,
    scope: str = "private",
    ch: _CHClient | None = None,
) -> Receipt | None:
    """Publish an amendment a handler builds from the key's served assertions
    (memory.amend). Fails closed: an amendment that cannot be journaled did not
    happen."""
    out = await publish_amendments(
        org, namespace, [(identity, memory_id, build)], operation=operation, scope=scope, ch=ch
    )
    return out[0] if out else None


async def publish_amendments(
    org: str,
    namespace: str,
    items: Sequence[tuple[Ident, str, Build]],
    *,
    operation: str,
    scope: str = "private",
    recheck: Callable[[], Awaitable[None]] | None = None,
    ch: _CHClient | None = None,
) -> list[Receipt]:
    """Publish one built amendment per key as one batch (one plan, one lock): a
    receipt per key that committed; a build that returns an empty amendment commits
    nothing for its key. `recheck` runs under the lock before the commits: what a
    plan was built on that changes without a commit (a redaction) is checked there."""
    from server.memory.app.provenance import writer_actor
    from server.memory.domain.provenance import source_class_for

    # The commit's provenance is the caller's, as a version row would carry it.
    actor, source_class = writer_actor(), source_class_for(default="asserted")
    changes = [
        _RowChange(
            {
                "agent": agent,
                "user_id": uid,
                "session_id": sid,
                "memory_key": key,
                "memory_id": memory_id,
                "actor": actor,
                "source_class": source_class,
            },
            build,
        )
        for (agent, uid, sid, key), memory_id, build in items
    ]
    if not changes:
        return []
    return await publish(
        org,
        namespace,
        changes,
        WriteIntent(),
        operation=operation,
        scope=scope,
        recheck=recheck,
        ch=ch,
    )


async def publish(
    org: str,
    ns: str,
    changes: Sequence[_RowChange],
    intent: WriteIntent,
    *,
    operation: str = "",
    scope: str = "private",
    recheck: Callable[[], Awaitable[None]] | None = None,
    ch: _CHClient | None = None,
) -> list[Receipt]:
    """Plan the batch's commits against a head snapshot, then take the namespace
    lock only to re-read the head, check the erasure fence, stamp sequences and
    insert. A commit that touched the batch's keys since the snapshot, or a repair
    of the journal, re-plans the batch."""
    ch = ch or _default_client()
    operation = operation or (current_query_tag().split(" ")[0] or "unknown")
    with sequential_reads():
        for attempt in range(_REPLANS):
            if attempt:  # others are writing these keys: spread the re-plans out
                await asyncio.sleep(random.uniform(0, 0.02 * attempt))
            batch = await _plan(org, ns, changes, intent, operation, scope, ch)
            async with namespace_lock(org, ns) as still_held:
                seq, last_at, broken = await store.head(org, ns, ch=ch)
                if broken:
                    # Under the lock nobody else publishes: repair, then plan again
                    # (a retry's earlier commit may be the one deleted).
                    log.warning("temporal journal %s/%s: repairing sequence %s", org, ns, broken)
                    await store.repair_namespace(org, ns, ch=ch)
                    if (await store.head(org, ns, ch=ch))[2]:
                        raise TemporalUnavailableError(
                            f"namespace {ns!r} journal has an integrity break at sequence "
                            f"{broken} that could not be repaired; retry"
                        )
                    continue
                if seq != batch.seq and await _touched(batch, seq, ch):
                    continue
                # A projection lands rows without a commit, so the sequence holds still:
                # the plan is current, but its rows go above the versions that landed.
                await _above_landed(batch, ch)
                if recheck is not None:
                    await recheck()
                # Stamp, then fence: an erasure requested after the stamp has a later
                # request time than every commit of the batch, so reads never serve
                # the batch even if its withdrawal below fails.
                # One clock for every time the journal compares: ClickHouse's.
                clock = await store.server_now(ch=ch)
                last_at = tm.utc(last_at) or _EPOCH
                # Stamping past the clock would date the batch after an erasure
                # requested once the fence below passed, and reads would serve it. A
                # small gap (replicas' clocks differ by milliseconds) is waited out.
                if timedelta(0) < last_at - clock <= _CLOCK_WAIT:
                    await asyncio.sleep((last_at - clock).total_seconds())
                    clock = await store.server_now(ch=ch)
                if last_at > clock:
                    raise TemporalUnavailableError(
                        f"namespace {ns!r}: ClickHouse's clock is behind the journal's "
                        "last commit; retry"
                    )
                _stamp(batch, seq, last_at, clock)
                batch.generation = await _erasure_fence(org, ns, changes, ch, clock)
                for c in batch.commits:
                    c["erasure_generation"] = batch.generation
                if batch.commits:
                    if not await still_held():
                        raise TemporalUnavailableError(
                            f"namespace {ns!r} publisher lease lost; retry"
                        )
                    # The allocation is durable before the insert: an insert whose
                    # outcome is unknown keeps its sequences, so a later one never
                    # reuses them, and erasing the newest commits frees none.
                    last = batch.commits[-1]
                    await store.mark_published(
                        org, ns, last["commit_seq"], last["recorded_at"], ch=ch
                    )
                    await _insert_commits(org, ns, batch.commits, ch)
                    await _refence(org, ns, changes, batch, ch)
            if batch.commits:
                first = batch.commits[0]["commit_seq"]
                await _project(
                    org,
                    ns,
                    batch.projection,
                    batch.commits[-1]["commit_seq"],
                    first_seq=first,
                    ch=ch,
                )
            if batch.refresh:
                await refresh_current(org, ns, batch.refresh)
            bag = _bag()
            if bag is not None:
                bag.extend(batch.receipts)
            return batch.receipts
    raise TemporalUnavailableError(f"namespace {ns!r} is busy with writes to the same keys; retry")


# A batch re-plans at most this many times while other writers keep changing its keys.
_REPLANS = 8
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# A clock at most this far behind the journal's last commit is waited out, not refused.
_CLOCK_WAIT = timedelta(seconds=1)


@dataclass
class _Planned:
    ident: Ident
    memory_id: str
    amendment: tm.Amendment
    payload_sha: str
    row: Mapping[str, Any]
    retired: list[Served]
    past_only: bool = False
    # The journal's first record of a key written before it (`_seed`).
    seed: bool = False
    # A restatement's: the claim its row shows.
    restated: str = ""


@dataclass
class _Batch:
    """One publication: the head it was planned against, what the batch's keys
    served then, the planned changes, and the commits, projection rows and
    receipts stamping builds."""

    org: str
    ns: str
    scope: str
    operation: str
    seq: int
    keys: list[Ident]
    parents: dict[Ident, str]
    idem: str
    request_sha: str
    erased: store.Erased = store.NOT_ERASED
    generation: int = 0
    planned: list[_Planned] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    projection: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    # Keys a past-only change re-cut the current claim of (a plan bounding it): its
    # row is not the write's, so it is reconciled once the batch is published.
    refresh: list[Ident] = field(default_factory=list)


async def _plan(
    org: str,
    ns: str,
    changes: Sequence[_RowChange],
    intent: WriteIntent,
    operation: str,
    scope: str,
    ch: _CHClient,
) -> _Batch:
    """Every amendment of the batch, against what its keys serve at a head
    snapshot. No lock: `publish` re-validates the head before it stamps."""
    seq, _, _ = await store.head(org, ns, ch=ch)
    planned_at = datetime.now(UTC)
    keys = list(dict.fromkeys(c.ident for c in changes))
    gone = await erased(org, ns)
    served, heads, parents, committed_v = await asyncio.gather(
        _served(org, ns, keys, seq, gone, ch),
        store.memory_heads(org, ns, keys, ch=ch),
        store.last_commits(org, ns, keys, ch=ch),
        store.newest_row_versions(org, ns, keys, ch=ch),
    )
    b = _Batch(
        org=org,
        ns=ns,
        scope=scope,
        operation=operation,
        seq=seq,
        keys=keys,
        parents=parents,
        idem=idempotency.request_key_var.get(),
        request_sha=idempotency.request_body_var.get(),
        erased=gone,
    )
    head_rows = {ident_of(h): h for h in heads}
    # The claim each current row shows, when a restatement wrote it (its commit names it).
    for ident, aid in (await store.restated_claims(org, ns, heads, ch=ch)).items():
        if ident in head_rows:
            head_rows[ident] = {**head_rows[ident], _SHOWS: aid}
    # Per key, the newest loom.memory version the journal or the current row knows:
    # a row is stamped above it, so the merge and the journal agree on the latest.
    floor = {
        ident: max(int(committed_v.get(ident, 0)), int(head_rows[ident]["version"] or 0))
        if ident in head_rows
        else int(committed_v.get(ident, 0))
        for ident in keys
    }
    priors = await _retried_commits(b, ch)
    own = {ident: {str(c["commit_id"]) for c in cs} for ident, cs in priors.items()}
    for change in changes:
        change.reset()
        ident = change.ident
        prior = priors[ident].pop(0) if priors.get(ident) else None
        if prior is not None:
            # A retry is answered with the commit its first attempt made, whatever
            # the key serves now; the same key with another body is refused.
            if str(prior["request_sha256"]) != b.request_sha:
                raise PreconditionFailedError(
                    "Idempotency-Key reused with a different request to this memory"
                )
            # Moved on: the key's last commit is not one this request made.
            moved_on = b.parents.get(ident) not in (None, *own.get(ident, ()))
            receipt = _receipt(ns, ident, prior, moved_on=moved_on)
            assert receipt.amendment is not None  # read back from the commit
            held = [x.a for x in served.get(ident, {}).values()]
            past = _retry_past_only(receipt.amendment, held, planned_at)
            b.receipts.append(replace(receipt, past_only=past, row=change.row))
            continue
        current = served.setdefault(ident, {})
        if not current and ident not in parents:
            _seed(b, ident, head_rows.get(ident), current)
        active = [x.a for x in current.values()]
        if change.build is not None:
            amendment = change.build(active)
        else:
            row_intent = intent_for(change.row, intent)
            if row_intent.expected_version and _committed_here(ns, ident, b):
                # A precondition holds for the request's first write of the key; the
                # rows after it (a conflict mark, a status settle) build on that write.
                row_intent = replace(row_intent, expected_version=0)
            planned = _plan_row(
                change.row, row_intent, current, head_rows.get(ident), floor, ident, planned_at
            )
            if isinstance(planned, str):
                b.receipts.append(
                    replace(_no_commit(ns, ident, planned_at, planned), row=change.row)
                )
                note_left_out((org, ns, *ident), planned)
                continue
            amendment = planned
            head_rows[ident] = change.row
            floor[ident] = max(floor.get(ident, 0), int(change.row.get("version") or 0))
        if amendment.empty and amendment.change_kind != "restate":
            continue
        retired = [current.pop(aid) for aid in amendment.retired]
        memory_id = str(change.row.get("memory_id") or uuid.uuid4())
        for a in amendment.added:
            current[a.assertion_id] = Served(a, memory_id, 0, _EPOCH)
        b.planned.append(
            _planned(b, change, intent, amendment, active, memory_id, retired, planned_at)
        )
    return b


def _planned(
    b: _Batch,
    change: _RowChange,
    intent: WriteIntent,
    amendment: tm.Amendment,
    active: Sequence[tm.Assertion],
    memory_id: str,
    retired: list[Served],
    now: datetime,
) -> _Planned:
    """One change as planned: whether it is past-only (and re-cuts the current claim,
    so the key is refreshed after), and for a restatement the claim it names."""
    ident = change.ident
    past = change.build is None and _past_only(amendment, change.row, active, now)
    if past and _reshapes_current(active, amendment, now):
        b.refresh.append(ident)
    restated = ""
    if change.build is None and amendment.change_kind == "restate":
        shown = _restated(change.row, active, now, intent_for(change.row, intent))
        restated = shown.assertion_id if shown is not None else ""
    return _Planned(
        ident,
        memory_id,
        amendment,
        amendment.payload_sha256(),
        change.row,
        retired,
        past,
        restated=restated,
    )


def _past_only(
    amendment: tm.Amendment, row: Mapping[str, Any], active: Sequence[tm.Assertion], now: datetime
) -> bool:
    """Is the change journaled without its row (late evidence, a plan; or a removal
    that left other claims holding, whose row is the key's remaining claim)?"""
    if not _current_after(amendment, row, active, now):
        return True
    if not removes(row):
        return False
    # A removal of what holds now removes the key's row: ended history stays in the
    # journal (valid_as_of, memory.intervals), never shown as the present again.
    after = tm.apply({a.assertion_id: a for a in active}, amendment).values()
    return bool(tm.valid_now(after, now))


def _committed_here(ns: str, ident: Ident, b: _Batch) -> bool:
    """Has this request already committed to the key (this batch or an earlier one)?"""
    if any(p.ident == ident and not p.seed for p in b.planned):
        return True
    return any(r.namespace == ns and r.ident == ident and r.commit_seq for r in _bag() or [])


def _plan_row(
    row: Mapping[str, Any],
    intent: WriteIntent,
    current: Mapping[str, Served],
    head: Mapping[str, Any] | None,
    floor: dict[Ident, int],
    ident: Ident,
    now: datetime,
) -> tm.Amendment | str:
    """The change one version row makes, or why it is left out."""
    if intent.expected_revision and intent.expected_revision not in current:
        raise PreconditionFailedError(
            f"{ident[3]!r} no longer serves revision {intent.expected_revision}; re-read and retry"
        )
    active = [x.a for x in current.values()]
    fields = declared_fields(row)
    if fields is not None and "value" not in fields:
        fields = fields - {"content_sha256"}  # a hash follows its value: the journal's
    claims = tm.current_claims(active, now)
    shown = _claim_shown(claims, head) if claims else None
    if intent.expected_version and not _shows(shown, head, intent.expected_version):
        # Any write conditioned on a hash, against what the journal holds now (a hash
        # names nothing when nothing holds).
        raise PreconditionFailedError(
            f"{ident[3]!r} no longer holds the content its precondition names; re-read and retry",
            current_sha256=shown.content_sha256 if shown is not None else "",
        )
    if fields is None and intent.existing and not removes(row):
        if shown is not None and not intent.qualifiers:
            # An update revises the claim the row shows, under that claim's qualifiers
            # (what it inherits is that claim's, not another qualifier's).
            intent = replace(intent, qualifiers=dict(shown.qualifiers))
    left_out = _left_out_before(row, intent, active, head, floor, ident)
    if not left_out and intent.existing and not _same_memory(row, current, now):
        left_out = "superseded"  # the key was deleted and written anew since the read
    if not left_out:
        if fields is not None:
            memory_ids = {aid: x.memory_id for aid, x in current.items()}
            left_out = _as_metadata(row, fields, head, active, intent, now, memory_ids)
        else:
            left_out = _as_value_write(row, active, intent, now, head)
    if left_out:
        return left_out
    if fields is not None and not removes(row):
        # Only what it sets, on what holds now: the row was built from the journal.
        return _assess_fields(row, fields, active, now) or tm.Amendment("restate")
    if fields is not None and intent.conditional:
        # An expiry removes only the claims that have expired; the key's others stay.
        gone = [
            a.assertion_id
            for a in tm.current_claims(active, now)
            if a.expires_at is not None and a.expires_at <= now
        ]
        kind = "redact" if row.get("redacted_at") is not None else "retract"
        return tm.Amendment(kind, tuple(gone), ())
    try:
        amendment = amendment_for_row(row, active, intent, head, now=now)
    except tm.EmptyInterval as e:
        raise ValidationError(f"{ident[3]!r}: {e}") from e
    if not amendment.empty:
        return amendment
    if not _restates_now(row, active, now):
        return "restated"
    # Committed with nothing in it: its row's version orders the key's next write,
    # and the commit names the claim the row shows (`restated_assertion_id`), as
    # the same text may hold under other qualifiers.
    restated = _restated(row, active, now, intent)
    if restated is not None:
        # The row shows the claim it restates, with that claim's validity.
        r = cast(dict[str, Any], row)
        r["valid_at"], r["valid_to"] = tm.row_start(restated), restated.valid_to
    return tm.Amendment("restate")


def _reshapes_current(
    before: Sequence[tm.Assertion], amendment: tm.Amendment, now: datetime
) -> bool:
    """Does the change alter the claims the current row shows (or their validity)?"""
    after = tm.apply({a.assertion_id: a for a in before}, amendment).values()
    return {a.assertion_id for a in tm.current_claims(before, now)} != {
        a.assertion_id for a in tm.current_claims(after, now)
    }


async def refresh_current(org: str, ns: str, idents: list[Ident]) -> None:
    from server.memory.app.temporal_projection import reconcile_identities

    try:
        await reconcile_identities(org, ns, idents)
    except Exception as e:  # noqa: BLE001 — committed; the reconciler's pass repairs the row
        log.warning("temporal: %s/%s current rows left to the reconciler: %r", org, ns, e)


def _seed(
    b: _Batch, ident: Ident, head: Mapping[str, Any] | None, current: dict[str, Served]
) -> None:
    """A key written before the journal existed: its live row is journaled first, as
    a commit of its own, so the change after it revises or removes a real claim.
    It is not this request's change: no retry key, no receipt."""
    if head is None or removes(head) or _erased_before(head, b.erased):
        return
    seed = tm.Amendment("assert", (), (assertion_from_row(head, WriteIntent()),))
    memory_id = str(head["memory_id"])
    for a in seed.added:
        current[a.assertion_id] = Served(a, memory_id, 0, _EPOCH)
    b.planned.append(_Planned(ident, memory_id, seed, seed.payload_sha256(), head, [], seed=True))


def _erased_before(head: Mapping[str, Any], gone: store.Erased) -> bool:
    """A row drawn before an erasure of its scope: a leftover the cut hides, never
    journaled anew. (A version is its draw time in nanoseconds.)"""
    drawn_us = int(head.get("version") or 0) // 1000
    users = dict(gone.users)
    return drawn_us < gone.namespace_us or drawn_us < users.get(str(head.get("user_id")), 0)


def _left_out_before(
    row: Mapping[str, Any],
    intent: WriteIntent,
    served: Sequence[tm.Assertion],
    head: Mapping[str, Any] | None,
    floor: dict[Ident, int],
    ident: Ident,
) -> str:
    """Why a row is not journaled at all ('' when it is). Superseded: its version
    was drawn at or below one its key already committed (the later draw wins, as
    loom.memory's merge decides). Locked: a removal the key's lock in force refuses.
    A write meant for the memory it read (`intent.existing`) on a key the journal
    no longer serves is refused outright."""
    newest = max([floor.get(ident, 0), *(a.source_version for a in served)])
    drawn = int(row.get("version") or 0)
    if drawn and drawn <= newest:
        return "superseded"
    if removes(row) and _locked_against_removal(served, head):
        return "locked"
    if intent.existing and not served:
        raise PreconditionFailedError(
            f"{ident[3]!r} was removed since it was read; re-read and retry"
        )
    return ""


# Columns a metadata row never takes from the journal: its identity and stamps.
_NOT_REBASED = frozenset(
    {
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "memory_key",
        "memory_id",
        "version",
        "updated_at",
        "created_at",
    }
)
# One lock: (mode, deadline, reason, by whom, since when).
_LOCK = ("lock_mode", "lock_until", "lock_reason", "locked_by", "locked_at")
_UNLOCKED: dict[str, Any] = {
    "lock_mode": "",
    "lock_until": None,
    "lock_reason": "",
    "locked_by": "",
    "locked_at": None,
}
_STRICTNESS = {"": 0, "no_delete": 1, "read_only": 2}


def _as_metadata(
    row: Mapping[str, Any],
    fields: frozenset[str],
    head: Mapping[str, Any] | None,
    served: Sequence[tm.Assertion],
    intent: WriteIntent,
    now: datetime,
    memory_ids: Mapping[str, str],
) -> str:
    """A metadata write (it declared the columns it sets): its row becomes the key's
    state now, the journal's current claim (and the current row's columns the
    journal does not keep, when that row is as new as the claim), with only the
    declared columns its own. '' or why it is left out."""
    claims = tm.current_claims(served, now)
    if intent.conditional and claims:
        shown = _claim_shown(claims, head)
        if shown.expires_at is None or shown.expires_at > now:
            return "superseded"  # no longer expired: touched since the sweep read it
    if intent.tighten_lock and _loosens(row, claims, now):
        return "locked"
    if "expires_at" in fields and locked_against(served, "expire", now):
        return "locked"
    if intent.redirect and _scheduled(served, now):
        return "scheduled"  # a plan landed since the move checked: it would revive the key
    if not claims:
        # Nothing holds now: a removal of nothing restates; any other metadata write
        # (built from a read of what has since gone) changes nothing, never revives.
        return "" if removes(row) else "superseded"
    attests = str(row.get("verified_sha256") or "") if "verified_sha256" in fields else ""
    if attests and all(a.content_sha256 != attests for a in claims):
        return "superseded"  # attests text that no longer holds: never another's
    claim = _claim_shown(claims, head)
    if memory_ids.get(claim.assertion_id, "") not in ("", str(row.get("memory_id") or "")):
        return "superseded"  # the key was deleted and written anew since the read
    theirs = columns_of(claim)
    if head is not None and int(head.get("version") or 0) >= claim.source_version:
        theirs = dict(head) | theirs
    r = cast(dict[str, Any], row)
    for c, v in theirs.items():
        if c in r and c not in fields and c not in _NOT_REBASED:
            r[c] = v
    return ""


def _scheduled(served: Sequence[tm.Assertion], now: datetime) -> bool:
    """Does the key hold a claim that starts later (a plan not yet due)?"""
    return any(a.valid_from is not None and a.valid_from > now for a in served)


def _same_memory(row: Mapping[str, Any], current: Mapping[str, Served], now: datetime) -> bool:
    """Does what holds now belong to the memory the writer read (its memory id)? A
    key deleted and written anew is another memory: a write meant for the old one
    never lands on it."""
    mid = str(row.get("memory_id") or "")
    held = {a.assertion_id for a in tm.current_claims([x.a for x in current.values()], now)}
    ids = {x.memory_id for aid, x in current.items() if aid in held and x.memory_id}
    return not mid or not ids or mid in ids


def _revises_shown(
    r: dict[str, Any], claims: Sequence[tm.Assertion], head: Mapping[str, Any] | None
) -> None:
    """A revision of the memory it read (an update) states no validity of its own:
    it revises the claim the current row shows, with that claim's exact validity
    (loom.memory keeps validity to the second, and a row behind a due plan still
    shows the ended span)."""
    if claims:
        shown = _claim_shown(claims, head)
        r["valid_at"], r["valid_to"] = tm.row_start(shown), shown.valid_to


# A planner-only key on a head row: the assertion its restatement commit names.
_SHOWS = "__restated_assertion_id"


def _shows(shown: tm.Assertion | None, head: Mapping[str, Any] | None, version: int) -> bool:
    """Is the claim that holds now the one the row at `version` shows (the row a
    conditional write checked its hash against)?"""
    if shown is None:
        return False
    if shown.source_version == version:
        return True
    if head is None or int(head.get("version") or 0) != version:
        return False  # the row it checked is no longer the current one
    # Still the current row: it shows the claim when its restatement named it, or it
    # carries that claim's content (an assessment, a reconcile or an amend wrote it at
    # another version; a row behind a due plan carries other content).
    return str(head.get(_SHOWS) or "") == shown.assertion_id or (
        str(head.get("content_sha256") or "") == shown.content_sha256
    )


def _claim_shown(claims: Sequence[tm.Assertion], head: Mapping[str, Any] | None) -> tm.Assertion:
    """The current claim the current row shows: the one its restatement named, else
    the one it was written from, else the one with its content and validity, else
    (a row behind the journal) the newest."""
    if head is not None:
        named = str(head.get(_SHOWS) or "")
        for a in claims:
            if a.assertion_id == named:
                return a
        version = int(head.get("version") or 0)
        for a in claims:
            if a.source_version == version:
                return a
        shows = (
            str(head.get("content_sha256") or ""),
            tm.utc(head.get("valid_at")),
            tm.utc(head.get("valid_to")),
        )
        for a in claims:
            if (a.content_sha256, tm.row_start(a), a.valid_to) == shows:
                return a
    return max(claims, key=lambda a: a.source_version)


def _as_value_write(
    row: Mapping[str, Any],
    served: Sequence[tm.Assertion],
    intent: WriteIntent,
    now: datetime,
    head: Mapping[str, Any] | None = None,
) -> str:
    """A write of its own row (a value, a restatement): its content, metadata and
    removal are its own, the later draw wins; the lock is the journal's, as it holds
    now. Refused (left out) where the journal's state forbids it: a new value under
    `read_only`, or, for a write that guards pins, a value replacing a pinned one
    it did not pin. '' or why it is left out."""
    claims = tm.current_claims(served, now)
    r = cast(dict[str, Any], row)
    if all(c in r for c in _LOCK):
        r.update(_lock_in_force(claims, now))
    if removes(row):
        return ""
    if intent.existing:
        _revises_shown(r, claims, head)
    sha = str(row.get("content_sha256") or "")
    # Against the claim it changes (under its qualifiers), not every claim's text.
    # A write naming qualifiers no claim holds under is a new value there.
    under = [a for a in claims if a.qualifiers == dict(intent.qualifiers)]
    target = under if intent.qualifiers else (under or claims)
    new_value = bool(claims) and all(a.content_sha256 != sha for a in target)
    if new_value and locked_against(served, "value", now):
        return "locked"
    # Against what the caller asked for, before anything is inherited: a pin the row
    # would take from the journal never authorizes replacing the pinned value.
    own_pin = intent.pin_requested if intent.stated is not None else bool(row.get("pinned"))
    if intent.pin_guard and new_value and not own_pin and any(a.pinned for a in claims):
        return "pinned"
    _carry(r, claims, intent, now)
    return ""


_SENSITIVITY = {"": 0, "low": 1, "medium": 2, "high": 3}


def _carry(
    r: dict[str, Any], claims: Sequence[tm.Assertion], intent: WriteIntent, now: datetime
) -> None:
    """A value write's metadata its caller did not state, from the journal as it holds
    now rather than from the head its writer read:
    - the pin of the same text (a pin binds content), or, for an edit that does not
      guard pins (memory.update of a pinned value), of the value it replaces;
    - the expiry of the claim it replaces, while that expiry is still ahead and the
      write's own claim holds now (late evidence keeps its own);
    - the attestation of the same text, and a sensitivity never lower than the
      journal's for that text."""
    if not claims:
        return
    sha = str(r.get("content_sha256") or "")
    under = [a for a in claims if a.qualifiers == dict(intent.qualifiers)] or claims
    same = next((a for a in under if a.content_sha256 == sha), None)
    replaced = same or max(under, key=lambda a: a.source_version)
    stated = intent.stated
    if stated is not None:
        if "pinned" not in stated and "pinned" in r and (same is not None or not intent.pin_guard):
            r["pinned"] = int(replaced.pinned)
        if "expires_at" not in stated and "expires_at" in r:
            # The present claim's expiry is the present's: a plan or late evidence
            # keeps none of it (what its writer copied from the head included).
            ahead = (
                replaced.expires_at if replaced.expires_at and replaced.expires_at > now else None
            )
            # Under the qualifiers the write takes (its own, else the one set the key's
            # claims share, as `inherit_shape` gives it).
            quals = dict(intent.qualifiers) or _shared_qualifiers(claims)
            peers = [a for a in claims if a.qualifiers == quals]
            r["expires_at"] = tm.utc(ahead) if _holds_now(r, peers, now) else None
    if same is not None:
        for c in ("verified_by", "verified_at", "verified_sha256"):
            if c in r:
                r[c] = columns_of(same)[c]
        held = same.sensitivity
        if (stated is None or "sensitivity" not in stated) and _SENSITIVITY.get(
            held, 0
        ) > _SENSITIVITY.get(str(r.get("sensitivity") or ""), 0):
            r["sensitivity"] = held
            _sensitive_window(r, now)
            if "tags" in r:
                from server.memory.domain.sensitivity import relabel

                r["tags"] = relabel(r["tags"] or [], held)  # the label that holds


def _sensitive_window(r: dict[str, Any], now: datetime) -> None:
    """A row raised into a sensitive tier takes that tier's retention window, as the
    handler gives a row written at that tier."""
    from server.memory.domain.sensitive_retention import sensitive_retention

    expires_at, on_expire, _ = sensitive_retention(
        sensitivity=str(r.get("sensitivity") or ""),
        expires_at=tm.utc(r.get("expires_at")),
        on_expire=str(r.get("on_expire") or ""),
        now=now,
        settings=settings,
    )
    r["expires_at"] = expires_at
    if "on_expire" in r:
        r["on_expire"] = on_expire


def _shared_qualifiers(claims: Sequence[tm.Assertion]) -> dict[str, str]:
    sets = {tuple(sorted(a.qualifiers.items())) for a in claims}
    return dict(next(iter(sets))) if len(sets) == 1 else {}


def _holds_now(r: Mapping[str, Any], claims: Sequence[tm.Assertion], now: datetime) -> bool:
    """Does the row's own claim hold now, as the journal will store it: by its
    validity, and not bounded by a later state that has begun (a dated write stops
    at one; an undated one holds now)?"""
    start = tm.utc(r.get("valid_at")) or tm.utc(r.get("temporal_anchor"))
    end = tm.utc(r.get("valid_to"))
    if start is not None and any(
        a.valid_from is not None and start < a.valid_from <= now for a in claims
    ):
        return False
    return (start is None or start <= now) and (end is None or end > now)


def _lock_in_force(claims: Sequence[tm.Assertion], now: datetime) -> dict[str, Any]:
    """The lock columns of the strictest lock in force on what holds now."""
    live = [a for a in claims if a.lock_mode and (a.lock_until is None or a.lock_until > now)]
    if not live:
        return dict(_UNLOCKED)
    a = max(
        live,
        key=lambda x: (
            _STRICTNESS.get(x.lock_mode, 0),
            x.lock_until is None,
            x.lock_until or tm.MIN,
        ),
    )
    cols = columns_of(a)
    return {c: cols[c] for c in _LOCK}


def _loosens(row: Mapping[str, Any], claims: Sequence[tm.Assertion], now: datetime) -> bool:
    """Would this lock loosen the one in force (a weaker mode, or a sooner end)?"""
    held = _lock_in_force(claims, now)
    if not held["lock_mode"]:
        return False
    mine, theirs = str(row.get("lock_mode") or ""), str(held["lock_mode"])
    if _STRICTNESS.get(mine, 0) < _STRICTNESS.get(theirs, 0):
        return True
    until, held_until = tm.utc(row.get("lock_until")), tm.utc(held["lock_until"])
    return (
        held_until is None
        and until is not None
        or (until is not None and held_until is not None and until < held_until)
    )


def _assess_fields(
    row: Mapping[str, Any], fields: frozenset[str], served: Sequence[tm.Assertion], now: datetime
) -> tm.Amendment | None:
    """The assessment a metadata write makes: its journaled columns, set on the
    claims of the content its row shows (the current claim and that text's history:
    an attestation or a sensitivity binds the text). Another text's claims keep
    their own. None when it changes none of them."""
    sha = str(row.get("content_sha256") or "")
    claims = [a for a in served if a.content_sha256 == sha]
    if not claims:
        return None
    new = assertion_from_row(row, WriteIntent())
    changes = {f: getattr(new, f) for f in ASSESS_FIELDS if f in fields}
    am = assess_claims(claims, changes, str(row.get("memory_key") or ""))
    return None if am.empty else am


def _locked_against_removal(served: Sequence[tm.Assertion], head: Mapping[str, Any] | None) -> bool:
    return locked_against(served, "delete", datetime.now(UTC))


def locked_against(served: Sequence[tm.Assertion], action: str, now: datetime) -> bool:
    """A lock that refuses `action` is in force on what holds now, as the journal
    keeps it (mode and deadline); a lock on an ended claim binds nothing."""
    from server.memory.domain.lock import REFUSES

    return any(
        action in REFUSES.get(a.lock_mode, ()) and (a.lock_until is None or a.lock_until > now)
        for a in tm.current_claims(served, now)
    )


def _restated(
    row: Mapping[str, Any], served: Sequence[tm.Assertion], now: datetime, intent: WriteIntent
) -> tm.Assertion | None:
    """The current claim a restatement restates: its content and metadata, under its
    own qualifiers when it names them."""
    value, sha = str(row.get("value") or ""), str(row.get("content_sha256") or "")
    mine = assertion_from_row(row, WriteIntent())
    same = [
        a
        for a in tm.current_claims(served, now)
        if (a.content_sha256 == sha or _same_value(a.value, value)) and _same_metadata(a, mine)
    ]
    under = [a for a in same if a.qualifiers == dict(intent.qualifiers)]
    picked = under or same
    return picked[0] if picked else None


def _restates_now(row: Mapping[str, Any], served: Sequence[tm.Assertion], now: datetime) -> bool:
    """A restatement's row is written only when its value is what applies now."""
    return _shows_current(tm.Amendment("restate"), row, served, now)


def _current_after(
    amendment: tm.Amendment, row: Mapping[str, Any], served: Sequence[tm.Assertion], now: datetime
) -> bool:
    """Is the row's claim the key's current state after the change? A removal, a
    correction or a transition writes its row; an assertion or assessment only
    when it leaves its row's claim current (else the row is left out: late
    evidence, a plan)."""
    if amendment.change_kind not in ("assert", "assess") or removes(row):
        return True
    return _shows_current(amendment, row, served, now)


def _shows_current(
    amendment: tm.Amendment, row: Mapping[str, Any], served: Sequence[tm.Assertion], now: datetime
) -> bool:
    """The claim the row shows after the change: its own when that is current, else
    a current one of the row's value. The row takes that claim's validity as the
    journal holds it (an inferred start stays in the journal). False when no
    current claim carries the row's value."""
    value, sha = str(row.get("value") or ""), str(row.get("content_sha256") or "")
    after = list(tm.apply({a.assertion_id: a for a in served}, amendment).values())
    current = tm.current_claims(after, now)
    own = row_assertion(amendment, value)

    def carries(a: tm.Assertion) -> bool:  # the row's content, by its stored hash
        return a.content_sha256 == sha or _same_value(a.value, value)

    # The row shows its own claim; an assessment the current claim it revised (same
    # content); a restatement the claim it restates (equal but for validity). Any
    # other row whose own claim is not current is left out, never shown with
    # metadata the journal does not hold.
    shown: tm.Assertion | None = None
    if own in current and amendment.change_kind != "assess":
        shown = own
    elif amendment.change_kind == "assess":
        revised = [a for a in current if a in amendment.added and carries(a)] or [
            a for a in current if a in amendment.added
        ]
        # The revision of the claim the row was built from (its validity is the row's).
        span = (tm.utc(row.get("valid_at")), tm.utc(row.get("valid_to")))
        shown = next((a for a in revised if (tm.row_start(a), a.valid_to) == span), None) or next(
            iter(revised), None
        )
    elif amendment.change_kind == "restate":
        # The row carries its own metadata: only a current claim that agrees with it.
        mine = assertion_from_row(row, WriteIntent())
        shown = next((a for a in current if carries(a) and _same_metadata(a, mine)), None)
    if shown is None:
        return False
    r = cast(dict[str, Any], row)
    r["valid_at"], r["valid_to"] = tm.row_start(shown), shown.valid_to
    if r["valid_at"] is None:  # an inferred start stays in the journal, anchor and all
        r["temporal_anchor"] = None
    return True


def _is_current(after: Sequence[tm.Assertion], own: tm.Assertion, now: datetime) -> bool:
    return own in tm.current_claims(after, now)


def _retry_past_only(
    amendment: tm.Amendment | None, held: Sequence[tm.Assertion], now: datetime
) -> bool:
    """A retry's commit is in `held` already: is its own claim late evidence (it does
    not hold now by its own interval, and is not the current state)? A later commit
    of the same request may have revised it since; that does not make it past."""
    own = row_assertion(amendment)
    if amendment is None or amendment.change_kind != "assert" or own is None:
        return False
    return not own.valid_at(now) and not _is_current(held, own, now)


def _no_commit(ns: str, ident: Ident, at: datetime, why: str) -> Receipt:
    log.debug("temporal: %s/%r left out (%s)", ns, ident, why)
    return Receipt(ns, ident, "", 0, at, "", left_out=why)


async def serving(org: str, ns: str, keys: Sequence[Ident]) -> set[Ident]:
    """The identities among `keys` whose journal still serves a claim (now or planned)."""
    if not keys:
        return set()
    ch = _default_client()
    seq, _, _ = await store.head(org, ns, ch=ch)
    served = await _served(org, ns, keys, seq, await erased(org, ns), ch)
    return {ident for ident, claims in served.items() if claims}


async def _above_landed(b: _Batch, ch: _CHClient) -> None:
    """Raise the batch's row versions above any loom.memory row of its keys that
    landed since the plan read its floor (a projection, which moves no sequence), so
    the merge ranks the batch's rows as the journal orders them: after it."""
    if not any(p.row.get("version") and not p.past_only for p in b.planned):
        return
    newest = dict(await store.row_versions(b.org, b.ns, b.keys, ch=ch))
    # Per key, each version this batch drew and raised: a later row of the batch can
    # carry a claim an earlier one added. Versions already committed stay as they are.
    raised_from: dict[Ident, dict[int, int]] = {}
    for p in b.planned:
        drawn = int(p.row.get("version") or 0)
        if drawn and not p.past_only:
            if newest.get(p.ident, 0) >= drawn:
                raised_from.setdefault(p.ident, {})[drawn] = newest[p.ident] + 1
                cast(dict[str, Any], p.row)["version"] = drawn = newest[p.ident] + 1
            newest[p.ident] = max(newest.get(p.ident, 0), drawn)
        moved = raised_from.get(p.ident)
        if moved and any(a.source_version in moved for a in p.amendment.added):
            p.amendment = replace(
                p.amendment,
                added=tuple(
                    replace(a, source_version=moved.get(a.source_version, a.source_version))
                    for a in p.amendment.added
                ),
            )
            p.payload_sha = p.amendment.payload_sha256()


async def _touched(b: _Batch, head: int, ch: _CHClient) -> bool:
    """Did a commit after the snapshot change one of the batch's keys?"""
    rows = await store.commits_between(
        b.org, b.ns, b.seq, head, memory_keys=sorted({k[3] for k in b.keys}), ch=ch
    )
    wanted = set(b.keys)
    return any(ident_of(c) in wanted for c in rows)


def _stamp(b: _Batch, seq: int, last_at: datetime, now: datetime) -> None:
    """Give each planned change the next sequence and a recording time that rises
    with it, and build its commit and projection rows."""
    seen = _request_commits()
    # Assertions this batch adds, as stamped: a later change of the batch that
    # retires one carries its real birth, not the plan's placeholder.
    born: dict[str, Served] = {}
    for p in b.planned:
        seq += 1
        last_at = _next_time(last_at, now)
        commit = _commit_row(b, p, seq, last_at)
        b.commits.append(commit)
        for old in p.retired:
            old = born.get(old.a.assertion_id, old)
            b.projection.append(
                projection_row(b.org, b.ns, b.scope, p.ident, old, seq, retired=(seq, last_at))
            )
        for a in p.amendment.added:
            s = Served(a, p.memory_id, seq, last_at)
            born[a.assertion_id] = s
            b.projection.append(projection_row(b.org, b.ns, b.scope, p.ident, s, seq))
        b.parents[p.ident] = commit["commit_id"]
        if p.seed:
            continue
        if b.idem:
            seen.add((b.ns, p.ident))
        b.receipts.append(
            Receipt(
                b.ns,
                p.ident,
                commit["commit_id"],
                seq,
                last_at,
                p.amendment.change_kind,
                p.amendment,
                past_only=p.past_only,
                row=p.row,
            )
        )


def _request_commits() -> set[tuple[str, Ident]]:
    """The keys this request already committed under its client retry key."""
    bag = request_work.current()
    return bag.temporal_commits if bag is not None else set()


async def _retried_commits(b: _Batch, ch: _CHClient) -> dict[Ident, list[dict[str, Any]]]:
    """Per key, the commits an earlier attempt under this request's client key made,
    in order: the batch's n-th change of a key is answered by the n-th. A key this
    request already committed in an earlier publication is not a retry."""
    if not b.idem:
        return {}
    seen = _request_commits()
    keys = [k for k in b.keys if (b.ns, k) not in seen]
    return await store.commits_for_idempotency_key(
        b.org, b.ns, b.idem, keys, erased=b.erased, ch=ch
    )


@asynccontextmanager
async def namespace_lock(org: str, ns: str) -> AsyncIterator[Callable[[], Awaitable[bool]]]:
    """Hold the namespace's publisher lock; yields a check that it is still held.
    Publication takes it, and so does anything that must see no commit land
    between a check and its change (a redaction). Without a Keeper, only a server
    process declared the single writer may publish: the CLI and any second process
    would allocate the same sequences."""
    name = f"memory_temporal/{org}\x1f{ns}"
    mode = settings.memory_temporal_publisher
    keeper = mode == "keeper" or (mode == "auto" and await _locks.has_keeper())
    # Process mode is one writer in all: never with sibling web processes.
    if not keeper and not (mode == "process" and _serving and settings.web_workers == 1):
        raise TemporalUnavailableError(
            "memory writes need a cross-process publisher: run with a Keeper, or declare "
            "LOOM_MEMORY_TEMPORAL_PUBLISHER=process on a single server process "
            "(LOOM_WEB_WORKERS=1, no separate workers container) that writes memory",
            retry_after_s=None,
        )
    async with AsyncExitStack() as stack:
        # Only taking the lock maps to "busy": a refusal from inside the section
        # (an erasure fence, a stale revision) keeps its own error.
        try:
            await stack.enter_async_context(_locks.hold_name(name, keeper_required=keeper))
        except PreconditionFailedError as e:
            raise TemporalUnavailableError(
                f"namespace {ns!r} publisher is busy; retry", retry_after_s=1
            ) from e
        except BackendUnavailableError as e:
            raise TemporalUnavailableError(f"namespace {ns!r} publisher lock unavailable") from e

        if not keeper and await store.foreign_publisher(org, ns, PUBLISHER_ID, await _started()):
            # Process mode means one writer in all; another one has committed here.
            raise TemporalUnavailableError(
                f"namespace {ns!r}: another process publishes memory without a Keeper; "
                "run one server process, or a Keeper"
            )

        async def still_held() -> bool:
            return not keeper or await _locks.still_held(name)

        yield still_held


_serving = False
# When this process started publishing, on ClickHouse's clock (the one commits are
# stamped with): a commit by another publisher since then means a second writer in
# process mode.
_STARTED: datetime | None = None
# When this process started, on its own clock (`serve`); `_started` moves it onto
# ClickHouse's once, so siblings started together see each other's commits.
_BOOTED = datetime.now(UTC)


async def _started() -> datetime:
    global _STARTED
    if _STARTED is None:
        _STARTED = _BOOTED + (await store.server_now() - datetime.now(UTC))
    return _STARTED


def serve() -> None:
    """Mark this process as the server: the one that may publish without a Keeper."""
    global _serving, _BOOTED
    _serving = True
    _BOOTED = datetime.now(UTC)


async def _served(
    org: str, ns: str, keys: Sequence[Ident], head_seq: int, gone: store.Erased, ch: _CHClient
) -> dict[Ident, dict[str, Served]]:
    """What each key serves at the head. From the projection when it is current;
    otherwise by reducing those keys' journal from the start."""
    out: dict[Ident, dict[str, Served]] = {}
    if not keys or head_seq == 0:
        return out
    if await store.watermark(org, ns, ch=ch) >= head_seq:
        for r in await store.served_for_keys(org, ns, keys, erased=gone, ch=ch):
            ident = ident_of(r)
            s = served_from_row(r)
            out.setdefault(ident, {})[s.a.assertion_id] = s
        return out
    wanted = set(keys)
    rows = await store.commits_between(
        org, ns, 0, head_seq, memory_keys=sorted({k[3] for k in keys}), erased=gone, ch=ch
    )
    for c in rows:
        ident = ident_of(c)
        if ident not in wanted:
            continue
        cur = out.setdefault(ident, {})
        for aid in c["retired_assertion_ids"]:
            cur.pop(str(aid), None)
        for d in json.loads(c["assertions"]):
            a = tm.Assertion.from_json(d)
            cur[a.assertion_id] = Served(
                a,
                str(c["memory_id"]),
                int(c["commit_seq"]),
                tm.utc(c["recorded_at"]) or c["recorded_at"],
            )
    return out


async def _erasure_fence(
    org: str,
    ns: str,
    changes: Sequence[_RowChange],
    ch: _CHClient,
    clock: datetime | None = None,
) -> int:
    """The erasure generation of this scope, checked under the namespace lock. A
    write is refused while an erasure of its namespace or users is queued or
    running, or when one was requested after the write began (its rows were read
    before it). A request and a worker pass each bind a work bag at their start; a
    caller without one is fenced from now."""
    users = sorted({c.ident[1] for c in changes})
    since = await _began(ch, clock)
    total, late = await fence.scope_erasures(org, ns, users, since - timedelta(seconds=1), ch=ch)
    if late:
        raise PreconditionFailedError(
            "an erasure of this namespace or user is running or began after this write; "
            "re-read and retry"
        )
    return total


async def _insert_commits(org: str, ns: str, commits: list[dict[str, Any]], ch: _CHClient) -> None:
    """Insert, and on an uncertain result reconcile by commit id and retry the same
    rows. An outcome still unknown fails the write; its sequences stay allocated, so
    if the insert lands later it lands on them, never on a successor's. Then read
    the batch's sequences back: one another publisher also wrote (a lease lost
    mid-insert) withdraws this batch's rows, which no caller was told of."""
    ids = [c["commit_id"] for c in commits]
    want = {c["commit_id"]: c["payload_sha256"] for c in commits}
    for attempt in range(settings.memory_temporal_insert_retries + 1):
        try:
            await store.insert_commits(commits, ch=ch)
            break
        except Exception as e:  # noqa: BLE001 — an insert error may still have landed
            err = e
        try:
            landed = {
                str(r["commit_id"]): str(r["payload_sha256"])
                for r in await store.commits_by_id(org, ns, ids, ch=ch)
            }
        except Exception as e:
            raise TemporalUnavailableError(
                "temporal journal insert outcome unknown; retry with the same Idempotency-Key"
            ) from e
        if all(landed.get(i) == want[i] for i in ids):
            break
        if landed:
            raise TemporalUnavailableError("temporal journal insert landed partially")
        await asyncio.sleep(0.05 * (attempt + 1))
    else:
        raise TemporalUnavailableError(f"temporal journal insert failed: {err}")
    seqs = [c["commit_seq"] for c in commits]
    others = await store.other_commits_at(org, ns, seqs, ids, ch=ch)
    if others:
        await _withdraw(org, ns, ids, ch)
        raise TemporalUnavailableError(
            f"namespace {ns!r}: another publisher wrote sequence {min(others)}; retry"
        )
    if await store.landed_late(org, ns, ids, ch=ch):
        # Not part of the journal (`store.SETTLE_S`), whether or not this delete
        # succeeds; the caller is told so.
        await _withdraw(org, ns, ids, ch)
        raise TemporalUnavailableError(f"namespace {ns!r}: the journal insert landed late; retry")


async def _refence(
    org: str, ns: str, changes: Sequence[_RowChange], batch: _Batch, ch: _CHClient
) -> None:
    """The fence again, once the commits are in: an erasure requested while they
    were being inserted may already have run. The batch withdraws; should that
    delete fail, reads still never serve it (it was recorded before the erasure
    was requested, `store.Erased`)."""
    try:
        await _erasure_fence(org, ns, changes, ch)
    except PreconditionFailedError:
        await _withdraw(org, ns, [c["commit_id"] for c in batch.commits], ch)
        raise


async def _withdraw(org: str, ns: str, ids: list[str], ch: _CHClient) -> None:
    """Delete this batch's commits. Should the delete fail, the sequence holds two
    payloads until the next write or projection pass repairs it."""
    try:
        await store.withdraw_commits(org, ns, ids, ch=ch)
    except Exception as e:  # noqa: BLE001 — the race is reported either way; a repair follows
        log.warning("temporal withdraw of %s/%s failed: %s", org, ns, e)


async def _project(
    org: str, ns: str, rows: list[dict[str, Any]], seq: int, *, first_seq: int, ch: _CHClient
) -> None:
    """Apply the commits to the assertion projection. A failure leaves the
    watermark behind; readers reduce the tail and the projection worker repairs it.
    It never revokes the committed belief."""
    try:
        previous = await store.watermark(org, ns, ch=ch)
        await store.insert_assertions(rows, ch=ch)
        if previous >= first_seq - 1:
            await store.advance_watermark(org, ns, seq, ch=ch)
    except Exception as e:  # noqa: BLE001 — the journal is committed; the worker catches up
        degradations.note("memory_temporal", "projection_behind", e)
        log.warning("temporal projection: %s/%s behind at %d: %s", org, ns, seq, e)


def _next_time(last: datetime, now: datetime) -> datetime:
    """The recording time of the next commit: now (ClickHouse's clock), or just
    after the last one when the clock has not moved past it (commits of one batch
    stamped in the same microsecond; `publish` refuses a clock behind the journal)."""
    return now if now > last else last + timedelta(microseconds=1)


def _commit_row(b: _Batch, p: _Planned, seq: int, at: datetime) -> dict[str, Any]:
    agent, uid, sid, key = p.ident
    return {
        "org": b.org,
        "namespace": b.ns,
        "scope": b.scope,
        "agent": agent,
        "user_id": uid,
        "session_id": sid,
        "memory_key": key,
        "memory_id": p.memory_id,
        "commit_seq": seq,
        "commit_id": str(uuid.uuid4()),
        "parent_commit_id": b.parents.get(p.ident),
        "publisher_id": PUBLISHER_ID,
        "recorded_at": at,
        "request_id": request_id_var.get() or "",
        "idempotency_key": "" if p.seed else b.idem,
        "request_sha256": b.request_sha if b.idem and not p.seed else "",
        "request_slot": slot_of(p.row),
        "operation": "memory.journal_seed" if p.seed else b.operation[:64],
        "change_kind": p.amendment.change_kind,
        "payload_schema": tm.PAYLOAD_SCHEMA,
        "payload_sha256": p.payload_sha,
        "actor": str(p.row.get("actor") or ""),
        "source_class": str(p.row.get("source_class") or ""),
        "source_resource_id": str(p.row.get("source_resource_id") or ""),
        "erasure_generation": b.generation,
        "retired_assertion_ids": list(p.amendment.retired),
        "assertions": json.dumps([a.to_json() for a in p.amendment.added], default=str),
        # Kept while any assertion it added or retired is: a retraction outlives
        # the assertion it retracts, so the journal never revives it.
        "retain_until": max(
            (a.expires_at for a in (*p.amendment.added, *(r.a for r in p.retired)) if a.expires_at),
            default=None,
        ),
        # The loom.memory version its row carries (an amendment's row comes later;
        # late evidence writes none, so it names none).
        "row_version": 0 if p.past_only else int(p.row.get("version") or 0),
        "restated_assertion_id": p.restated or None,
    }


def _receipt(ns: str, ident: Ident, prior: Mapping[str, Any], *, moved_on: bool) -> Receipt:
    """A retry's receipt: the change the earlier attempt committed, read back."""
    amendment = tm.Amendment(
        str(prior["change_kind"]),
        tuple(str(x) for x in prior["retired_assertion_ids"]),
        tuple(tm.Assertion.from_json(d) for d in json.loads(prior["assertions"])),
    )
    return Receipt(
        ns,
        ident,
        str(prior["commit_id"]),
        int(prior["commit_seq"]),
        tm.utc(prior["recorded_at"]) or prior["recorded_at"],
        amendment.change_kind,
        amendment,
        moved_on,
        str(prior.get("memory_id") or ""),
        row_version=int(prior.get("row_version") or 0),
        request_slot=int(prior.get("request_slot", -1)),
    )
