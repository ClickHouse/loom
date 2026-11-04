"""The sync of registered document sources into their repositories' documents:
one pass over every active source, run by the `knowledge_source_sync` worker and
by `loom knowledge sync`. A folder's turn is below; each other kind has its own
turn module (`source_sync_linear`, `source_sync_recording`), chosen by `_turn_of`,
which builds on the public helpers here: `Pass` and `TurnResult`, `start_waits`,
`full_due` and `capped` for a turn's start and stamp, `sync_item` for one listed
item, with `give_up`, `wait` and `require_current` for its fetch,
`write_document`, `retire_absent` and `retire`, and `client_error`.

A pass reads the active sources and drops those whose namespace is inactive or no
longer a repository's trunk. Sources a maintainer asked to sync (`sync_now`) go
first, the rest from where the previous pass stopped. Each source's turn holds
the source's own lease, so two copies of the worker never sync it at once.

A turn lists the source's files: every file on its first turn, when asked
(`full`) and once `knowledge_source_sync_full_interval_seconds` have passed
since the last full listing; otherwise those changed after the stored stamp. A
file whose stored copy has the listed revision is skipped without a fetch. A
changed file is fetched, turned into Markdown and written with
`knowledge.upsert` as `worker:knowledge-source-sync`, `imported`, tagged
`source:<name>`. A native document is exported as Markdown, a presentation
converted slide by slide, and a PDF or `.pptx` sent to the extractor service its
format is routed to (the server parses no original itself); the bytes are kept
as the document's original. A file that cannot be stored is counted
`unconverted` with its reason and not written. One the store or the extractor
refuses, one over the size bound and one whose write is refused are also given
up on their revision at once, so later passes skip them without a fetch; one
with no https link or a format no extractor declares costs no fetch and is
counted again each pass. A file whose fetch, extraction or write fails in a way
that may pass (a 5xx or a timeout on that file, its extractor call, the write)
waits: the turn stays incomplete for `_WAIT_PASSES` passes on the same revision,
then the file is given up on. A given-up file is tried again when it changes,
once after each completed full listing, and on a full sync request. What fails
for every file stays the turn's: the token exchange, the listing, a 401, a rate
limit, an extractor whose capabilities cannot be read.

Before each write the source is read again: a source removed or replaced during
its turn stops the turn, which records nothing.

A document is retired only on positive evidence that the source dropped it:
absence from a complete full listing whose folders the account can still see,
confirmed by a lookup of that file; an address that is no longer its file's; or a
maintainer's `remove(delete_documents=true)`. A fetch error, a rate limit or
a folder the account lost retires nothing. A document another writer or another
source holds is never written or retired.

The stamp moves only when every listed file was handled, so a cut turn is
listed again and its written files are skipped by revision.

After the sync, the extraction leg mines into decision rows in the same namespace
(`decision_extract.extract_document`, as `loom decisions extract` does) each of a
source's documents whose stored revision is not the one last mined for it. The
mined revision is kept per document in the source's sync state and written only
after that document's extraction settled, so a document a turn wrote is mined on
a later pass whatever stopped this one. The leg holds the source's lease and
bills its model calls to the namespace. A namespace whose own spend on this leg
reached `knowledge_source_sync_cap_share` of its daily cost cap (read before each
document; a cap that cannot be read counts as reached), an org whose model calls
are switched off and a namespace whose calls are held are left for a later pass.
A document whose extraction fails waits like a file: it is tried again for
`_WAIT_PASSES` passes on the same revision, then left until it is written again.
Its model reads stop shortly before the pass's deadline (a try, nothing written;
one that had a whole pass's read time is given up on at once). Each decision's
writes run whole with their audit row, and the run stops writing before the next
one once the pass is stopped or the source's lease is lost.

`knowledge_source_sync_documents_per_pass` bounds the files fetched, the
documents retired and the documents mined in one pass, across all sources.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import functools
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from server.clients import gdrive
from server.code.app.index.pass_budget import PassBudget, rotate_to
from server.code.domain.index.remote import repo_tag
from server.code.domain.index.repos import repo_for_namespace
from server.core import leases
from server.core.ch import sequential_reads
from server.core.errors import OverloadedError, ValidationError
from server.core.secrets_at_rest import unseal
from server.knowledge.api.models_knowledge import KnowledgeDeleteInput, KnowledgeUpsertInput
from server.knowledge.app import extractors, writes
from server.knowledge.app import sources as source_ops
from server.knowledge.domain import convert
from server.knowledge.domain.sources import (
    GDRIVE_MIME_TYPES,
    SYNC_WORKER_ACTOR,
    KnowledgeSource,
    json_object,
    source_tag,
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
_MIN_MINE_S = 60.0  # not a setting: a few windows' model calls
_FORMAT_OF_MIME = {
    GDRIVE_MIME_TYPES["pdf"]: "pdf",
    GDRIVE_MIME_TYPES["pptx"]: "pptx",
}
# Namespaces whose model calls are held, each until its hold ends: the extraction leg
# leaves their documents pending.
_HELD: Held[tuple[str, str]] = Held()


class SourceGone(Exception):
    """The source was removed or replaced during its turn: the turn stops."""


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
    # Why a turn that did not fail is still incomplete (the pass bound, an
    # incomplete listing); shown as the error when there is none.
    note: str = ""
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
    # The pass bound stopped the turn before every file was handled.
    cut: bool = False
    # The newest revision the listing returned, capped at the listing's start.
    newest: str = ""
    full: bool = False

    def fail(self, status: str, error: str) -> None:
        """The turn ends `status` with `error`, as a pair: the first plain failure
        stays, and a turn-wide status (a rate limit, a refused credential) replaces it."""
        self.complete = False
        if not self.error or status != "failed":
            self.status, self.error = status, error

    def stop_at_bound(self) -> None:
        self.complete, self.cut = False, True
        self.note = self.note or "pass bound reached; the next pass goes on"

    def unconverted_for(self, reason: str) -> None:
        self.unconverted += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


@dataclass
class PassResult:
    sources: int = 0
    synced: int = 0
    changed: int = 0
    removed: int = 0
    unconverted: int = 0
    skipped: int = 0
    # Documents of removed sources retired at a maintainer's request.
    retired: int = 0
    # Documents the extraction leg mined, rows it tagged source-changed, documents it
    # gave up on, and sources whose pending documents it left for a later pass.
    extracted: int = 0
    flagged_pinned: int = 0
    unextracted: int = 0
    extract_deferred: int = 0
    # Sources whose turn ended with a status other than ok, or failed outright, and
    # those a document's extraction failed on.
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
    return f"{src.org}/{src.namespace}/{src.name}"


def _budget() -> PassBudget:
    started = time.monotonic()
    seconds = float(settings.knowledge_source_sync_max_pass_seconds) * _BUDGET_FRACTION
    return PassBudget(started, started + seconds)


async def run_sync(
    *, org: str = "", namespace: str = "", only: str = "", full: bool = False
) -> PassResult:
    """One pass over every active source, or over one namespace's (`org`,
    `namespace`, and `only` one source by name). `full` records a full sync request
    for each (`source_ops.request_full_sync`), as `sync_now(full=true)` does."""
    result = PassResult()
    run = Pass(budget=_budget(), left=int(settings.knowledge_source_sync_documents_per_pass))
    found = await store.all_active_sources()
    if org:
        found = [s for s in found if (s.org, s.namespace) == (org, namespace)]
    if only:
        found = [s for s in found if s.name == only]
    usable = await _usable(found)
    if full:
        for src in usable:
            await source_ops.request_full_sync(src)
    states = await _states(usable)
    asked = sorted(
        (s for s in usable if _requested(states, s)),
        key=lambda s: _requested(states, s),
    )
    rest = [s for s in usable if not _requested(states, s)]
    order = asked + rotate_to(rest, pass_context.resume_cursor(), key=_cursor_of)
    result.sources = len(order)
    # A maintainer's delete request goes before the backlog of changed files.
    await _retire_removed_sources(run, result, org=org, namespace=namespace, only=only)
    for i, src in enumerate(order):
        if run.spent():
            pass_context.set_cursor(_cursor_of(src))
            result.cut = True
            break
        # A pass cancelled inside this source resumes at the next one.
        nxt = order[i + 1] if i + 1 < len(order) else (rest or order)[0]
        pass_context.set_cursor(_cursor_of(nxt))
        await _sync_one(src, states.get(_cursor_of(src), {}), run, result)
    await _extract_pending(order, run, result)
    if not order and not result.retired:
        pass_context.mark_idle()
    return result


async def _usable(found: list[KnowledgeSource]) -> list[KnowledgeSource]:
    """The sources a pass syncs: their namespace is active and still a repository's
    trunk code namespace."""
    pairs = sorted({(s.org, s.namespace) for s in found})
    inactive = await store.inactive_namespaces(pairs)
    trunk = {p for p in pairs if p not in inactive and await source_ops.is_trunk_namespace(*p)}
    return [s for s in found if (s.org, s.namespace) in trunk]


async def _states(found: list[KnowledgeSource]) -> dict[str, dict[str, str]]:
    """Each source's stored sync state, by cursor."""
    out: dict[str, dict[str, str]] = {}
    for org, ns in sorted({(s.org, s.namespace) for s in found}):
        names = [s.name for s in found if (s.org, s.namespace) == (org, ns)]
        for name, state in (await store.read_states(org, ns, names)).items():
            out[f"{org}/{ns}/{name}"] = state
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
        f"knowledge-source-sync:{_cursor_of(src)}", ttl_s=_LEASE_TTL_S
    ) as lease:
        with customer.for_org(src.org, namespace=src.namespace), pipeline_actor(SYNC_WORKER_ACTOR):
            yield lease


