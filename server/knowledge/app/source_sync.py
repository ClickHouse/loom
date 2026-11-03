"""The sync of registered document sources into their repositories' documents:
one pass over every active source, run by the `knowledge_source_sync` worker and
by `loom knowledge sync`.

A pass reads the active sources and drops those whose namespace is inactive or no
longer a repository's trunk. Sources a maintainer asked to sync (`sync_now`) go
first, the rest from where the previous pass stopped. Each source's turn holds
the source's own lease, so two copies of the worker never sync it at once.

A turn lists the source's files: every file on its first turn, when asked
(`full`) and once `knowledge_source_sync_full_interval_seconds` have passed since
the last full listing; otherwise those changed after the stored stamp. A file
whose stored copy has the listed revision is skipped without a fetch. A changed
file is fetched, turned into Markdown and written with `knowledge.upsert` as
`worker:knowledge-source-sync`, `imported`, tagged `source:<name>`. A native
document is exported as Markdown, a presentation converted slide by slide, and
a PDF or `.pptx` sent to the extractor service its format is routed to (the
server parses no original itself); the bytes are kept as the document's
original. A file that cannot be turned into text (no extractor declares its
format, the extractor or the store refuses it, it is too large) is counted
`unconverted` with its reason and not written; it is tried again when it changes
and on every full listing. A file whose extractor cannot be reached keeps the
turn incomplete for `_EXTRACTOR_WAIT_PASSES` passes, then counts as unconverted.

Before each write the source is read again: a source removed or replaced during
its turn stops the turn, which records nothing.

A document is retired only on positive evidence that the source dropped it:
absence from a complete full listing whose folders the account can still see,
or a maintainer's `remove(delete_documents=true)`. A fetch error, a rate limit or
a folder the account lost retires nothing. A document another writer or another
source holds is never written or retired.
The stamp moves only when every listed file was handled, so a cut turn is
listed again and its written files are skipped by revision.

`knowledge_source_sync_documents_per_pass` bounds the files fetched and the
documents retired in one pass, across all sources.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.clients import gdrive
from server.code.app.index.pass_budget import PassBudget, rotate_to
from server.code.domain.index.remote import repo_tag
from server.code.domain.index.repos import repo_for_namespace
from server.core import leases
from server.core.ch import sequential_reads
from server.core.errors import OverloadedError
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
from server.workers.status import set_worker_identity

log = logging.getLogger("loom.knowledge.source_sync")

WORKER = "knowledge_source_sync"
# The share of the pass deadline after which no new source is started.
_BUDGET_FRACTION = 0.75  # not a setting: the shape every cursor-resumed worker uses
# A source's lease outlives a stalled renewal by this much; it is renewed while held.
_LEASE_TTL_S = 120.0  # not a setting: the entry-lease shape
# Passes a file waits on an extractor that cannot be reached before it counts as
# unconverted: about 45 minutes at the default interval, past a restart or a
# redeploy of the service, and short enough that one file cannot hold a source's
# stamp for good. Unconverted, it is tried again on the next full listing.
_EXTRACTOR_WAIT_PASSES = 3  # not a setting: a bound on how long a stamp may stall
_FORMAT_OF_MIME = {
    GDRIVE_MIME_TYPES["pdf"]: "pdf",
    GDRIVE_MIME_TYPES["pptx"]: "pptx",
}


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
    # Why files were unconverted: reason -> count.
    reasons: dict[str, int] = field(default_factory=dict)
    # Files waiting on an extractor that could not be reached: id -> [revision, passes].
    waits: dict[str, list[Any]] = field(default_factory=dict)
    # Every listed file was handled, so the stamp may move.
    complete: bool = True
    # The newest revision the listing returned.
    newest: str = ""
    full: bool = False

    def fail(self, status: str, error: str) -> None:
        self.status, self.error, self.complete = status, error, False

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
    # Sources whose turn ended with a status other than ok, or failed outright.
    failed: int = 0
    # The pass stopped before every source had its turn.
    cut: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class _Pass:
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
    `namespace`, and `only` one source by name). `full` lists every file of each."""
    result = PassResult()
    run = _Pass(budget=_budget(), left=int(settings.knowledge_source_sync_documents_per_pass))
    found = await store.all_active_sources()
    if org:
        found = [s for s in found if (s.org, s.namespace) == (org, namespace)]
    if only:
        found = [s for s in found if s.name == only]
    usable = await _usable(found)
    states = await _states(usable)
    asked = sorted(
        (s for s in usable if _requested(states, s)),
        key=lambda s: _requested(states, s),
    )
    rest = [s for s in usable if not _requested(states, s)]
    order = asked + rotate_to(rest, pass_context.resume_cursor(), key=_cursor_of)
    result.sources = len(order)
    for i, src in enumerate(order):
        if run.spent():
            pass_context.set_cursor(_cursor_of(src))
            result.cut = True
            break
        # A pass cancelled inside this source resumes at the next one.
        nxt = order[i + 1] if i + 1 < len(order) else (rest or order)[0]
        pass_context.set_cursor(_cursor_of(nxt))
        await _sync_one(src, states.get(_cursor_of(src), {}), run, full, result)
    if not result.cut:
        await _retire_removed_sources(run, result, org=org, namespace=namespace, only=only)
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
async def _source_turn(src: KnowledgeSource) -> AsyncIterator[None]:
    """The source's lease (LeaseBusy when another copy holds it), its tenant for
    the embedding calls, and the worker's actor for every write."""
    pass_context.set_namespace(src.namespace)
    set_worker_identity(WORKER, org=src.org, namespace=src.namespace)
    async with leases.cluster_lease(f"knowledge-source-sync:{_cursor_of(src)}", ttl_s=_LEASE_TTL_S):
        with customer.for_org(src.org, namespace=src.namespace), pipeline_actor(SYNC_WORKER_ACTOR):
            yield


