"""Moving document originals from ClickHouse to the bucket (`loom knowledge
migrate-originals`): each original whose latest catalog row still holds its bytes,
and that the namespace still needs (`blobs.needed`; the rest are the sweep's), is
copied to an object, hash-verified and switched (`move_to_bucket`). A run is
idempotent and resumable: what it switched is no longer walked, and a run stopped
part way leaves every original readable from wherever its row says."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

from server.core import object_store as objects
from server.core.ch import sequential_reads, set_query_tag
from server.core.errors import ValidationError
from server.core.repo_base import _CHClient
from server.erasure.app import ledger_reads
from server.erasure.domain.scopes import may_cover, names_namespace
from server.knowledge.store import blobs
from server.settings import DOCUMENT_UPLOAD_BUDGET_S

log = logging.getLogger(__name__)

# Rows read per page of a namespace's walk, and namespaces per page of their listing;
# each original is moved one at a time.
_PAGE = 200  # not a setting: a page of a one-off operator walk


class CorruptOriginal(Exception):
    """An original's bytes do not hash to its sha256 or do not fill its size, in
    ClickHouse or read back from the bucket: nothing is switched."""


class ErasureOpen(Exception):
    """An erasure that covers the original has not finished, or ran while it was
    copied and kept it: it stays in ClickHouse. The message names the erasure."""


class SwitchUnconfirmed(Exception):
    """A switch that could not be confirmed: an erasure that started as the row landed
    has not finished, or a check or write around the switch failed. The message says
    what to check. `moved` is False when the switch may not stand: its row's insert
    failed (it may not have landed), or an erasure took the original and only taking
    the switch back failed."""

    def __init__(self, message: str, *, moved: bool = True) -> None:
        super().__init__(message)
        self.moved = moved


# How long a move waits for an erasure that started while it moved, and how often it
# reads the ledger meanwhile.
_ERASURE_WAIT_S = 600  # not a setting: a one-off operator command's patience
_ERASURE_POLL_S = 1.0  # not a setting: a one-off operator command's poll cadence


@dataclass(frozen=True)
class _Erasure:
    id: str
    scope_type: str
    scope_value: str
    status: str

    def deletes_namespace(self, namespace: str) -> bool:
        """Whether this erasure deletes every original of `namespace` for certain: its
        namespace's, or a list that names it. A scope it cannot read is not taken for
        one (it fences a move, but a take-back on it would be a guess)."""
        return names_namespace(self.scope_type, self.scope_value, namespace) is True

    @property
    def done(self) -> bool:
        return self.status == "completed"

    def __str__(self) -> str:
        return f"erasure {self.id} ({self.scope_type}, {self.status})"


def _fence_advice(erasures: list[_Erasure], what: str) -> str:
    """What holds a move back, named by the erasure that holds it longest (a failed one,
    else an unfinished one, else one that finished just now), and what to do."""
    failed = [e for e in erasures if e.status == "failed"]
    if failed:
        # The recovery worker retries it; one past its attempts needs a re-verify.
        return (
            f"{failed[0]} {what}; run again once the recovery worker has completed it, or "
            "complete it with loom erasure-re-verify --resume"
        )
    open_ = [e for e in erasures if not e.done]
    if open_:
        return f"{open_[0]} {what}; run again once it has finished"
    return f"{erasures[0]}, which finished just now, {what}; run again in a minute"


async def _erasures_since(org: str, namespace: str, since_s: int) -> list[_Erasure]:
    """The erasures that cover the namespace's originals and had not finished at
    `since_s`."""
    rows = await ledger_reads.unfinished_since(org, since_s, ledger_reads.LEDGER_CLOCK_MARGIN_S)
    return [
        _Erasure(str(r["erasure_id"]), str(r["scope_type"]), str(r["scope_value"]), str(r["state"]))
        for r in rows
        if may_cover(str(r["scope_type"]), str(r["scope_value"]), namespace)
    ]


async def _await_erasures(held: blobs.Held, since_s: int, ids: set[str]) -> None:
    """Wait until the erasures `ids` (those that raced the switch) have completed;
    `SwitchUnconfirmed` after _ERASURE_WAIT_S. One registered later reads the row."""
    deadline = time.monotonic() + _ERASURE_WAIT_S
    while True:
        raced = await _erasures_since(held.org, held.namespace, since_s)
        open_ = [e for e in raced if e.id in ids and not e.done]
        if not open_:
            return
        # A failed one is waited for too: the recovery worker retries it.
        if time.monotonic() >= deadline:
            raise SwitchUnconfirmed(
                f"{open_[0]} started as its row was switched and has not finished within "
                f"{_ERASURE_WAIT_S} s; the switched row and its object are left to it"
            )
        await asyncio.sleep(_ERASURE_POLL_S)


async def fence(org: str, namespace: str) -> str:
    """The refusal that holds every original of the namespace back now, naming the
    erasure that holds it longest; '' when none does. Coverage depends on the namespace
    alone, so one read serves a walk until an original is not switched cleanly."""
    fenced = await _erasures_since(org, namespace, await blobs.ch_now_s())
    return _fence_advice(fenced, "covers it") if fenced else ""


async def move_to_bucket(held: blobs.Held, *, ch: _CHClient | None = None) -> bool:
    """Copy one original from its ClickHouse row to a new object in the bucket and
    switch its catalog row to that object; every document naming the sha reads from
    the bucket from then on. The walk reads the namespace's erasure fence first
    (`fence`); the order of the move and how it meets each erasure, the sweep and the
    bucket walk are in docs/features/knowledge.md, "Where originals are stored".

    True when the switch stands. False when there is nothing to move or the move did
    not stand: the row went or was replaced since the walk read it, or an erasure took
    the original while it moved; its object is deleted, or left to the bucket walk when
    the bucket refuses the delete or a write outranked the switched row.
    `CorruptOriginal`: the bytes do not fill their size or hash to their sha256, in
    ClickHouse or read back. `ErasureOpen`: an erasure that overlapped the copy, the
    original still held in ClickHouse. `SwitchUnconfirmed`: the row switched (or may
    have) but could not be confirmed, against an erasure or against a later write that
    may outrank it. `ObjectStoreError`: the bucket refused, the new object was gone
    before its read-back, or the copy outlasted DOCUMENT_UPLOAD_BUDGET_S.
    `ValidationError`: its object key would be too long (the walk refuses such a
    namespace before calling this).

    `ch` is the client for the knowledge plane's own reads and writes; the erasure ledger
    is read through the erasure plane, on its client."""
    # This move's own start: an erasure that finished before it cannot race its copy, and
    # one still running then, or finished within the margin, is caught after the copy.
    since = await blobs.ch_now_s(ch=ch)
    # Before the original is read: a key too long for the bucket walk's cursor refuses the
    # move. The walk refuses such a namespace whole; this guards a direct call.
    loc = objects.new_location(held.org, held.namespace, held.sha)
    # Read as the checks after it are: a replica behind the walk's must not make a sound
    # original read as one whose bytes are missing.
    with sequential_reads():
        data = await blobs.read(
            held.org, held.namespace, held.sha, stored=blobs.Stored(held.size, ""), ch=ch
        )
    if data is None:
        # Corrupt only while the row the walk read is still the latest; any other was
        # written since, and keeps what its write left.
        if await blobs.latest(held, ch=ch) == blobs.Latest("", held.version_ms):
            raise CorruptOriginal("bytes in ClickHouse do not fill its size")
        return False
    if hashlib.sha256(data).hexdigest() != held.sha:
        raise CorruptOriginal("bytes in ClickHouse do not hash to its sha256")
    async with objects.transfer() as lease:
        started = time.monotonic()  # as an upload's: from the write, not the slot's wait
        await objects.put(loc, data, held.sha, lease)
    try:
        back = await objects.read(loc)
    except objects.ObjectStoreError:
        await _drop(loc)  # no row names it yet
        raise
    if back is not None and hashlib.sha256(back).hexdigest() != held.sha:
        await _drop(loc)
        raise CorruptOriginal("bytes read back from the bucket do not hash to its sha256")
    # The latest row read again just before the switch: an erasure or any write since the
    # walk keeps what it left (a later version outranks the switch).
    if await blobs.latest(held, ch=ch) != blobs.Latest("", held.version_ms):
        await _drop(loc)  # no row names it, nor will
        return False
    overtaken = await _erasures_since(held.org, held.namespace, since)
    if overtaken or back is None:
        # No row names the object yet. The original is the erasure's: gone, or kept,
        # and then still held here.
        await _drop(loc)
        after = await blobs.latest(held, ch=ch)
        if after is None or after.location:
            return False
        if overtaken:
            # Still held here: the erasure has not reached it yet, or kept it (a user's
            # erasure keeps one another writer still needs). Reported, not counted as gone.
            raise ErasureOpen(
                _fence_advice(overtaken, "overlapped its copy, and it stays in ClickHouse")
            )
        raise objects.ObjectStoreError(
            "object store read failed (its new object was gone before its read-back)",
            operation="read",
        )
    if time.monotonic() - started > DOCUMENT_UPLOAD_BUDGET_S:
        await _drop(loc)
        raise objects.ObjectStoreError(
            f"object store write took longer than {DOCUMENT_UPLOAD_BUDGET_S} s before its row",
            operation="write",
        )
    try:
        await blobs.switch_to(held, loc, ch=ch)
    except Exception as e:  # the row may have landed: a failed insert is reported, not lost
        raise SwitchUnconfirmed(
            f"writing the row switching it to {loc.uri()} failed ({e}); check whether that row "
            "landed, and if it did, that no erasure of its namespace or a user ran then",
            moved=False,
        ) from e
    return await _settle(held, since, loc, ch)


async def _drop(loc: objects.Location) -> None:
    """Delete an object of the move's own that no row names; when the bucket refuses, the
    bucket walk takes it once it is old enough, and the move's outcome stands."""
    try:
        await objects.delete(loc.bucket, [loc.key])
    except objects.ObjectStoreError as e:
        log.warning("originals: %s left to the bucket walk: %s", loc.uri(), e)