async def _sync_one(
    src: KnowledgeSource, state: dict[str, str], run: Pass, result: PassResult
) -> None:
    try:
        async with _source_turn(src):
            try:
                turn = await _turn_of(src.kind)(src, state, run)
            except SourceGone:
                log.info("knowledge_source_sync: %s was removed during its turn", _cursor_of(src))
                return
            except Exception as e:  # noqa: BLE001 — recorded as the source's failed turn
                log.warning("knowledge_source_sync: %s failed: %s", _cursor_of(src), e)
                turn = TurnResult()
                turn.fail("failed", type(e).__name__)
            await _write_state(src, state, turn)
    except leases.LeaseBusy:
        log.info("knowledge_source_sync: %s is being synced by another copy", _cursor_of(src))
        return
    except Exception as e:  # noqa: BLE001 — one source's failure must not stop the others
        result.errors.append(f"{_cursor_of(src)}: {type(e).__name__}")
        result.failed += 1
        log.warning("knowledge_source_sync: %s failed: %s", _cursor_of(src), e)
        pass_context.note_failure(e, _cursor_of(src))
        return
    result.synced += 1
    result.changed += turn.changed
    result.removed += turn.removed
    result.unconverted += turn.unconverted
    result.skipped += turn.skipped
    result.cut |= turn.cut
    if turn.status != "ok":
        result.failed += 1
        result.errors.append(f"{_cursor_of(src)}: {turn.status} {turn.error}".strip())
        pass_context.note_failure(RuntimeError(turn.error or turn.status), _cursor_of(src))