async def _sync_one(
    src: KnowledgeSource, state: dict[str, str], run: _Pass, full: bool, result: PassResult
) -> None:
    try:
        async with _source_turn(src):
            try:
                turn = await _turn(src, state, run, full)
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
    if turn.status != "ok":
        result.failed += 1
        result.errors.append(f"{_cursor_of(src)}: {turn.status} {turn.error}".strip())
        pass_context.note_failure(RuntimeError(turn.error or turn.status), _cursor_of(src))


def _full_due(detail: dict[str, Any]) -> bool:
    last = str(detail.get("full_listed_at") or "")
    try:
        listed = datetime.fromisoformat(last)
    except ValueError:
        return True
    age = (datetime.now(UTC) - listed).total_seconds()
    return age >= int(settings.knowledge_source_sync_full_interval_seconds)


async def _turn(src: KnowledgeSource, state: dict[str, str], run: _Pass, full: bool) -> TurnResult:
    """List, fetch, convert and write one source's changed files; retire those a
    complete full listing no longer has."""
    detail = json_object(state.get("detail", ""))
    stamp = state.get("state_value", "")
    turn = TurnResult()
    turn.full = (
        full or bool(detail.get("full")) or not gdrive.valid_stamp(stamp) or _full_due(detail)
    )
    credential = unseal("knowledge_sources.credential", src.credential_enc)
    if not credential:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        turn.fail("credential_invalid", "the stored credential no longer opens; rotate it")
        return turn
    folders = list(src.config.get("folder_ids") or [])
    mime_types = [GDRIVE_MIME_TYPES[k] for k in src.config.get("include") or []]
    # Only the token exchange and the listing speak for the credential: a refusal of
    # one file is that file's.
    try:
        token = await gdrive.access_token(gdrive.parse_service_account(credential))
        files = await gdrive.list_all(
            token, folders, mime_types, modified_after="" if turn.full else stamp
        )
    except gdrive.RateLimited:
        turn.fail("rate_limited", "rate_limited")
        return turn
    except gdrive.CredentialRefused as e:
        turn.fail("credential_invalid", _error(e))
        return turn
    except gdrive.DriveError as e:
        turn.fail("failed", _error(e))
        return turn
    if turn.full and not await _folders_visible(token, folders, turn):
        return turn
    files = [f for f in files if gdrive.valid_file_id(str(f.get("id") or ""))]
    turn.documents_seen = len(files)
    turn.newest = max((str(f.get("modifiedTime") or "") for f in files), default="")
    stored = {
        d["file_id"]: d for d in await store.source_revisions(src.org, src.namespace, src.name)
    }
    waits = detail.get("extractor_waits")
    if not isinstance(waits, dict):
        waits = {}
    for f in files:
        if not await _sync_file(src, token, f, stored.get(str(f["id"])), run, turn, waits):
            break
    if turn.full and turn.complete:
        await _retire_absent(src, {str(f["id"]) for f in files}, run, turn)
    return turn


