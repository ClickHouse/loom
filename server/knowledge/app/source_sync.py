"""Common worker, publication and accounting for selected MCP copies and recordings.

Each source turn holds its own lease and rechecks current registration authority
before publication. MCP imports use the fixed selected set and protected mirror
engine. Recording sources retain their transcript and decision-extraction path.
Errors and incomplete work never authorize deletion; explicit writer erasure
requests retain the existing retirement pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import functools
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeGuard

from server.code.app.index import repo_registry
from server.code.app.index.pass_budget import PassBudget, rotate_to
from server.code.domain.index.remote import repo_tag
from server.code.domain.index.repos import (
    configured_repos,
    docs_namespaces,
    require_registry_read,
)
from server.core import leases
from server.core.ch import sequential_reads
from server.core.errors import OverloadedError
from server.core.pg_lease import PublicationPending
from server.knowledge.api.models_knowledge import KnowledgeDeleteInput, KnowledgeUpsertInput
from server.knowledge.app import private_sync, writes
from server.knowledge.app import sources as source_ops
from server.knowledge.domain import access_list, convert
from server.knowledge.domain.sources import (
    NAMESPACE_ACCESS,
    SYNC_WORKER_ACTOR,
    KnowledgeSource,
    home,
    json_object,
    mirror_synced,
    served,
    source_key,
    source_tag,
    turn_lease_name,
)
from server.knowledge.store import documents
from server.knowledge.store import sources as store
from server.llm import customer
from server.memory.domain.provenance import pipeline_actor
from server.observability import audit
from server.settings import settings
from server.workers import pass_context
from server.workers.holds import Held, hold_seconds
from server.workers.status import set_worker_identity

if TYPE_CHECKING:
    from server.memory.app.decision_extract import ExtractReport

log = logging.getLogger("loom.knowledge.source_sync")

WORKER = "knowledge_source_sync"
# The share of the pass deadline after which no new source is started.
_BUDGET_FRACTION = 0.75  # not a setting: the shape every cursor-resumed worker uses
# The mirrored sources' share of the time a pass starts work in: every listing of theirs
# reads every file's list, so they are bounded apart from the namespace sources.
_MIRROR_SHARE = 1 / 3  # not a setting: the design's split of a pass
# A source's lease outlives a stalled renewal by this much; it is renewed while held.
_LEASE_TTL_S = 120.0  # not a setting: the entry-lease shape
# Passes one file's failure that may pass (a 5xx, a timeout, its extractor call, its
# write) holds the turn before the file counts as unconverted: about 45 minutes at
# the default interval, past a restart or a redeploy of the service it needs, and
# short enough that one file cannot hold a source's stamp for good.
_WAIT_PASSES = 3  # not a setting: a bound on how long one file may stall a stamp
# How far behind the listing's start the stamp stays: a file the store stamps just
# before the listing but indexes just after it is listed again next turn.
_STAMP_SKEW_S = 60.0  # not a setting: a margin on the store's own indexing delay
# How long before the pass's deadline a document's model reads stop, so its writes,
# their audit rows and the turn's state write finish inside the pass.
_MINE_MARGIN_S = 30.0  # not a setting: time for the writes after the last read
# A document is not started with less read time than this left: the try would be spent
# on a run cut short. The pass counts as spent instead.
_MIN_MINE_S = 60.0  # not a setting: a small document's read and support check
# A document whose reads run out with at least this share of a pass's read time will not
# finish in any pass: the sync before the leg rarely takes more than a quarter of the
# read time.
_FULL_READ_SHARE = 0.75  # not a setting: a bound on how much of a pass one document may need

# Namespaces whose model calls are held, each until its hold ends: the extraction leg
# leaves their documents pending.
_HELD: Held[tuple[str, str]] = Held()


class SourceGone(Exception):
    """The source was removed or replaced during its turn: the turn stops."""


# Why a turn stopped short: they lead its error text, and the dashboard reads a note that
# starts with one as "Incomplete" (frontend/src/lib/knowledgeSources.ts, CUT_SHORT).
class TurnMark(StrEnum):
    PASS_BOUND = "pass bound reached"
    INCOMPLETE_SEARCH = "listing: incomplete search"
    ITEM_LOST = "listing: an item listed nothing"
    INCOMPLETE_RESULT = "listing: incomplete result"


@dataclass
class TurnResult:
    """What one turn did to one source, as its sync state records it."""

    status: str = "ok"
    documents_seen: int = 0
    changed: int = 0
    removed: int = 0
    unconverted: int = 0
    skipped: int = 0
    error: str = ""
    # Why the turn stopped short (the pass bound, an incomplete listing, a lost item),
    # each once; they lead the error text, so the dashboard tells a cut turn from a note.
    marks: list[TurnMark] = field(default_factory=list)
    # What else a turn that did not fail noted: an item or file it could not read. With
    # the marks and the held-back count, shown as the error when there is none.
    note: str = ""
    # New documents the turn may still write; `stored_copies_with_room` sets it before any
    # file, so a turn that never calls it writes nothing new.
    room: int = 0
    # LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS as this turn read it.
    bound: int = 0
    # New files held back past the room.
    held: int = 0
    # Why files were unconverted: reason -> count.
    reasons: dict[str, int] = field(default_factory=dict)
    # Files waiting on a failure that may pass: id -> [revision, passes, reason].
    waits: dict[str, list[Any]] = field(default_factory=dict)
    # The turn read the stored waits; one that never did leaves them as stored.
    waits_read: bool = False
    # The sync request whose full sync reset the waits, so it resets them once.
    waits_reset_for: str = ""
    # The full listing a periodic retry of given-up files was granted under, so the
    # retry is granted again only after a full listing completed.
    retry_granted_for: str | None = None
    # Every listed file was handled, so the stamp may move.
    complete: bool = True
    # The newest revision the listing returned, capped at the listing's start.
    newest: str = ""
    full: bool = False
    # A kind's own bookkeeping, written into the sync state as it is.
    kind_state: dict[str, Any] = field(default_factory=dict)
    # A mirrored source's mirror, once its turn knows it: the state the turn derived
    # from the mirror's documents is written there (`_write_state`).
    mirror: str = ""

    def fail(self, status: str, error: str) -> None:
        """The turn ends `status` with `error`, as a pair: the first plain failure
        stays, and a turn-wide status (a rate limit, a refused credential) replaces it."""
        self.complete = False
        if not self.error or status != "failed":
            self.status, self.error = status, error

    def mark(self, why: TurnMark) -> None:
        """Record why the turn stopped short, once: it keeps its stamp and retires
        nothing more. The pass bound goes first."""
        self.complete = False
        if why in self.marks:
            return
        if why is TurnMark.PASS_BOUND:
            self.marks.insert(0, why)
        else:
            self.marks.append(why)

    @property
    def cut(self) -> bool:
        """The pass bound stopped the turn before every file was handled."""
        return TurnMark.PASS_BOUND in self.marks

    @property
    def state_error(self) -> str:
        """The marks, then the held-back count and the notes, cut to 200 characters."""
        held = f"too_many_documents: {self.held} new files past {self.bound}" if self.held else ""
        return "; ".join([*self.marks, *filter(None, [held, self.note])])[:200]

    def unconverted_for(self, reason: str) -> None:
        self.unconverted += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


@dataclass
class PassResult:
    """Public pass summary; mirrored turns contribute codes, never document figures."""

    private_work: bool = False
    sources: int = 0
    synced: int = 0
    changed: int = 0
    removed: int = 0
    unconverted: int = 0
    skipped: int = 0
    # Documents of removed sources retired at an admin's request.
    retired: int = 0
    # Documents the extraction leg mined, rows it tagged source-changed, documents it
    # gave up on, and sources whose pending documents it left for a later pass.
    extracted: int = 0
    flagged_pinned: int = 0
    unextracted: int = 0
    extract_deferred: int = 0
    # Sources whose turn ended with a status other than ok or failed outright, plus one
    # for each document's failed extraction try.
    failed: int = 0
    # The pass bound stopped the pass before every source, or every file of one, was
    # handled.
    cut: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class Pass:
    """What every turn of one pass shares: the time and document budget, and per
    uploaded format whether an extractor serves it (None: its service could not be
    read this pass)."""

    budget: PassBudget
    left: int
    formats: dict[str, bool | None] = field(default_factory=dict)

    def spent(self) -> bool:
        return self.left <= 0 or self.budget.exhausted()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _cursor_of(src: KnowledgeSource) -> str:
    return source_key(src.org, src.namespace, src.name)


def _budget() -> PassBudget:
    started = time.monotonic()
    seconds = float(settings.knowledge_source_sync_max_pass_seconds) * _BUDGET_FRACTION
    return PassBudget(started, started + seconds)


async def require_source_registry() -> None:
    await repo_registry.refresh_db_repos()
    require_registry_read()


async def run_sync(
    *, org: str = "", namespace: str = "", only: str = "", full: bool = False
) -> PassResult:
    """One pass over every active source, or over one namespace's (`org`,
    `namespace`, and `only` one source by name). `full` records a full sync request
    for each (`source_ops.request_full_sync`), as `sync_now(full=true)` does."""
    result = PassResult()
    run = Pass(budget=_budget(), left=int(settings.knowledge_source_sync_documents_per_pass))
    found = await store.all_active_sources()
    if only:
        selected = await store.get_source(org, namespace, only)
        if selected is not None and mirror_synced(selected):
            found.append(selected)
    if org:
        found = [s for s in found if (s.org, s.namespace) == (org, namespace)]
    if only:
        found = [s for s in found if s.name == only]
    # An admin's delete request goes first, before the backlog of changed files and
    # before anything that reads the code registry.
    await _retire_removed_sources(run, result, org=org, namespace=namespace, only=only)
    if (
        not org
        and settings.auth_actually_required()
        and settings.knowledge_mirrored_sources_enabled
    ):
        from server.knowledge.app.mcp_source_worker import run as run_mcp

        try:
            await run_mcp(run.budget.phase(1 - _MIRROR_SHARE), result, full=full)
        except Exception as e:  # noqa: BLE001 — failed MCP discovery must not stop recording work
            result.private_work = True
            result.failed += 1
            result.errors.append("MCP_SCHEDULER_UNAVAILABLE")
            pass_context.note_failure(RuntimeError("MCP_SCHEDULER_UNAVAILABLE"), "")
            log.warning("knowledge_source_sync: MCP admission failed: %s", type(e).__name__)
    if found:
        # The repository tags come from the registry: refresh it once per pass, and
        # stop the pass (RegistryUnread) while it has never been read.
        await require_source_registry()
    usable = await _usable(found)
    if full:
        for src in usable:
            await source_ops.request_full_sync(src)
    states = await _states(usable)
    mirrored = [s for s in usable if mirror_synced(s)]
    if mirrored:
        # Until a third of the pass's start window; the namespace sources then have the
        # rest of the pass's own budget, and their document bound is their own.
        await _sync_mirrored(mirrored, states, run.budget, result)
    usable = [s for s in usable if not mirror_synced(s)]
    asked = sorted(
        (s for s in usable if _requested(states, s)),
        key=lambda s: _requested(states, s),
    )
    rest = [s for s in usable if not _requested(states, s)]
    order = asked + rotate_to(rest, pass_context.resume_cursor(), key=_cursor_of)
    result.sources += len(order) + len(mirrored)
    for i, src in enumerate(order):
        if run.spent():
            pass_context.set_cursor(_cursor_of(src))
            result.cut = True
            break
        # A pass cancelled inside this source resumes at the next one.
        nxt = order[i + 1] if i + 1 < len(order) else (rest or order)[0]
        pass_context.set_cursor(_cursor_of(nxt))
        turn = await _sync_one(src, states.get(_cursor_of(src), {}), run, result)
        if turn is not None and turn.cut and not turn.documents_seen and i > len(asked):
            # Out of time before any file, on what earlier sources left: the next pass
            # starts here. The first of the rest moves on instead, so one slow source never
            # holds the others back; a requested source goes first anyway.
            pass_context.set_cursor(_cursor_of(src))
            break
    await _extract_pending(order, run, result)
    if not order and not mirrored and not result.retired and not result.private_work:
        pass_context.mark_idle()
    return result


async def _sync_mirrored(
    mirrored: list[KnowledgeSource],
    states: dict[str, dict[str, str]],
    whole: PassBudget,
    result: PassResult,
) -> None:
    """The mirrored sources start work in the first quarter of the pass under their own
    document bound, the one synced longest ago first, so every source gets its turn
    however many there are. A sync request goes in that rotation
    too: a source whose turns keep ending incomplete (one with more changed files than
    the bound) keeps its request, and going first on it would starve the rest."""
    run = Pass(
        budget=whole.phase(1 - _MIRROR_SHARE),
        left=int(settings.knowledge_source_mirror_documents_per_pass),
    )

    def synced_at(src: KnowledgeSource) -> str:
        detail = json_object(states.get(_cursor_of(src), {}).get("detail", ""))
        return str(detail.get("last_synced_at") or "")

    for src in sorted(mirrored, key=synced_at):
        if run.spent():
            return
        await _sync_one(src, states.get(_cursor_of(src), {}), run, result)


async def _usable(found: list[KnowledgeSource]) -> list[KnowledgeSource]:
    """The sources a pass syncs: those whose namespace is active, of the sources this
    build serves and the mirrored ones its worker writes, the latter only while auth is
    on."""
    # With auth off there is no reader a mirror's lists could be checked against.
    mirrors_on = settings.auth_actually_required()
    syncable = [s for s in found if served(s) or (mirror_synced(s) and mirrors_on)]
    syncable = [s for s in syncable if s.kind != "mcp" or _mcp_syncable(s)]
    inactive = await store.inactive_namespaces(sorted({(s.org, s.namespace) for s in syncable}))
    return [s for s in syncable if (s.org, s.namespace) not in inactive]


def _mcp_syncable(src: KnowledgeSource) -> bool:
    from server.knowledge.app.mcp_source_authority import MCPAuthorityError, source_contract

    try:
        return source_contract(src).mode != "live"
    except (MCPAuthorityError, ValueError, TypeError):
        return False


async def _states(found: list[KnowledgeSource]) -> dict[str, dict[str, str]]:
    """Each source's stored sync state, by cursor."""
    out: dict[str, dict[str, str]] = {}
    for org, ns in sorted({(s.org, s.namespace) for s in found}):
        names = [s.name for s in found if (s.org, s.namespace) == (org, ns)]
        for name, state in (await store.read_states(org, ns, names)).items():
            out[source_key(org, ns, name)] = state
    return out