def _turn_of(kind: str) -> Callable[..., Awaitable[TurnResult]]:
    """The turn function of a source kind; this module's `_turn` reads a folder."""
    # Imported here: a kind's module builds on this one's helpers.
    if kind == "linear":
        from server.knowledge.app import source_sync_linear

        return source_sync_linear.turn
    if kind == "recording":
        from server.knowledge.app import source_sync_recording

        return source_sync_recording.turn
    return _turn


def full_due(detail: dict[str, Any]) -> bool:
    last = str(detail.get("full_listed_at") or "")
    try:
        listed = datetime.fromisoformat(last)
    except ValueError:
        return True
    age = (datetime.now(UTC) - listed).total_seconds()
    return age >= int(settings.knowledge_source_sync_full_interval_seconds)


async def _turn(src: KnowledgeSource, state: dict[str, str], run: Pass) -> TurnResult:
    """List, fetch, convert and write one source's changed files; retire those a
    complete full listing no longer has."""
    detail = json_object(state.get("detail", ""))
    stamp = state.get("state_value", "")
    turn = TurnResult()
    turn.full = bool(detail.get("full")) or not gdrive.valid_stamp(stamp) or full_due(detail)
    start_waits(turn, detail)
    credential = unseal("knowledge_sources.credential", src.credential_enc)
    if not credential:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        turn.fail("credential_invalid", "the stored credential no longer opens; rotate it")
        return turn
    folders = list(src.config.get("folder_ids") or [])
    mime_types = [GDRIVE_MIME_TYPES[k] for k in src.config.get("include") or []]
    listed_at = datetime.now(UTC)
    try:
        token = await gdrive.access_token(gdrive.parse_service_account(credential))
        files, whole = await gdrive.list_all(
            token, folders, mime_types, modified_after="" if turn.full else stamp
        )
    except gdrive.RateLimited:
        turn.fail("rate_limited", "rate_limited")
        return turn
    except gdrive.CredentialRefused as e:
        turn.fail("credential_invalid", client_error(e))
        return turn
    except gdrive.DriveError as e:
        turn.fail("failed", client_error(e))
        return turn
    if turn.full and not await _folders_visible(token, folders, turn):
        return turn
    if not whole:
        # The store could not search everything: write what it listed, but keep the
        # stamp and retire nothing, and list again next pass.
        turn.complete = False
        turn.note = "listing: incomplete search"
    # Only a whole listing of folders the account still sees says what is absent.
    trusted = turn.full and turn.complete
    files = [f for f in files if gdrive.valid_file_id(str(f.get("id") or ""))]
    turn.documents_seen = len(files)
    turn.newest = capped(
        max((str(f.get("modifiedTime") or "") for f in files), default=""), listed_at
    )
    if trusted:
        listed_ids = {str(f["id"]) for f in files}
        turn.waits = {k: v for k, v in turn.waits.items() if k in listed_ids}
    stored = {
        d["file_id"]: d for d in await store.source_revisions(src.org, src.namespace, src.name)
    }
    for f in files:
        if not await _sync_file(src, token, f, stored.get(str(f["id"])), run, turn):
            break
    if trusted and turn.complete:
        # A file listed without an address it can be stored at counts as absent, so
        # its document is kept only if the lookup finds the file still there.
        addresses = {str(f["id"]): source_uri_of(f) for f in files}
        listed = {k: v for k, v in addresses.items() if v.startswith("https://")}
        gone = functools.partial(_file_gone, token, folders=set(folders), turn=turn)
        await retire_absent(src, listed, gone, run, turn)
    return turn


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
            if isinstance(entry, list) and len(entry) == 3 and int(entry[1]) >= _WAIT_PASSES:
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
    return cap.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cap.microsecond // 1000:03d}Z"