async def _folders_visible(token: str, folders: list[str], turn: TurnResult) -> bool:
    """Whether the account still sees every folder. A folder it lost lists empty,
    not refused, so a full listing is trusted for removals only after this; one that
    fails stops the turn with nothing written or retired."""
    for folder in folders:
        try:
            await gdrive.require_folder(token, folder)
        except gdrive.RateLimited:
            turn.fail("rate_limited", "rate_limited")
            return False
        except gdrive.DriveError as e:
            turn.fail("failed", _error(e))
            return False
    return True


async def _retire_absent(
    src: KnowledgeSource, listed: set[str], run: _Pass, turn: TurnResult
) -> None:
    """Retire the source's documents whose file a complete full listing lacks."""
    docs = await store.source_revisions(src.org, src.namespace, src.name, searchable_only=False)
    for doc in docs:
        if doc["file_id"] in listed:
            continue
        if run.spent():
            turn.complete = False
            return
        run.left -= 1
        await _require_current(src)
        if await retire(src, doc["source_uri"]):
            turn.removed += 1


def _error(e: BaseException) -> str:
    """The client's own message: what failed and the HTTP status, never a body."""
    return str(e)[:200]


def source_uri_of(f: dict[str, Any]) -> str:
    """A file's address: the link the store reports, without its query or fragment,
    so it reads the same however the link was shared and stays one citation token."""
    link = str(f.get("webViewLink") or "")
    return link.split("#", 1)[0].split("?", 1)[0]


async def _sync_file(
    src: KnowledgeSource,
    token: str,
    f: dict[str, Any],
    prior: dict[str, str] | None,
    run: _Pass,
    turn: TurnResult,
    waits: dict[str, Any],
) -> bool:
    """Handle one listed file; False when the turn must stop listing."""
    revision = str(f.get("modifiedTime") or "")
    uri = source_uri_of(f)
    if not uri.startswith("https://"):
        turn.skipped += 1
        return True
    if prior and prior["source_revision"] == revision and prior["source_uri"] == uri:
        return True  # unchanged: no fetch, no embedding
    # Before any fetch: an address another writer holds costs nothing.
    if not await _may_write(src, uri):
        turn.skipped += 1
        return True
    try:
        if not await _servable(f, run):
            turn.unconverted_for("no_extractor")  # costs no fetch, so none of the bound
            return True
        if run.spent():
            turn.complete = False
            return False
        run.left -= 1
        fetched = await _fetch(token, f)
    except gdrive.RateLimited:
        turn.fail("rate_limited", "rate_limited")
        return False
    except gdrive.TooLarge:
        turn.unconverted_for("too_large")
        return True
    except gdrive.DriveError as e:
        if not e.refused:
            turn.fail("failed", _error(e))
            return False
        # The store refuses this one file (an export over its limit, a download
        # turned off, an API not enabled): the others go on.
        turn.unconverted_for(f"http_{e.status}")
        return True
    except extractors.ExtractionFailed as e:
        log.info("knowledge_source_sync: %s not extracted: %s", f["id"], e.code)
        turn.unconverted_for(f"extraction_failed:{e.code}")
        return True
    except extractors.ExtractorUnavailable as e:
        _wait_for_extractor(f, run, turn, waits, e)
        return True
    await _require_current(src)
    await _write(src, f, uri, fetched)
    turn.changed += 1
    if prior and prior["source_uri"] != uri and await retire(src, prior["source_uri"]):
        turn.removed += 1
    return True


def _wait_for_extractor(
    f: dict[str, Any],
    run: _Pass,
    turn: TurnResult,
    waits: dict[str, Any],
    e: extractors.ExtractorUnavailable,
) -> None:
    """A file whose extractor cannot be reached: the turn stays incomplete, until
    the same revision has waited `_EXTRACTOR_WAIT_PASSES` passes."""
    fmt = _FORMAT_OF_MIME.get(str(f.get("mimeType") or ""))
    if fmt is not None:
        run.formats[fmt] = None  # the rest of the pass does not call it again
    file_id, revision = str(f["id"]), str(f.get("modifiedTime") or "")
    before = waits.get(file_id)
    passes = 1
    if isinstance(before, list) and len(before) == 2 and before[0] == revision:
        passes = int(before[1]) + 1
    if passes >= _EXTRACTOR_WAIT_PASSES:
        turn.unconverted_for("extractor_unavailable")
        return
    turn.waits[file_id] = [revision, passes]
    turn.fail("failed", f"extractor_unavailable: {e}"[:200])