def _requested(states: dict[str, dict[str, str]], src: KnowledgeSource) -> str:
    detail = json_object(states.get(_cursor_of(src), {}).get("detail", ""))
    return str(detail.get("requested_at") or "")


@contextlib.asynccontextmanager
async def _source_turn(src: KnowledgeSource) -> AsyncIterator[leases.Lease]:
    """The source's lease (LeaseBusy when another copy holds it), its tenant for
    the embedding calls, and the worker's actor for every write."""
    pass_context.set_namespace(src.namespace)
    set_worker_identity(WORKER, org=src.org, namespace=src.namespace)
    async with leases.cluster_lease(
        turn_lease_name(src.org, src.namespace, src.name), ttl_s=_LEASE_TTL_S
    ) as lease:
        with customer.for_org(src.org, namespace=src.namespace), pipeline_actor(SYNC_WORKER_ACTOR):
            yield lease


async def _sync_one(
    src: KnowledgeSource, state: dict[str, str], run: Pass, result: PassResult
) -> TurnResult | None:
    if not mirror_synced(src):
        return await _sync_turn(src, state, run, result)
    result.private_work = True
    with private_sync.scope():
        return await _sync_turn(src, state, run, result)


async def _sync_turn(
    src: KnowledgeSource, state: dict[str, str], run: Pass, result: PassResult
) -> TurnResult | None:
    try:
        async with _source_turn(src):
            try:
                if src.credential_mode == "connected":
                    await _connected_current(src)
                turn = await _turn_of(src)(src, state, run)
                if src.credential_mode == "connected":
                    await _connected_current(src)
            except SourceGone:
                log.info("knowledge_source_sync: %s was removed during its turn", _cursor_of(src))
                return None
            except Exception as e:  # noqa: BLE001 — recorded as the source's failed turn
                if not mirror_synced(src):
                    log.warning("knowledge_source_sync: %s failed: %s", _cursor_of(src), e)
                turn = TurnResult()
                turn.fail("failed", type(e).__name__)
            await _write_state(src, state, turn)
    except leases.LeaseBusy:
        log.info("knowledge_source_sync: %s is being synced by another copy", _cursor_of(src))
        return None
    except Exception as e:  # noqa: BLE001 — one source's failure must not stop the others
        if mirror_synced(src):
            _mirror_failure(src, result)
            return None
        result.errors.append(f"{_cursor_of(src)}: {type(e).__name__}")
        result.failed += 1
        log.warning("knowledge_source_sync: %s failed: %s", _cursor_of(src), e)
        pass_context.note_failure(e, _cursor_of(src))
        return None
    if mirror_synced(src):
        if turn.status != "ok":
            _mirror_failure(src, result)
        return turn
    result.synced += 1
    result.changed += turn.changed
    result.removed += turn.removed
    result.unconverted += turn.unconverted
    result.skipped += turn.skipped
    result.cut |= turn.cut
    if turn.status != "ok" and not (
        turn.status == "provider_removed"
        and src.kind == "recording"
        and src.config.get("provider") == "meet"
    ):
        result.failed += 1
        result.errors.append(f"{_cursor_of(src)}: {turn.status} {turn.error}".strip())
        pass_context.note_failure(RuntimeError(turn.error or turn.status), _cursor_of(src))
    return turn