async def _folders_visible(token: str, folders: list[str], turn: TurnResult) -> bool:
    """Check that the account still sees every folder; whether the turn may go on.
    A folder it lost lists empty, not refused, so a full listing is trusted for
    removals only after this. A folder that fails leaves the turn `failed` and
    incomplete, so nothing is retired while the files of the folders it still sees
    are written; a rate limit or a 401 ends the turn."""
    for folder in folders:
        try:
            await gdrive.require_folder(token, folder)
        except gdrive.RateLimited:
            turn.fail("rate_limited", "rate_limited")
            return False
        except gdrive.DriveError as e:
            if e.status == 401:
                turn.fail("credential_invalid", client_error(e))
                return False
            turn.fail("failed", client_error(e))
            return True
    return True


async def retire_absent(
    src: KnowledgeSource,
    listed: dict[str, str],
    gone: Callable[[str], Awaitable[bool | None]],
    run: Pass,
    turn: TurnResult,
) -> None:
    """Retire the source's documents whose item a complete listing lacks, or that
    sit at an address that is no longer their item's. `listed` maps each listed
    item's id to its address. An item missing from the listing is looked up first
    with `gone(id)`: a listing paged while items changed can miss one, so only an
    item the store reports gone counts. `gone` answers False when the lookup fails
    (nothing is retired) and None when the turn must stop (it records why)."""
    docs = await store.source_revisions(src.org, src.namespace, src.name, searchable_only=False)
    for doc in docs:
        address = listed.get(doc["file_id"])
        if address == doc["source_uri"]:
            continue
        if run.spent():
            turn.stop_at_bound()
            return
        run.left -= 1
        if address is None:
            found_gone = await gone(doc["file_id"])
            if found_gone is None:
                return
            if not found_gone:
                continue
        await require_current(src)
        if await retire(src, doc["source_uri"]):
            turn.removed += 1


async def _file_gone(token: str, file_id: str, folders: set[str], turn: TurnResult) -> bool | None:
    """Whether the store says the file is gone: not found, trashed, or in none of
    the source's folders. A lookup that fails retires nothing; a rate limit or a
    401 is the turn's, and None stops the retiring."""
    try:
        state = await gdrive.file_state(token, file_id)
    except gdrive.NotFound:
        return True
    except gdrive.RateLimited:
        turn.fail("rate_limited", "rate_limited")
        return None
    except gdrive.DriveError as e:
        if e.status == 401:
            turn.fail("credential_invalid", client_error(e))
            return None
        return False
    return bool(state.get("trashed")) or not folders & set(state.get("parents") or [])


def client_error(e: BaseException) -> str:
    """The client's own message: what failed and the HTTP status, never a body."""
    return str(e)[:200]