async def _stands(held: blobs.Held, loc: objects.Location, ch: _CHClient | None) -> bool:
    """Whether the switched row is still the original's latest. Outranked, its object is
    left to the bucket walk, which takes it once no row names it. When this read finds no
    row (the sweep took the original with the switched row), the move deletes its object
    now, as no row can name it again; a sweep delete landing after the read leaves the
    object to the bucket walk."""
    try:
        now = await blobs.latest(held, ch=ch)
    except Exception as e:  # the row is switched: an unread latest row is reported, not lost
        raise SwitchUnconfirmed(
            f"switched to {loc.uri()}, but reading whether a later write outranked it failed "
            f"({e}); check whether the latest loom.document_blobs row of this original names "
            f"{loc.uri()}"
        ) from e
    if now is None:
        await _drop(loc)
        return False
    return now.location == loc.uri()


async def _settle(
    held: blobs.Held, since: int, loc: objects.Location, ch: _CHClient | None
) -> bool:
    """After the switched row landed, an erasure registered as it landed decides:

    - none: the switch stands, unless a write since the re-read outranks it;
    - a namespace's: the move is taken back at once;
    - a user's: once it has finished, the move is taken back if the erasure took the
      moved object, its object deleted if the erasure took the row alone, and it stands
      if it kept both.

    A read or delete that fails here leaves the switched row, reported
    (`SwitchUnconfirmed`)."""
    try:
        raced = await _erasures_since(held.org, held.namespace, since)
        if not raced:
            # No erasure raced the switch: it stands, unless a write since the re-read
            # outranks it. Its object is then named only by a superseded row, and the
            # bucket walk takes it once the rows merge.
            return await _stands(held, loc, ch)
        # Taken back at once only for an erasure that certainly deletes the namespace's
        # originals; any other is waited for and decided by the moved object.
        taken = any(e.deletes_namespace(held.namespace) for e in raced)
        if not taken:
            # Logged before the wait: the row is switched, and a stop during the wait
            # would otherwise leave no line naming it.
            log.warning(
                "originals: %s/%s/%s switched to %s; waiting on %s",
                held.org,
                held.namespace,
                held.sha,
                loc.uri(),
                ", ".join(str(e) for e in raced),
            )
            await _await_erasures(held, since, {e.id for e in raced})
            # Listed, not read: whether it is there is all that decides.
            taken = not await objects.count_under(loc.bucket, loc.key)
        if not taken:
            if await blobs.row_names(held, loc, ch=ch):
                # The erasure kept the original; the switch stands as an unraced one does.
                return await _stands(held, loc, ch)
            # It took the row but not the object: a replica without the bucket set that
            # read the scope's rows before this one landed deletes rows only. The object
            # holds bytes nothing names, and goes now.
            await _drop(loc)
            return False
    except SwitchUnconfirmed:
        raise
    except Exception as e:  # the row is switched: an unread ledger or object is reported, not lost
        raise SwitchUnconfirmed(
            f"switched to {loc.uri()}, but the check for an erasure that raced it failed ({e}); "
            "check that no erasure of its namespace or a user ran then"
        ) from e
    try:
        await blobs.unwrite(held, loc, ch=ch)
    except Exception as e:  # the row is switched: a failed take-back is reported, not lost
        raise SwitchUnconfirmed(
            f"switched to {loc.uri()} as an erasure took it, but deleting that row failed ({e}); "
            f"delete the loom.document_blobs row of this original whose location is {loc.uri()}",
            moved=False,
        ) from e
    await _drop(loc)
    return False