async def _connected_current(src: KnowledgeSource) -> None:
    await require_current(src)


def _mirror_failure(src: KnowledgeSource, result: PassResult) -> None:
    """Report the same code for every cause without exporting turn details."""
    result.errors.append(f"{_cursor_of(src)}: SYNC_FAILED")
    pass_context.note_failure(RuntimeError("SYNC_FAILED"), _cursor_of(src))


def _turn_of(src: KnowledgeSource) -> Callable[..., Awaitable[TurnResult]]:
    if src.kind == "mcp" and mirror_synced(src):
        from server.knowledge.app import source_sync_mcp

        return source_sync_mcp.turn
    if src.kind == "recording" and served(src):
        from server.knowledge.app import source_sync_recording

        return source_sync_recording.turn
    raise SourceGone(_cursor_of(src))


def full_due(detail: dict[str, Any]) -> bool:
    last = str(detail.get("full_listed_at") or "")
    try:
        listed = datetime.fromisoformat(last)
    except ValueError:
        return True
    age = (datetime.now(UTC) - listed).total_seconds()
    return age >= int(settings.knowledge_source_sync_full_interval_seconds)


async def stored_copies_with_room(
    src: KnowledgeSource, turn: TurnResult, *, pictures: bool = False
) -> dict[str, dict[str, str]]:
    """The source's searchable stored copies by file id (`store.source_revisions`), and
    the turn's room for new documents: LOOM_KNOWLEDGE_SOURCE_MAX_DOCUMENTS less what it
    holds, so past the bound a turn still updates and retires but writes no new file."""
    revisions = await store.source_revisions(src.org, *home(src), pictures=pictures)
    return index_with_room(turn, revisions)