def source_uri_of(f: dict[str, Any]) -> str:
    """A file's address: the link the store reports, without its query or fragment,
    so it reads the same however the link was shared and stays one citation token."""
    link = str(f.get("webViewLink") or "")
    return link.split("#", 1)[0].split("?", 1)[0]


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
) -> bool:
    """Handle one listed item (`f`: its `id` and its revision as `modifiedTime`) at
    address `uri`; False when the turn must stop listing. In order: an item with no
    https address is unconverted; one whose stored copy has this revision and
    address is skipped; one given up on this revision is unconverted; one at an
    address another writer holds is skipped; `before_fetch` (a kind's own checks)
    may settle it; then it costs one of the pass bound and `fetch_and_write` runs,
    answering `written`, `waiting`, `given_up` or `stop`. A written item that moved
    to a new address has its old one retired."""
    file_id = str(f["id"])
    revision = str(f.get("modifiedTime") or "")
    if not uri.startswith("https://"):
        turn.unconverted_for("no_address")
        return True
    if prior and prior["source_revision"] == revision and prior["source_uri"] == uri:
        turn.waits.pop(file_id, None)
        return True  # unchanged: no fetch, no embedding
    entry = _waiting(turn, f)
    if entry is not None and int(entry[1]) >= _WAIT_PASSES:
        # It waited its passes on this revision: unconverted until it changes.
        turn.unconverted_for(str(entry[2]))
        return True
    # Before any fetch: an address another writer holds costs nothing.
    if not await _may_write(src, uri):
        turn.waits.pop(file_id, None)
        turn.skipped += 1
        return True
    if before_fetch is not None and not await before_fetch():
        return True
    if run.spent():
        turn.stop_at_bound()
        return False
    run.left -= 1
    outcome = await fetch_and_write()
    if outcome == "stop":
        return False
    if outcome not in ("waiting", "given_up"):
        turn.waits.pop(file_id, None)
    if outcome == "written" and prior and prior["source_uri"] != uri:
        if await retire(src, prior["source_uri"]):
            turn.removed += 1
    return True


async def _sync_file(
    src: KnowledgeSource,
    token: str,
    f: dict[str, Any],
    prior: dict[str, str] | None,
    run: Pass,
    turn: TurnResult,
) -> bool:
    """Handle one listed file; False when the turn must stop listing."""
    uri = source_uri_of(f)
    before_fetch = functools.partial(_fetchable, f, run, turn)
    fetch_and_write = functools.partial(_fetch_and_write, src, token, f, uri, turn)
    return await sync_item(src, f, uri, prior, run, turn, fetch_and_write, before_fetch)


async def _fetchable(f: dict[str, Any], run: Pass, turn: TurnResult) -> bool:
    """Whether a file goes on to its fetch; one that does not is counted here, at no
    fetch and none of the bound."""
    try:
        if not await _servable(f, run):
            turn.waits.pop(str(f["id"]), None)
            turn.unconverted_for("no_extractor")
            return False
    except extractors.ExtractorUnavailable as e:
        # The service its format is routed to cannot be read: the whole format waits,
        # the turn's, not this file's.
        turn.fail("failed", f"extractor_unavailable: {e}"[:200])
        return False
    if _over_size(f):
        give_up(f, turn, "too_large")  # the listing says so
        return False
    return True


async def _fetch_and_write(
    src: KnowledgeSource, token: str, f: dict[str, Any], uri: str, turn: TurnResult
) -> str:
    """Fetch, convert and write one file: `written`, `waiting` (a failure that may
    pass holds the turn for `_WAIT_PASSES` passes), `given_up` (a refusal gives the
    file up at once; for both the wait entry is kept, and `start_waits` says when
    it is tried again) or `stop` (the turn's own failure: a rate limit, a refused
    credential)."""
    try:
        fetched = await _fetch(token, f)
    except gdrive.RateLimited:
        turn.fail("rate_limited", "rate_limited")
        return "stop"
    except gdrive.TooLarge:
        return give_up(f, turn, "too_large")
    except gdrive.DriveError as e:
        if e.status == 401:
            turn.fail("credential_invalid", client_error(e))
            return "stop"
        if e.refused:
            # The store refuses this one file (an export over its limit, a download
            # turned off, an API not enabled): the others go on.
            return give_up(f, turn, f"http_{e.status}")
        # A 5xx, a timeout, an answer that is not JSON: this file may fetch next time.
        reason = f"http_{e.status}" if e.status else "unreachable"
        return wait(f, turn, reason, client_error(e))
    except extractors.ExtractionFailed as e:
        log.info("knowledge_source_sync: %s not extracted: %s", f["id"], e.code)
        return give_up(f, turn, f"extraction_failed:{e.code}")
    except extractors.ExtractorUnavailable as e:
        # This one file's extraction: the format's other files may still go through.
        return wait(f, turn, "extractor_unavailable", f"extractor_unavailable: {e}")
    except Exception as e:  # noqa: BLE001 — one file's conversion must not stop the others
        log.warning("knowledge_source_sync: reading %s failed: %s", f["id"], e)
        return wait(f, turn, f"fetch_failed:{type(e).__name__}", type(e).__name__)
    await require_current(src)
    try:
        await write_document(
            src,
            f,
            uri,
            fetched.content,
            ingested_format=fetched.ingested_format,
            original=fetched.original,
            extra_metadata={"extraction_coverage": fetched.coverage} if fetched.coverage else None,
        )
    except ValidationError as e:
        # The write refuses this file whatever the pass: it will not go through.
        return give_up(f, turn, f"write_refused:{e.to_detail().get('code') or 'invalid'}")
    except Exception as e:  # noqa: BLE001 — one file's write must not stop the others
        log.warning("knowledge_source_sync: writing %s failed: %s", f["id"], e)
        return wait(f, turn, f"write_failed:{type(e).__name__}", type(e).__name__)
    turn.changed += 1
    return "written"