@dataclass
class Migrated:
    moved: int = 0  # switched to an object in the bucket
    skipped: int = 0  # gone, replaced or erased since the walk read them
    # Nothing needs them now (no live document, unexpired upload or open ingestion): not
    # moved, and the sweep's once older than document_blob_min_age_seconds.
    unneeded: int = 0
    pending: int = 0  # a dry run's originals still held in ClickHouse
    bytes: int = 0  # of the originals moved, or with a dry run of those held
    errors: list[str] = field(default_factory=list)  # originals not moved, one line each
    # Namespaces refused for the rest of the walk (by an erasure, or object keys too long),
    # one line each.
    refused: list[str] = field(default_factory=list)
    # Switches that could not be confirmed: an erasure that raced it has not finished, a
    # check or the row's insert failed, or taking it back failed. Each says what to check.
    unconfirmed: list[str] = field(default_factory=list)


async def migrate_originals(
    *, org: str = "", namespace: str = "", dry_run: bool = False, out: Migrated | None = None
) -> Migrated:
    """Move every original held in ClickHouse that its namespace still needs (of
    `org`, or of its `namespace`) to the bucket, counting the others as left to the
    sweep; with `dry_run`, count them. An original that fails (bytes that do not hash
    to their sha or fill their size, a bucket that refuses) is reported and left where
    it is, and the walk goes on with the next. One not switched cleanly (gone or
    replaced, held back, or unconfirmed) makes the walk read the namespace's fence
    again: an erasure holding it refuses the rest of the namespace in one line. A
    ClickHouse error stops the run: the next run goes on."""
    set_query_tag("cli:knowledge-migrate-originals")
    # The caller's, when given: what a run stopped by an error gathered is still its own.
    out = out if out is not None else Migrated()
    for org_, ns in await _scopes(org, namespace):
        # One fence read per namespace, again after any original not switched cleanly; a
        # dry run checks the fence and the keys too, so it counts only what a run would move.
        refusal = await fence(org_, ns)
        if not refusal:
            try:
                objects.check_key_fits(org_, ns)
            except ValidationError as e:
                refusal = str(e)  # no original of the namespace can be moved
        after = ""
        while not refusal:
            page = await blobs.held_in_clickhouse(org_, ns, after, _PAGE)
            wanted = await blobs.needed(org_, ns, [h.sha for h in page.held])
            for held in page.held:
                if held.sha not in wanted:
                    out.unneeded += 1
                    continue
                if not await _one(held, dry_run, out):
                    refusal = await fence(org_, ns)
                    if refusal:
                        break
            if not page.after:
                break
            after = page.after
        if refusal:
            # The rest of the namespace is refused alike: once, not once per original.
            out.refused.append(
                f"{org_}/{ns}: {refusal}; the rest of its originals are left in ClickHouse"
            )
    return out