@dataclass(frozen=True)
class _Fetched:
    content: str
    ingested_format: str
    original: bytes = b""
    # How much of an uploaded file the extractor read (`convert.coverage_of`).
    coverage: dict[str, Any] = field(default_factory=dict)


async def _servable(f: dict[str, Any], run: _Pass) -> bool:
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
    if int(f.get("size") or 0) > max_bytes:
        raise gdrive.TooLarge(f"{file_id}: over {max_bytes} bytes")
    data = await gdrive.download(token, file_id, max_bytes=max_bytes)
    artifact = await extractors.extract(
        data, name=str(f.get("name") or file_id), media_type=mime, declared_format=fmt
    )
    return _Fetched(convert.artifact_markdown(artifact), fmt, data, convert.coverage_of(artifact))


async def _may_write(src: KnowledgeSource, uri: str) -> bool:
    """Whether the live document at `uri`, if any, is this source's: a document
    another writer or another source holds is never overwritten or retired."""
    doc = await documents.get_one(src.org, src.namespace, uri, include_superseded=False)
    return doc is None or (doc.actor == SYNC_WORKER_ACTOR and source_tag(src.name) in doc.tags)


async def _require_current(src: KnowledgeSource) -> None:
    """SourceGone unless the source is still active as the registration this turn
    started with: a remove during the turn stops it before its next write."""
    with sequential_reads():
        fresh = await store.get_source(src.org, src.namespace, src.name)
    if fresh is None or fresh.source_id != src.source_id:
        raise SourceGone(_cursor_of(src))


def _repo_tag(src: KnowledgeSource) -> str:
    cfg = repo_for_namespace(src.org, src.namespace)
    return repo_tag(cfg.git_remote or cfg.repo) if cfg is not None else ""


async def _write(src: KnowledgeSource, f: dict[str, Any], uri: str, fetched: _Fetched) -> None:
    tags = ["source-kind:design_doc", source_tag(src.name)]
    if repo := _repo_tag(src):
        tags.insert(1, repo)
    metadata: dict[str, Any] = {
        "source_kind": src.kind,
        "source_name": src.name,
        "source_revision": str(f.get("modifiedTime") or ""),
        "ingested_from": str(f["id"]),
        "ingested_format": fetched.ingested_format,
    }
    if fetched.coverage:
        metadata["extraction_coverage"] = fetched.coverage
    pass_context.check_stop()  # a stopped pass or a lost lease writes nothing
    await writes.handle_knowledge_upsert(
        KnowledgeUpsertInput(
            org=src.org,
            namespace=src.namespace,
            source_uri=uri,
            content=fetched.content,
            title=str(f.get("name") or ""),
            description=convert.description_of(fetched.content),
            tags=tags,
            metadata=metadata,
            original_bytes_b64=(
                base64.b64encode(fetched.original).decode() if fetched.original else None
            ),
            original_filename=str(f.get("name") or "") if fetched.original else "",
            blob_content_type=str(f.get("mimeType") or "") if fetched.original else "",
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
            error=turn.error,
            extractor_waits=turn.waits,
        )
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
    run: _Pass, result: PassResult, *, org: str, namespace: str, only: str
) -> None:
    """Retire the documents of removed sources whose maintainer asked for it, then
    clear the request once none is left."""
    for src in await store.pending_delete_sources():
        if org and (src.org, src.namespace) != (org, namespace):
            continue
        if only and src.name != only:
            continue
        if run.spent():
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


async def _retire_all(src: KnowledgeSource, run: _Pass, result: PassResult) -> None:
    docs = await store.source_documents(src.org, src.namespace, src.name)
    for doc in docs:
        if run.spent():
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


__all__ = [
    "PassResult",
    "SourceGone",
    "TurnResult",
    "WORKER",
    "retire",
    "run_sync",
    "source_uri_of",
]