def give_up(f: dict[str, Any], turn: TurnResult, reason: str) -> str:
    """A file refused for good: unconverted now, and recorded as given up on its
    revision, so later passes skip it without a fetch or any of the bound until it
    changes or `start_waits` grants a retry."""
    turn.waits[str(f["id"])] = [str(f.get("modifiedTime") or ""), _WAIT_PASSES, reason]
    turn.unconverted_for(reason)
    return "given_up"


def _over_size(f: dict[str, Any]) -> bool:
    """Whether the listing already says an uploaded file is over the size bound."""
    if str(f.get("mimeType") or "") not in _FORMAT_OF_MIME:
        return False
    try:
        size = int(f.get("size") or 0)
    except (TypeError, ValueError):
        size = 0  # unknown: the download's own bound catches it
    return size > int(settings.knowledge_source_sync_max_file_bytes)


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


@dataclass(frozen=True)
class _Fetched:
    content: str
    ingested_format: str
    original: bytes = b""
    # How much of an uploaded file the extractor read (`convert.coverage_of`).
    coverage: dict[str, Any] = field(default_factory=dict)


async def _servable(f: dict[str, Any], run: Pass) -> bool:
    """Whether the file can be turned into text here: a native document or deck, or
    an uploaded file whose format an extractor service declares. ExtractorUnavailable
    when the service its format is routed to could not be read: down is not the
    same as not serving the format."""
    mime = str(f.get("mimeType") or "")
    if mime in (GDRIVE_MIME_TYPES["docs"], GDRIVE_MIME_TYPES["slides"]):
        return True
    fmt = _FORMAT_OF_MIME.get(mime)
    if fmt is None:
        return False
    if fmt not in run.formats:
        url = extractors.endpoint_for(fmt, mime)
        if not url:
            run.formats[fmt] = False
        else:
            try:
                run.formats[fmt] = fmt in (await extractors.capabilities(url)).formats
            except extractors.ExtractorUnavailable:
                run.formats[fmt] = None
    served = run.formats[fmt]
    if served is None:
        raise extractors.ExtractorUnavailable(f"the extractor for {fmt} could not be read")
    return served


async def _fetch(token: str, f: dict[str, Any]) -> _Fetched:
    """The file as Markdown, with its bytes when it is an uploaded file (`_servable`)."""
    file_id, mime = str(f["id"]), str(f.get("mimeType") or "")
    max_bytes = int(settings.knowledge_source_sync_max_file_bytes)
    if mime == GDRIVE_MIME_TYPES["docs"]:
        text = await gdrive.export_markdown(token, file_id, max_bytes=max_bytes)
        return _Fetched(text, "markdown")
    if mime == GDRIVE_MIME_TYPES["slides"]:
        deck = await gdrive.presentation(token, file_id, max_bytes=max_bytes)
        return _Fetched(convert.presentation_markdown(deck), "slides")
    fmt = _FORMAT_OF_MIME[mime]
    data = await gdrive.download(token, file_id, max_bytes=max_bytes)
    artifact = await extractors.extract(
        data, name=str(f.get("name") or file_id), media_type=mime, declared_format=fmt
    )
    return _Fetched(convert.artifact_markdown(artifact), fmt, data, convert.coverage_of(artifact))