def index_with_room(turn: TurnResult, revisions: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """The stored copies `revisions` by file id, and the turn's room for new documents
    (`stored_copies_with_room`)."""
    stored = {d["file_id"]: d for d in revisions}
    turn.bound = settings.knowledge_source_max_documents
    turn.room = max(0, turn.bound - len(stored))
    return stored


def start_waits(turn: TurnResult, detail: dict[str, Any]) -> None:
    """The turn's file waits, from the stored ones. A full sync request
    (`sync_now(full=true)`, `loom knowledge sync --full`) retries every waiting
    file, once per request. A periodic full listing gives each given-up
    file one more try, so a failure gives it up again at once, and grants it once
    per completed full listing: a full turn that keeps ending incomplete does not
    fetch the given-up files every pass."""
    stored = detail.get("file_waits")
    waits = dict(stored) if isinstance(stored, dict) else {}
    requested = str(detail.get("requested_at") or "")
    listed = str(detail.get("full_listed_at") or "")
    if detail.get("full") and requested != detail.get("waits_reset_for"):
        waits = {}
        turn.waits_reset_for = requested
        # The reset is this full listing's retry: the request's later turns grant none.
        turn.retry_granted_for = listed
    elif turn.full and detail.get("retry_granted_for") != listed:
        turn.retry_granted_for = listed
        for file_id, entry in waits.items():
            if _spent(entry):
                waits[file_id] = [entry[0], _WAIT_PASSES - 1, entry[2]]
    turn.waits = waits
    turn.waits_read = True


def capped(newest: str, listed_at: datetime) -> str:
    """`newest`, but never later than the listing's start less the skew margin: a
    file stamped in the store's future, or indexed just after the listing, is
    listed again rather than skipped by the next incremental turn."""
    if not newest:
        return ""
    cap = listed_at - timedelta(seconds=_STAMP_SKEW_S)
    try:
        seen = datetime.fromisoformat(newest)
    except ValueError:
        return ""
    if seen <= cap:
        return newest
    return stamp_of(cap)


def stamp_of(at: datetime) -> str:
    """`at` (UTC) as the stores write their stamps: RFC 3339 with milliseconds."""
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def valid_stamp(stamp: str) -> bool:
    """Whether a stored stamp is one a store reported or `stamp_of` wrote: RFC 3339,
    UTC, the one form every store's stamps take (the folder client's check)."""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z", stamp))


def client_error(e: BaseException) -> str:
    """The client's own message: what failed and the HTTP status, never a body."""
    return str(e)[:200]


def _spent(entry: Any) -> TypeGuard[list[Any]]:
    """Whether a wait entry ([revision, passes, reason]) waited all its passes."""
    return isinstance(entry, list) and len(entry) == 3 and int(entry[1]) >= _WAIT_PASSES


def _waiting(turn: TurnResult, f: dict[str, Any]) -> list[Any] | None:
    """The file's wait entry, dropped when its revision changed since."""
    file_id = str(f["id"])
    entry = turn.waits.get(file_id)
    if not isinstance(entry, list) or len(entry) != 3:
        turn.waits.pop(file_id, None)
        return None
    if entry[0] != str(f.get("modifiedTime") or ""):
        turn.waits.pop(file_id)
        return None
    return entry


async def sync_item(
    src: KnowledgeSource,
    f: dict[str, Any],
    uri: str,
    prior: dict[str, str] | None,
    run: Pass,
    turn: TurnResult,
    fetch_and_write: Callable[[], Awaitable[str]],
    before_fetch: Callable[[], Awaitable[bool]] | None = None,
    *,
    refetch: bool = False,
) -> bool:
    """Handle one listed item (`f`: its `id` and its revision as `modifiedTime`) at
    address `uri`; False when the turn must stop listing. In order: an item with no
    https address is unconverted; one whose stored copy has this revision and address
    is skipped; one given up on this revision is unconverted; a new one past the
    source's bound is held back (`stored_copies_with_room`), with no read; one at an
    address another writer holds is skipped; `before_fetch` (a kind's own checks) may
    settle it; then it costs one of the pass bound and `fetch_and_write` runs, answering
    `written`, `unchanged` (fetched again and nothing to write), `skipped`, `waiting`,
    `given_up` or `stop`; only a new item `written` takes room under the bound. A
    written item that moved to a new address has its old one retired. With `refetch`,
    a stored copy at this revision is fetched again all the same (a kind's own
    conversion changed, a page read too soon after its edit)."""
    file_id = str(f["id"])
    revision = str(f.get("modifiedTime") or "")
    if not uri.startswith("https://"):
        turn.unconverted_for("no_address")
        return True
    if (
        not refetch
        and prior
        and prior["source_revision"] == revision
        and prior["source_uri"] == uri
    ):
        turn.waits.pop(file_id, None)
        return True  # unchanged: no fetch, no embedding
    entry = _waiting(turn, f)
    if _spent(entry):
        # It waited its passes on this revision: unconverted until it changes.
        turn.unconverted_for(str(entry[2]))
        return True
    if prior is None and turn.room <= 0:
        turn.held += 1  # past the source's bound: it waits for room, at no read
        return True
    # Before any fetch: an address another writer holds costs nothing.
    if not await _may_write(src, uri):
        turn.waits.pop(file_id, None)
        turn.skipped += 1
        return True
    if before_fetch is not None and not await before_fetch():
        return True
    if run.spent():
        turn.mark(TurnMark.PASS_BOUND)
        return False
    run.left -= 1
    outcome = await fetch_and_write()
    if outcome == "stop":
        return False
    if outcome not in ("waiting", "given_up"):
        turn.waits.pop(file_id, None)
    if outcome == "written" and prior is None:
        turn.room -= 1
    if outcome == "written" and prior and prior["source_uri"] != uri:
        if await retire(src, prior["source_uri"]):
            turn.removed += 1
    return True


def _given_up(turn: TurnResult, f: dict[str, Any]) -> bool:
    """Whether the file waited its passes on its listed revision and was given up on."""
    entry = turn.waits.get(str(f["id"]))
    return _spent(entry) and entry[0] == str(f.get("modifiedTime") or "")


def give_up(f: dict[str, Any], turn: TurnResult, reason: str) -> str:
    """A file refused for good: unconverted now, and recorded as given up on its
    revision, so later passes skip it without a fetch or any of the bound until it
    changes or `start_waits` grants a retry."""
    turn.waits[str(f["id"])] = [str(f.get("modifiedTime") or ""), _WAIT_PASSES, reason]
    turn.unconverted_for(reason)
    return "given_up"


def wait(f: dict[str, Any], turn: TurnResult, reason: str, error: str) -> str:
    """One more pass this file waited on its revision; unconverted once it waited
    `_WAIT_PASSES`, else the turn stays incomplete. Always `waiting`: the entry
    stays once exhausted too, so a given-up file is tried again only when it
    changes or `start_waits` grants a retry."""
    file_id, revision = str(f["id"]), str(f.get("modifiedTime") or "")
    entry = _waiting(turn, f)
    passes = int(entry[1]) + 1 if entry is not None else 1
    turn.waits[file_id] = [revision, passes, reason]
    if passes >= _WAIT_PASSES:
        turn.unconverted_for(reason)
    else:
        turn.fail("failed", error[:200])
    return "waiting"


def counted(turn: TurnResult, written: bool) -> str:
    """The outcome of a write step: `written`, or `skipped` for an address another writer
    took during the fetch (counted as the check before the fetch counts it)."""
    if not written:
        turn.skipped += 1
        return "skipped"
    turn.changed += 1
    return "written"


async def _may_write(src: KnowledgeSource, uri: str) -> bool:
    """Whether the live document at `uri`, if any, is this source's: a document
    another writer or another source holds is never overwritten or retired."""
    namespace, _ = home(src)
    doc = await documents.get_one(src.org, namespace, uri, include_superseded=False)
    return doc is None or ours(src, doc)


def ours(src: KnowledgeSource, doc: documents.DocumentRow) -> bool:
    """Whether `doc` is one this source's sync wrote."""
    _, tag = home(src)
    return doc.actor == SYNC_WORKER_ACTOR and tag in doc.tags


async def require_current(src: KnowledgeSource) -> None:
    """SourceGone unless the source is still active as the registration this turn
    started with: a remove during the turn stops it before its next write."""
    if src.kind == "mcp":
        from server.knowledge.app.mcp_source_authority import validate_source

        await validate_source(src)
        return
    with sequential_reads():
        fresh = await store.get_source(src.org, src.namespace, src.name)
    if fresh is None or not fresh.active or fresh.source_id != src.source_id:
        raise SourceGone(_cursor_of(src))
    if not served(src):
        raise SourceGone(_cursor_of(src))


@contextlib.asynccontextmanager
async def publication_guard(src: KnowledgeSource) -> AsyncIterator[None]:
    if src.kind == "mcp":
        from server.knowledge.app import mcp_source_authority

        async with mcp_source_authority.publication_guard(src):
            yield
    else:
        yield


def _repo_tag(src: KnowledgeSource) -> str:
    """`repo:<id>` for a source on a repository's trunk code namespace, else ""."""
    entries = configured_repos()  # one snapshot for both lookups
    cfg = next((c for c in entries if (c.org, c.namespace) == (src.org, src.namespace)), None)
    if cfg is None or docs_namespaces(entries).get((cfg.org, cfg.repo)) != src.namespace:
        return ""
    return repo_tag(cfg.git_remote or cfg.repo)


async def write_document(
    src: KnowledgeSource,
    f: dict[str, Any],
    uri: str,
    content: str,
    *,
    ingested_format: str,
    original: bytes = b"",
    extra_metadata: dict[str, Any] | None = None,
    permissions: access_list.AccessList = access_list.UNREAD,
) -> bool:
    """Write one item (`f`: its `id`, `name`, `modifiedTime` and, with an original,
    its `mimeType`) at `uri` in the source's home (`domain.home`) with
    `knowledge.upsert`, as the sync worker: tagged `source-kind:design_doc`,
    `source:<name>`, in a mirror `source-id:<id>` and, on a repository's trunk code
    namespace, its `repo:<id>`, class `imported`, with the source's kind and name, the
    item's id and revision and the conversion in its metadata, and the permission
    list its turn read (`unread` outside a mirror). False, writing nothing, when
    another writer took the address while the item was fetched: the check before the
    fetch is seconds old by now."""
    namespace, tag = home(src)
    if src.credential_mode == "connected" and src.kind == "notion":
        permissions = access_list.AccessList(owner=src.owner_subject)
    tags = ["source-kind:design_doc", source_tag(src.name)]
    if not served(src):
        tags.append(tag)  # its `source-id:<id>`, and no repository tag
    elif repo := _repo_tag(src):
        tags.insert(1, repo)
    metadata: dict[str, Any] = {
        "source_kind": src.kind,
        "source_name": src.name,
        "source_revision": str(f.get("modifiedTime") or ""),
        "ingested_from": str(f["id"]),
        "ingested_format": ingested_format,
        **(extra_metadata or {}),
    }
    with sequential_reads():  # a replica behind the person's write would say free
        free = await _may_write(src, uri)
    if not free:
        return False
    pass_context.check_stop()  # a stopped pass or a lost lease writes nothing
    request = KnowledgeUpsertInput(
        org=src.org,
        namespace=namespace,
        source_uri=uri,
        content=content,
        title=str(f.get("name") or ""),
        description=convert.description_of(content),
        tags=tags,
        metadata=metadata,
        original_bytes_b64=base64.b64encode(original).decode() if original else None,
        original_filename=str(f.get("name") or "") if original else "",
        blob_content_type=str(f.get("mimeType") or "") if original else "",
        source_class="imported",
    )
    guard = (
        functools.partial(publication_guard, src) if src.credential_mode == "connected" else None
    )
    if src.kind == "mcp":
        from server.knowledge.app.mcp_source_authority import publication_guard as mcp_guard

        guard = functools.partial(mcp_guard, src)
    with contextlib.suppress(PublicationPending):  # committed; the worker publishes it
        await writes.handle_knowledge_upsert(
            request,
            permissions=permissions,
            commit_guard=guard,
        )
    return True


async def retire(src: KnowledgeSource, uri: str) -> bool:
    """Retire one of the source's documents, with the audit row the transport would
    have written for `knowledge.delete` (none in a mirror: the audit log outlives an
    erasure); False when it is no longer the source's."""
    namespace, _ = home(src)
    with sequential_reads():  # a replica behind a person's write would say still ours
        still_ours = await _may_write(src, uri)
    if not still_ours:
        return False
    pass_context.check_stop()
    try:
        out = await writes.handle_knowledge_delete(
            KnowledgeDeleteInput(org=src.org, namespace=namespace, source_uri=uri)
        )
        deleted = out.deleted
    except PublicationPending:  # the delete is committed; the worker publishes it
        deleted = True
    if deleted and src.access == NAMESPACE_ACCESS:
        await audit.emit(
            "knowledge.delete",
            org=src.org,
            namespace=namespace,
            actor=SYNC_WORKER_ACTOR,
            subject={"source_uri": uri, "source_name": src.name},
        )
    return deleted


async def _write_state(src: KnowledgeSource, read: dict[str, str], turn: TurnResult) -> None:
    """Record the turn in the source's sync state, under the namespace's sources
    lease like every other write of it. Nothing is written for a source removed or
    replaced during the turn; a sync request made during it stays. A mirrored
    source's namespace row keeps only its request, status and time: the rest is
    derived from the mirror's documents, so it goes under the mirror (`mirror_state`)."""
    async with source_ops.sources_lease(src.org, src.namespace):
        with sequential_reads():
            fresh = await store.get_source(src.org, src.namespace, src.name)
            states = await store.read_states(src.org, src.namespace, [src.name])
            mirror_row = await mirror_state(src, turn.mirror) if turn.mirror else {}
        if (
            fresh is None
            or fresh.source_id != src.source_id
            or (src.kind == "mcp" and replace(fresh, mirror=src.mirror) != src)
        ):
            return
        current = states.get(src.name) or {}
        if mirror_synced(src):
            await _write_mirrored_state(src, read, turn, current, mirror_row)
            return
        detail = json_object(current.get("detail", ""))
        asked = json_object(read.get("detail", "")).get("requested_at")
        now = _now_iso()
        _record_turn(detail, turn, now)
        stamp = current.get("state_value", "")
        if turn.complete:
            # A change made after this listing is stamped later than anything it saw.
            stamp = max(turn.newest, stamp if valid_stamp(stamp) else "")
            if turn.full:
                detail["full_listed_at"] = now
            if detail.get("requested_at") == asked:
                detail.pop("requested_at", None)
                detail.pop("full", None)
        await store.write_state(src.org, src.namespace, src.name, state_value=stamp, detail=detail)


def _record_turn(detail: dict[str, Any], turn: TurnResult, now: str) -> None:
    """What a turn did, into the sync state `detail` it is written with."""
    detail.update(
        last_synced_at=now,
        status=turn.status,
        documents_seen=turn.documents_seen,
        changed=turn.changed,
        removed=turn.removed,
        unconverted=turn.unconverted,
        unconverted_reasons=turn.reasons,
        skipped=turn.skipped,
        error=turn.error or turn.state_error,
    )
    detail.update(turn.kind_state)
    if turn.waits_read:
        detail["file_waits"] = turn.waits
        if turn.waits_reset_for:
            detail["waits_reset_for"] = turn.waits_reset_for
        if turn.retry_granted_for is not None:
            detail["retry_granted_for"] = turn.retry_granted_for


async def mirror_state(src: KnowledgeSource, mirror: str) -> dict[str, str]:
    """A mirrored source's own sync state: under its mirror and id, so a source added
    again under the name starts afresh and the namespace's erasure takes it with the
    mirror."""
    return (await store.read_states(src.org, mirror, [src.source_id])).get(src.source_id) or {}


async def _write_mirrored_state(
    src: KnowledgeSource,
    read: dict[str, str],
    turn: TurnResult,
    current: dict[str, str],
    mirror_row: dict[str, str],
) -> None:
    """`_write_state` for a mirrored source: codes in the namespace row, the rest under
    the mirror. A turn that failed before it knew its mirror, or that raised (its
    result is then a fresh one), writes the codes only: the mirror row keeps what the
    last turn that returned recorded."""
    now = _now_iso()
    stored = json_object(current.get("detail", ""))
    shown: dict[str, Any] = {
        k: stored[k] for k in ("requested_at", "full", "mcp_selection") if k in stored
    }
    asked = json_object(read.get("detail", "")).get("requested_at")
    shown.update(last_synced_at=now, status=turn.status)
    if turn.complete and shown.get("requested_at") == asked:
        shown.pop("requested_at", None)
        shown.pop("full", None)
    if turn.mirror:
        detail = json_object(mirror_row.get("detail", ""))
        _record_turn(detail, turn, now)
        if turn.complete and turn.full:
            detail["full_listed_at"] = now
        await store.write_state(src.org, turn.mirror, src.source_id, state_value="", detail=detail)
    await store.write_state(
        src.org, src.namespace, src.name, state_value=current.get("state_value", ""), detail=shown
    )


async def _retire_removed_sources(
    run: Pass, result: PassResult, *, org: str, namespace: str, only: str
) -> None:
    """Retire the documents of removed sources whose admin asked for it, then
    clear the request once none is left."""
    for src in await store.pending_delete_sources():
        if org and (src.org, src.namespace) != (org, namespace):
            continue
        if only and src.name != only:
            continue
        if src.access != NAMESPACE_ACCESS:
            continue  # protected copies use the existing mirror retirement queue
        if run.spent():
            result.cut = True
            return
        try:
            async with _source_turn(src):
                await _retire_all(src, run, result)
        except leases.LeaseBusy:
            continue
        except Exception as e:  # noqa: BLE001 — one source's failure must not stop the others
            result.errors.append(f"{_cursor_of(src)}: {type(e).__name__}")
            result.failed += 1
            log.warning("knowledge_source_sync: retiring %s failed: %s", _cursor_of(src), e)
            pass_context.note_failure(e, _cursor_of(src))


async def _retire_all(src: KnowledgeSource, run: Pass, result: PassResult) -> None:
    docs = await store.source_documents(src.org, *home(src))
    for doc in docs:
        if run.spent():
            result.cut = True
            return
        run.left -= 1
        if await retire(src, str(doc["source_uri"])):
            result.retired += 1
    with sequential_reads():
        if await store.count_documents(src.org, *home(src)):
            return
    try:
        async with source_ops.sources_lease(src.org, src.namespace):
            with sequential_reads():
                latest = await store.latest_source(src.org, src.namespace, src.name)
            # Only the request this pass acted on: a source added again under the
            # name since is another registration.
            if latest is not None and latest.source_id == src.source_id and latest.pending_delete:
                await store.insert_source(dataclasses.replace(latest, pending_delete=False))
    except OverloadedError:
        return  # the next pass clears it


def _mined_of(detail: dict[str, Any]) -> dict[str, str]:
    """The revision each of a source's documents was last mined at: address -> revision."""
    stored = detail.get("mined")
    if not isinstance(stored, dict):
        return {}
    return {k: v for k, v in stored.items() if isinstance(v, str)}


def _extract_waits(detail: dict[str, Any]) -> dict[str, list[Any]]:
    """Documents whose extraction failed: address -> [revision, failed tries, reason]."""
    stored = detail.get("extract_waits")
    if not isinstance(stored, dict):
        return {}
    return {k: v for k, v in stored.items() if isinstance(v, list) and len(v) == 3}


def _mined_revision(doc: dict[str, str]) -> str:
    """The revision a document is mined at: its source revision, with the hash of its
    text when its metadata records one (`text_sha256`: a page store's page and a
    folder's native document), since such a copy can be written again at the same
    revision with other text. Every other document's key is the source revision
    alone."""
    text = doc.get("text_sha256") or ""
    return f"{doc['source_revision']}@{text}" if text else doc["source_revision"]


def _unmined(docs: list[dict[str, str]], detail: dict[str, Any]) -> list[dict[str, str]]:
    """The documents whose stored revision is not the one last mined: one whose last try
    ran out of read time first, so it gets the most of the next pass, then oldest first."""
    mined, waits = _mined_of(detail), _extract_waits(detail)

    def order(d: dict[str, str]) -> tuple[bool, str, str]:
        wait = waits.get(d["source_uri"]) or ["", 0, ""]
        late = wait[0] == _mined_revision(d) and wait[2] == "out_of_time"
        return not late, d["source_revision"], d["source_uri"]

    todo = [d for d in docs if mined.get(d["source_uri"]) != _mined_revision(d)]
    return sorted(todo, key=order)


@dataclass
class _Mined:
    """What the extraction leg did to one source's documents."""

    waits: dict[str, list[Any]]
    # Documents settled at a revision: mined, or given up on.
    settled: dict[str, str] = field(default_factory=dict)
    # A document's extraction was tried and came back (mined or failed), not held.
    tried: bool = False
    extracted: int = 0
    flagged: int = 0
    unextracted: int = 0
    failures: int = 0
    error: str = ""
    # Why the turn left documents for a later pass (`over_cap`, `held:<code>`,
    # `pass_bound`).
    deferred: str = ""
    # What stops the whole leg: `held:<code>` for a hold on every org's calls, or
    # `stopped` when the pass was stopped mid-write.
    stop: str = ""
    # The run stopped writing because the source's lease was lost: this source's turn
    # ends with nothing recorded, and the leg goes on to the next source.
    lease_lost: bool = False


async def _extract_pending(order: list[KnowledgeSource], run: Pass, result: PassResult) -> None:
    """The extraction leg: mine each source's unmined documents while the pass has
    budget left, and keep each source's `pending_extract` count and, when it waits,
    the reason current."""
    from server.memory.app.decision_extract import no_model_error

    # A mirrored document is shown only to the readers its list admits, and a decision
    # row to every reader of the namespace: nothing of a mirror is mined.
    order = [s for s in order if served(s)]
    with sequential_reads():
        # What this pass's turns just wrote.
        states = await _states(order)
        queues = []
        for src in order:
            detail = json_object(states.get(_cursor_of(src), {}).get("detail", ""))
            try:
                docs = await store.source_revisions(src.org, *home(src))
            except Exception as e:  # noqa: BLE001 — one source's failed read must not stop the others
                result.errors.append(f"{_cursor_of(src)}: extraction {type(e).__name__}")
                result.failed += 1
                pass_context.note_failure(e, _cursor_of(src))
                continue
            todo = _unmined(docs, detail)
            # A source with nothing to mine and no stale count to clear needs no write.
            if todo or detail.get("pending_extract"):
                queues.append((src, docs, todo))
    if not queues:
        return
    stop = ""
    if error := no_model_error():
        log.warning("knowledge_source_sync: no decisions mined: %s", error)
        stop = "no_model"
    off: frozenset[str] = frozenset()
    try:
        off = await customer.switched_off_orgs()
    except customer.SwitchUnread as e:
        # Fail closed: no org's documents are mined until the switch is read.
        log.info("knowledge_source_sync: no decisions mined: %s", e)
        stop = stop or "switch_unread"
    for src, docs, todo in queues:
        if stop == "stopped":
            return  # the pass was stopped: nothing more is written
        why = (stop if todo else "") or _not_now(src, todo, off, run)
        if why:
            if todo:
                result.extract_deferred += 1
            result.cut |= why == "pass_bound"
            try:
                await _write_mined(src, None, docs, pending=len(todo), waiting=why)
            except Exception as e:  # noqa: BLE001 — one source's failed write must not stop the others
                result.errors.append(f"{_cursor_of(src)}: extraction {type(e).__name__}")
                result.failed += 1
                log.warning("knowledge_source_sync: recording %s failed: %s", _cursor_of(src), e)
                pass_context.note_failure(e, _cursor_of(src))
            continue
        stop = await _extract_source(src, docs, run, result)


def _not_now(
    src: KnowledgeSource, todo: list[dict[str, str]], off: frozenset[str], run: Pass
) -> str:
    """Why the source's documents are not mined now; '' when they are."""
    if not todo:
        return "nothing"
    if src.org in off:
        return "switched_off"
    if _HELD.left((src.org, src.namespace)) > 0:
        return "held"
    if not _time_for_one(run):
        return "pass_bound"
    return ""


def _read_deadline(run: Pass) -> float:
    """When a document's model reads stop: shortly before the pass's deadline."""
    deadline = run.budget.started + float(settings.knowledge_source_sync_max_pass_seconds)
    return deadline - _MINE_MARGIN_S


def _full_read_s() -> float:
    """The read time a document has when the leg starts it early in a pass: one that
    runs out of it will not fit in any pass."""
    read_s = float(settings.knowledge_source_sync_max_pass_seconds) - _MINE_MARGIN_S
    return read_s * _FULL_READ_SHARE


def _time_for_one(run: Pass) -> bool:
    return not run.spent() and _read_deadline(run) - time.monotonic() >= _MIN_MINE_S


async def _over_cap(src: KnowledgeSource) -> bool:
    """Whether the leg's own spend in the namespace reached its share of the cap, or the
    whole cap is spent; a cap that cannot be read counts as reached. The calls finished
    so far are flushed first, so the read sees them."""
    from server.observability.budget import namespace_over_cap
    from server.observability.events import get_emitter

    await get_emitter().flush_now()
    return await namespace_over_cap(
        src.org,
        src.namespace,
        operation=f"worker.{WORKER}",
        share=float(settings.knowledge_source_sync_cap_share),
        fail_open=False,
    )


async def _extract_source(
    src: KnowledgeSource, docs: list[dict[str, str]], run: Pass, result: PassResult
) -> str:
    """Mine one source's unmined documents and record what was settled; what stops the
    leg (`_Mined.stop`), else ''. A pass stopped or cancelled records nothing: the
    documents it did not settle are unmined still."""
    mined = _Mined(waits={})
    try:
        async with _source_turn(src) as lease:
            with sequential_reads():
                states = await store.read_states(src.org, src.namespace, [src.name])
            detail = json_object((states.get(src.name) or {}).get("detail", ""))
            mined.waits = _extract_waits(detail)
            todo = _unmined(docs, detail)

            def may_write() -> bool:
                return lease.held and not pass_context.stop_requested()

            try:
                await _extract_all(src, todo, run, result, mined, may_write)
            except Exception:
                # What was settled before the failure stays settled.
                await _write_mined(src, mined, docs, pending=len(todo) - len(mined.settled))
                raise
            if mined.stop == "stopped":
                return mined.stop  # the pass was stopped: it writes nothing more
            if mined.lease_lost:
                return ""  # the lease is lost: the copy that holds it now records the turn
            await _write_mined(src, mined, docs, pending=len(todo) - len(mined.settled))
    except (leases.LeaseBusy, leases.LeaseLost, SourceGone):
        # Busy or lost: the copy holding the lease records the turn; a removed source has
        # none to record.
        return ""
    except Exception as e:  # noqa: BLE001 — one source's failure must not stop the others
        result.errors.append(f"{_cursor_of(src)}: extraction {type(e).__name__}")
        result.failed += 1
        log.warning("knowledge_source_sync: mining %s failed: %s", _cursor_of(src), e)
        pass_context.note_failure(e, _cursor_of(src))
    result.extracted += mined.extracted
    result.flagged_pinned += mined.flagged
    result.unextracted += mined.unextracted
    result.failed += mined.failures
    if mined.failures:
        result.errors.append(f"{_cursor_of(src)}: extraction {mined.error}")
        pass_context.note_failure(RuntimeError(mined.error), _cursor_of(src))
    if mined.deferred:
        result.extract_deferred += 1
    if mined.deferred.startswith("held:"):
        result.errors.append(f"{_cursor_of(src)}: extraction {mined.deferred}")
    return mined.stop


async def _extract_all(
    src: KnowledgeSource,
    todo: list[dict[str, str]],
    run: Pass,
    result: PassResult,
    mined: _Mined,
    may_write: Callable[[], bool],
) -> None:
    for doc in todo:
        if not _time_for_one(run):
            result.cut = True
            mined.deferred = "pass_bound"
            return
        if await _over_cap(src):
            mined.deferred = "over_cap"
            return
        if not await _extract_one(src, doc, run, mined, may_write):
            return


def _counts_as_try(report: ExtractReport) -> bool:
    """Whether the run failed in a way a later run may not: it could not run, a window
    or a retire step was refused or lost, or a write was refused for a reason of the
    moment. A refusal every run gets (a blacklisted decision, a pinned peer) is not:
    the document is settled with the rest of its decisions written."""
    transient = [d for d in report.dropped if d.refused and not d.permanent]
    return bool(report.error or report.refused or transient)


async def _extract_one(
    src: KnowledgeSource,
    doc: dict[str, str],
    run: Pass,
    mined: _Mined,
    may_write: Callable[[], bool],
) -> bool:
    """Mine one document; False when a hold or a lost lease stops the source's turn."""
    uri, revision = doc["source_uri"], _mined_revision(doc)
    await require_current(src)
    pass_context.check_stop()  # a stopped pass or a lost worker's lease writes nothing
    run.left -= 1
    read_s = _read_deadline(run) - time.monotonic()
    try:
        report = await asyncio.create_task(_mine(src, uri, _read_deadline(run), may_write))
    except Exception as e:  # noqa: BLE001 — one document's failure must not stop the others
        log.warning("knowledge_source_sync: mining %s failed: %s", uri, e)
        mined.tried = True
        _extract_failed(mined, uri, revision, f"failed:{type(e).__name__}")
        return True
    if report.held is not None or report.write_stopped:
        # A wait, not a try: the document stays unmined, what it wrote stays, and the
        # counts of the last turn that tried one stand.
        if report.write_stopped and pass_context.stop_requested():
            mined.stop = "stopped"
        elif report.write_stopped:
            mined.lease_lost = True
        elif isinstance(report.held, customer.GROUP_HOLDS):
            mined.deferred = f"held:{report.held.code}"
            _HELD.hold([(src.org, src.namespace)], hold_seconds(report.held))
        elif report.held is not None:
            mined.deferred = mined.stop = f"held:{report.held.code}"
        return False
    mined.tried = True
    if report.out_of_time and read_s >= _full_read_s():
        # It had all the read time a pass gives and did not finish: no pass will do.
        _give_up_mining(mined, uri, revision, "too_large_for_pass")
    elif _counts_as_try(report):
        log.warning(
            "knowledge_source_sync: mining %s: %s",
            uri,
            report.error or "; ".join(report.refused + [d.reason for d in report.dropped]),
        )
        reason = "out_of_time" if report.out_of_time else "not_run" if report.error else "refused"
        _extract_failed(mined, uri, revision, reason)
    else:
        mined.settled[uri] = revision
        mined.waits.pop(uri, None)
        mined.extracted += 1
        mined.flagged += len(report.flagged)
    return True


async def _mine(
    src: KnowledgeSource, uri: str, read_deadline: float, may_write: Callable[[], bool]
) -> ExtractReport:
    """`extract_document` on one document, its spend billed to the source's namespace,
    which the cap reads. Run as its own task, so the cost accumulator it binds leaves
    the pass's own (the sync's embedding spend) as it was."""
    from server.core.background import TaskSet
    from server.memory.app.decision_extract import extract_document, finish_writes
    from server.observability import cost_accumulator
    from server.workers.cost_event import emit_worker_cost_event_for

    started = time.monotonic()
    cost_accumulator.begin_op()
    writes = TaskSet()
    try:
        return await extract_document(
            src.org,
            src.namespace,
            uri,
            dry_run=False,
            read_deadline=read_deadline,
            may_write=may_write,
            writes=writes,
        )
    finally:
        try:
            # A write unit a cancel left running bills to this accumulator (its task
            # copied this context): wait for this run's, so their spend is in the row.
            await finish_writes(writes)
        finally:
            emit_worker_cost_event_for(
                org=src.org,
                namespace=src.namespace,
                operation=f"worker.{WORKER}",
                started=started,
            )


def _give_up_mining(mined: _Mined, uri: str, revision: str, reason: str) -> None:
    """The revision counts as settled though not mined: it is tried again only once the
    sync writes a new one."""
    mined.waits.pop(uri, None)
    mined.settled[uri] = revision
    mined.unextracted += 1
    mined.failures += 1
    mined.error = mined.error or reason


def _extract_failed(mined: _Mined, uri: str, revision: str, reason: str) -> None:
    """One more failed try on the document's revision; given up after `_WAIT_PASSES`."""
    entry = mined.waits.get(uri)
    tries = int(entry[1]) + 1 if entry and entry[0] == revision else 1
    if tries >= _WAIT_PASSES:
        _give_up_mining(mined, uri, revision, reason)
        return
    mined.waits[uri] = [revision, tries, reason]
    mined.failures += 1
    mined.error = mined.error or reason


async def _write_mined(
    src: KnowledgeSource,
    mined: _Mined | None,
    docs: list[dict[str, str]],
    *,
    pending: int,
    waiting: str = "",
) -> None:
    """Record the leg's turn in the source's sync state, under the namespace's sources
    lease: the revisions it settled, the waits, the pending count, the counts of a turn
    that tried a document, and why documents wait (`waiting`, or the turn's own reason)
    as `extract_error`. Nothing is written when none of it changed."""
    async with source_ops.sources_lease(src.org, src.namespace):
        with sequential_reads():
            fresh = await store.get_source(src.org, src.namespace, src.name)
            states = await store.read_states(src.org, src.namespace, [src.name])
        if fresh is None or fresh.source_id != src.source_id:
            return
        current = states.get(src.name) or {}
        detail = json_object(current.get("detail", ""))
        before = dict(detail)
        detail["pending_extract"] = pending
        if waiting == "nothing":
            detail["extract_error"] = ""  # nothing waits: no reason stands
        elif waiting:
            detail["extract_error"] = waiting
        if mined is not None:
            live = {d["source_uri"] for d in docs}
            # Documents no longer the source's are dropped, so the map stays its size.
            known = {u: r for u, r in {**_mined_of(detail), **mined.settled}.items() if u in live}
            detail["mined"] = known
            detail["extract_waits"] = {u: w for u, w in mined.waits.items() if u in live}
            if mined.tried:
                detail.update(
                    last_extracted_at=_now_iso(),
                    extracted=mined.extracted,
                    flagged_pinned=mined.flagged,
                    unextracted=mined.unextracted,
                    extract_error=mined.error,
                )
            if mined.deferred:
                detail["extract_error"] = mined.error or mined.deferred
        if detail == before:
            return
        await store.write_state(
            src.org,
            src.namespace,
            src.name,
            state_value=current.get("state_value", ""),
            detail=detail,
        )


__all__ = [
    "WORKER",
    "Pass",
    "PassResult",
    "SourceGone",
    "TurnResult",
    "capped",
    "client_error",
    "counted",
    "full_due",
    "give_up",
    "index_with_room",
    "mirror_state",
    "ours",
    "require_current",
    "retire",
    "run_sync",
    "stamp_of",
    "start_waits",
    "stored_copies_with_room",
    "sync_item",
    "valid_stamp",
    "wait",
    "write_document",
]