async def _scopes(org: str, namespace: str) -> list[tuple[str, str]]:
    """The (org, namespace) pairs to walk: the one named, or each holding originals."""
    if org and namespace:
        return [(org, namespace)]
    pairs: list[tuple[str, str]] = []
    after = ("", "")
    while True:
        page = await blobs.namespaces(after, _PAGE, org=org)
        pairs += page
        if len(page) < _PAGE:
            return pairs
        after = page[-1]


async def _one(held: blobs.Held, dry_run: bool, out: Migrated) -> bool:
    """Count one original. False when an erasure may explain its outcome (it held it
    back, raced its switch, or took it), so the walk reads the fence again; a corrupt or
    refused one is an error the fence does not explain."""
    if dry_run:
        out.pending += 1
        out.bytes += held.size
        return True
    try:
        if await move_to_bucket(held):
            out.moved += 1
            out.bytes += held.size
        else:
            # Gone, replaced, or erased: an erasure may be what took it.
            out.skipped += 1
            return False
    except SwitchUnconfirmed as e:
        # Listed as unconfirmed; counted as moved only when the switch stands.
        if e.moved:
            out.moved += 1
            out.bytes += held.size
        # Logged now too: a switched row is not walked again, so this line is its record.
        log.warning(
            "originals: %s/%s/%s switched, unconfirmed: %s", held.org, held.namespace, held.sha, e
        )
        out.unconfirmed.append(f"{held.org}/{held.namespace}/{held.sha}: {e}")
        return False
    except (
        CorruptOriginal,
        ErasureOpen,
        objects.ObjectStoreError,
    ) as e:
        log.warning("originals: %s/%s/%s not moved: %s", held.org, held.namespace, held.sha, e)
        out.errors.append(f"{held.org}/{held.namespace}/{held.sha}: {e}")
        # An erasure that held it back may hold the rest: the walk reads the fence again.
        return not isinstance(e, ErasureOpen)
    return True