async def _may_write(src: KnowledgeSource, uri: str) -> bool:
    """Whether the live document at `uri`, if any, is this source's: a document
    another writer or another source holds is never overwritten or retired."""
    doc = await documents.get_one(src.org, src.namespace, uri, include_superseded=False)
    return doc is None or _ours(src, doc)


def _ours(src: KnowledgeSource, doc: documents.DocumentRow) -> bool:
    return doc.actor == SYNC_WORKER_ACTOR and source_tag(src.name) in doc.tags


async def require_current(src: KnowledgeSource) -> None:
    """SourceGone unless the source is still active as the registration this turn
    started with: a remove during the turn stops it before its next write."""
    with sequential_reads():
        fresh = await store.get_source(src.org, src.namespace, src.name)
    if fresh is None or fresh.source_id != src.source_id:
        raise SourceGone(_cursor_of(src))


def _repo_tag(src: KnowledgeSource) -> str:
    cfg = repo_for_namespace(src.org, src.namespace)
    return repo_tag(cfg.git_remote or cfg.repo) if cfg is not None else ""


async def write_document(
    src: KnowledgeSource,
    f: dict[str, Any],
    uri: str,
    content: str,
    *,
    ingested_format: str,
    original: bytes = b"",
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    """Write one item (`f`: its `id`, `name`, `modifiedTime` and, with an original,
    its `mimeType`) at `uri` with `knowledge.upsert`, as the sync worker: tagged
    `source-kind:design_doc`, the repository's `repo:<id>` and `source:<name>`,
    class `imported`, with the source's kind and name, the item's id and revision
    and the conversion in its metadata."""
    tags = ["source-kind:design_doc", source_tag(src.name)]
    if repo := _repo_tag(src):
        tags.insert(1, repo)
    metadata: dict[str, Any] = {
        "source_kind": src.kind,
        "source_name": src.name,
        "source_revision": str(f.get("modifiedTime") or ""),
        "ingested_from": str(f["id"]),
        "ingested_format": ingested_format,
        **(extra_metadata or {}),
    }
    pass_context.check_stop()  # a stopped pass or a lost lease writes nothing
    await writes.handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org=src.org,
            namespace=src.namespace,
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
    )


async def retire(src: KnowledgeSource, uri: str) -> bool:
    """Retire one of the source's documents, with the audit row the transport would
    have written for `knowledge.delete`; False when it is no longer the source's."""
    if not await _may_write(src, uri):
        return False
    pass_context.check_stop()
    out = await writes.handle_knowledge_delete(
        KnowledgeDeleteInput(org=src.org, namespace=src.namespace, source_uri=uri)
    )
    if out.deleted:
        await audit.emit(
            "knowledge.delete",
            org=src.org,
            namespace=src.namespace,
            actor=SYNC_WORKER_ACTOR,
            subject={"source_uri": uri, "source_name": src.name},
        )
    return out.deleted


async def _write_state(src: KnowledgeSource, read: dict[str, str], turn: TurnResult) -> None:
    """Record the turn in the source's sync state, under the namespace's sources
    lease like every other write of it. Nothing is written for a source removed or
    replaced during the turn; a sync request made during it stays."""
    async with source_ops.sources_lease(src.org, src.namespace):
        with sequential_reads():
            fresh = await store.get_source(src.org, src.namespace, src.name)
            states = await store.read_states(src.org, src.namespace, [src.name])
        if fresh is None or fresh.source_id != src.source_id:
            return
        current = states.get(src.name) or {}
        detail = json_object(current.get("detail", ""))
        asked = json_object(read.get("detail", "")).get("requested_at")
        now = _now_iso()
        detail.update(
            last_synced_at=now,
            status=turn.status,
            documents_seen=turn.documents_seen,
            changed=turn.changed,
            removed=turn.removed,
            unconverted=turn.unconverted,
            unconverted_reasons=turn.reasons,
            skipped=turn.skipped,
            error=turn.error or turn.note,
        )
        if turn.waits_read:
            detail["file_waits"] = turn.waits
            if turn.waits_reset_for:
                detail["waits_reset_for"] = turn.waits_reset_for
            if turn.retry_granted_for is not None:
                detail["retry_granted_for"] = turn.retry_granted_for
        stamp = current.get("state_value", "")
        if turn.complete:
            # A change made after this listing is stamped later than anything it saw.
            stamp = max(turn.newest, stamp if gdrive.valid_stamp(stamp) else "")
            if turn.full:
                detail["full_listed_at"] = now
            if detail.get("requested_at") == asked:
                detail.pop("requested_at", None)
                detail.pop("full", None)
        await store.write_state(src.org, src.namespace, src.name, state_value=stamp, detail=detail)


async def _retire_removed_sources(
    run: Pass, result: PassResult, *, org: str, namespace: str, only: str
) -> None:
    """Retire the documents of removed sources whose maintainer asked for it, then
    clear the request once none is left."""
    for src in await store.pending_delete_sources():
        if org and (src.org, src.namespace) != (org, namespace):
            continue
        if only and src.name != only:
            continue
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
    docs = await store.source_documents(src.org, src.namespace, src.name)
    for doc in docs:
        if run.spent():
            result.cut = True
            return
        run.left -= 1
        if await retire(src, str(doc["source_uri"])):
            result.retired += 1
    with sequential_reads():
        if await store.count_documents(src.org, src.namespace, src.name):
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


def _unmined(docs: list[dict[str, str]], detail: dict[str, Any]) -> list[dict[str, str]]:
    """The documents whose stored revision is not the one last mined: one whose last try
    ran out of read time first, so it gets the most of the next pass, then oldest first."""
    mined, waits = _mined_of(detail), _extract_waits(detail)

    def order(d: dict[str, str]) -> tuple[bool, str, str]:
        wait = waits.get(d["source_uri"]) or ["", 0, ""]
        late = wait[0] == d["source_revision"] and wait[2] == "out_of_time"
        return not late, d["source_revision"], d["source_uri"]

    todo = [d for d in docs if mined.get(d["source_uri"]) != d["source_revision"]]
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
    # Why the turn left documents for a later pass (`over_cap`, `held:<code>`).
    deferred: str = ""
    # What stops the whole leg: `held:<code>` for a hold on every org's calls, or
    # `stopped` when the pass was stopped or the source's lease lost mid-write.
    stop: str = ""


async def _extract_pending(order: list[KnowledgeSource], run: Pass, result: PassResult) -> None:
    """The extraction leg: mine each source's unmined documents while the pass has
    budget left, and keep each source's `pending_extract` count and, when it waits,
    the reason current."""
    from server.memory.app.decision_extract import no_model_error

    with sequential_reads():
        # What this pass's turns just wrote.
        states = await _states(order)
        queues = []
        for src in order:
            detail = json_object(states.get(_cursor_of(src), {}).get("detail", ""))
            try:
                docs = await store.source_revisions(src.org, src.namespace, src.name)
            except Exception as e:  # noqa: BLE001 — one source's failed read must not stop the others
                result.errors.append(f"{_cursor_of(src)}: extraction {type(e).__name__}")
                result.failed += 1
                continue
            queues.append((src, detail, docs, _unmined(docs, detail)))
    queues = [q for q in queues if q[3] or q[1].get("pending_extract")]
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
    for src, _detail, docs, todo in queues:
        if stop == "stopped":
            return  # the pass or a lease ended: nothing more is written
        why = (stop if todo else "") or _not_now(src, todo, off, run)
        if why:
            if todo:
                result.extract_deferred += 1
            result.cut |= why == "pass_bound"
            await _write_mined(src, None, docs, pending=len(todo), waiting=why)
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
    return read_s * _BUDGET_FRACTION


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
            if mined.stop != "stopped":
                await _write_mined(src, mined, docs, pending=len(todo) - len(mined.settled))
    except (leases.LeaseBusy, SourceGone):
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
    uri, revision = doc["source_uri"], doc["source_revision"]
    await require_current(src)
    pass_context.check_stop()  # a stopped pass or a lost lease writes nothing
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
        if report.write_stopped:
            mined.stop = "stopped"
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
    from server.memory.app.decision_extract import extract_document
    from server.observability import cost_accumulator
    from server.workers.cost_event import emit_worker_cost_event_for

    started = time.monotonic()
    cost_accumulator.begin_op()
    try:
        return await extract_document(
            src.org,
            src.namespace,
            uri,
            dry_run=False,
            read_deadline=read_deadline,
            may_write=may_write,
        )
    finally:
        emit_worker_cost_event_for(
            org=src.org, namespace=src.namespace, operation=f"worker.{WORKER}", started=started
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
        if waiting and waiting != "nothing":
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
    "full_due",
    "give_up",
    "require_current",
    "retire",
    "retire_absent",
    "run_sync",
    "source_uri_of",
    "start_waits",
    "sync_item",
    "wait",
    "write_document",
]
