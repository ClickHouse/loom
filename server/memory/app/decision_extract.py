"""`loom decisions extract`: mine one knowledge document into decision records.

The model reads the document whole, or in windows of whole sections when it is longer
than `memory_decision_extract_window_chars`, once, and once more when its reply holds no
decision. It writes each decision as a maintainer would (outcome, reasons, rejected
options, in its own words), each claim citing quotes of the text; a claim none of whose
quotes is in the text is left out, and a decision whose outcome has none is dropped
(`server.memory.domain.decision_extract`). A second call over the same text, the support
check, keeps only the claims their quotes state or directly imply, and reads each of the
extractor's stored decisions on this document against the text: still stated, and which
candidate restates it. A kept candidate is reconciled against what the namespace already
holds, then written through `decision.record` under one fixed identity, so a re-run
revises the extractor's own rows instead of writing beside them:

- the key's active record is the extractor's and the support check finds it still stated,
  and its row carries the same files, source, constraint and repository tags: nothing is
  written (a pinned or attested row may still gain the repository's tag by a tags-only
  write); with other row fields, an unfrozen one is recorded again in its stored words,
  unless its outcome is recorded: then only its tags are reconciled;
- it is found no longer stated and its row is the extractor's, not pinned or attested, and
  from this document: recorded again (Revise), unless its outcome is recorded: then it
  is tagged `source-changed`;
- it is found no longer stated and its row is frozen (pinned, or attested in its current
  form) and from this document: nothing is written, the new body is printed in the run's
  report, and the row is tagged `source-changed`, a tag a later run drops once the
  document says the same again;
- its row is another writer's or another document's: nothing is written;
- no record under the key, but a pinned or attested decision row reads the same at or
  above the conflict threshold: skipped as already recorded.

A stored row's verdict holds over every window: stated when any window states it, no
longer stated only when every window says so in two checks (the second without
candidates), else not judged. A candidate that restates a stored decision not found gone
is not written, under a new key or the key of a stored decision not found stated: the
stored row and its stage stand. After the writes, the extractor deletes its rows citing
this document, unless pinned or attested, that are no longer stated. A row it keeps
(pinned, attested, or with a recorded outcome) that is no longer stated is tagged
`source-changed` instead. A row not judged is never flagged or deleted; one that is not
frozen or acted on is recorded again when the run gives its key and that candidate
restates no other row not found gone: as the text reads when the words differ, in its
stored words when only its row fields do. A refused write is reported and the run goes
on; a model call held for the org (`customer.HOLDS`) stops the run before any write.

A document outside a repository's code namespace names the repository: its code names
resolve in the repository's trunk, and the rows go there or stay beside the document,
tagged with the repository (`_target`). Every memory row the reconcile and retire steps
read is in the namespace the rows go to (`_read_own`). Runs in-process: with no auth
context (the CLI) the operator may do everything; with one bound, `_target` checks the
document's, the repository's and the target namespace, and `_read_own` the trunk of the
document's branch namespace.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from server.auth.authz import row_in_scope
from server.code.domain.extract.lang_markdown import is_markdown_path
from server.core.background import TaskSet
from server.core.errors import LoomServerError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.llm import customer
from server.memory.api.models_decision import DecisionRecordInput
from server.memory.app import decisions as svc
from server.memory.domain import decision_extract as dx
from server.memory.domain.corpus_synthesis import CorpusChunk, Window, windows
from server.memory.domain.decision_schema import CONSTRAINT_TAG, PROPOSED_TAG, SOURCE_CHANGED_TAG
from server.memory.domain.decision_statement import alternatives, collapse_ws, memory_value
from server.memory.domain.files import detect_value_files, normalize_files
from server.memory.domain.trust import confirmed, row_content_sha256, verification_state
from server.memory.store import (
    Identity,
    MemoryRow,
    actors_by_key,
    get_by_key,
    get_many_by_keys,
    list_by_namespace,
    live_writer_record,
    memory_rows_by_identity_keys,
)
from server.observability.genai import llm_stage
from server.operations.base import Role
from server.settings.budgets import llm_max_tokens

log = logging.getLogger(__name__)

# One identity for every row the extractor writes: `loom.memory` keys rows by identity, so
# a key written from another would be a second live row beside the first.
EXTRACTOR_AGENT = "loom-decisions"
DESIGN_DOC_TAG = "source-kind:design_doc"
# A model's reading of a document, ranked below a row someone wrote directly.
EXTRACTED_CONFIDENCE = 0.5
_PAGE = 500


# What a candidate did to the namespace.
Action = Literal[
    "written",
    "revised",
    "unchanged",
    "flagged",
    "already_flagged",
    "row_changed",
    "not_ours",
    "outcome_known",
    "already_recorded",
]


@dataclass
class RecordReport:
    memory_key: str
    # row_changed (a tag write found its row gone or moved on) is reported in `races`; the
    # caller keeps it out of `records`.
    action: Action
    # The decision.record body, with the identity the write goes (or would go) under.
    body: dict[str, Any]
    # One line per mention looked up in the code index: name, signal, what it resolved to.
    signals: list[str] = field(default_factory=list)
    # Pull request numbers and setting names, for a human to resolve into files.
    unresolved: list[str] = field(default_factory=list)
    note: str = ""
    # "deleted" or "rewritten": the row a tag write found deleted or rewritten since the run
    # read it.
    race: str = ""


@dataclass
class Dropped:
    section: str
    reason: str
    quote: str = ""
    # A read the run needed (a window, or a read or write of the candidate) was refused or
    # failed (the reason says which): it counts toward the exit code, as
    # `ExtractReport.refused` does.
    refused: bool = False
    # The refusal is one every later run gets too (`PERMANENT_REFUSALS`): the
    # document, not the moment, decides it.
    permanent: bool = False


@dataclass
class Retried:
    """A second read of a window: the section it is reported under and why."""

    section: str
    reason: str


@dataclass
class ExtractReport:
    source_uri: str
    dry_run: bool
    # Why the run read nothing and retired nothing: a refusal of its target (`_target`), no
    # document, no chunks for the current embedding model, no usable model, the extractor's
    # rows unreadable, elsewhere or linked to another repository (`_read_own`), or no window
    # read.
    error: str = ""
    windows: int = 0
    records: list[RecordReport] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    image_only: list[str] = field(default_factory=list)
    # A second read of a window, and why: its reply held no decision. A window gets at
    # most one extra read.
    retried: list[Retried] = field(default_factory=list)
    # Keys deleted (or, in a dry run, to delete): the extractor's rows the support check
    # found no longer stated, twice.
    retired: list[str] = field(default_factory=list)
    # "<old key> -> <key>" for each extractor row deleted (or to delete) because it is no
    # longer stated and this run recorded the decision under another key in its place.
    replaced: list[str] = field(default_factory=list)
    # Keys tagged (or to tag) `source-changed`: frozen rows (pinned or attested), and
    # rows with a recorded outcome, whose document now says otherwise.
    flagged: list[str] = field(default_factory=list)
    # Keys of rows the retire step keeps that gained (or would gain) the repository's tag;
    # a row reconcile tags says so in its record's note.
    linked: list[str] = field(default_factory=list)
    # The retire step's "<key>: refused: <code>" for a read or write the server refused,
    # "<key>: failed: <error>" for one a provider error stopped. A candidate's is its
    # `dropped` line, marked `refused`.
    refused: list[str] = field(default_factory=list)
    # "<key>: deleted since the run read it" or "<key>: rewritten since the run read it":
    # a write that found its row gone or moved on, a race the next run settles.
    races: list[str] = field(default_factory=list)
    # The hold (`customer.HOLDS`) that stopped the run's model calls, its reads or its
    # writes: the org's calls are switched off or refused for now. A run held while
    # reading wrote nothing; one held while writing kept what it wrote. Neither retired.
    held: LoomServerError | None = None
    # The run's `read_deadline` passed before every window was read: it wrote and
    # retired nothing.
    out_of_time: bool = False
    # `may_write` said no before a write: the run stopped there; what it wrote stays.
    write_stopped: bool = False

    def reads_stopped(self) -> bool:
        return self.held is not None or self.out_of_time


def _frozen(row: MemoryRow) -> bool:
    """The row is frozen against re-extraction: pinned, or attested in its current form,
    read as memory.get reads it."""
    current = row_content_sha256(row)
    state = verification_state(row.verified_by, row.verified_sha256, current)
    return confirmed(row.pinned, state)


def _is_extractor(agent: str, user_id: str, session_id: str) -> bool:
    return (agent, user_id, session_id) == (EXTRACTOR_AGENT, "-", "-")


def source_resource_id(namespace: str, source_uri: str, section: str) -> str:
    """The citation of a section, in the form context.pack gives a chunk."""
    return f"knowledge:{namespace}/{source_uri}#{section}"


async def cites_document(org: str, resource_id: str, namespace: str, source_uri: str) -> bool:
    """`resource_id` cites a section of this document: it starts `knowledge:<ns>/<uri>#`,
    and no longer document `<uri>#<part>` it also starts with has ever been ingested. So a
    heading holding a `#` ("C# bindings") still cites this document, and `doc#v2#Intro`
    cites `doc#v2` once that document exists, live or deleted."""
    from server.knowledge.api.models_knowledge import KnowledgeGetInput
    from server.knowledge.app.document_reads import handle_knowledge_get

    prefix = source_resource_id(namespace, source_uri, "")
    if not resource_id.startswith(prefix):
        return False
    rest = resource_id[len(prefix) :]
    for i, ch in enumerate(rest):
        if ch != "#":
            continue
        longer = await handle_knowledge_get(
            KnowledgeGetInput(
                org=org,
                namespace=namespace,
                source_uri=f"{source_uri}#{rest[:i]}",
                include_superseded=True,
            )
        )
        if longer.found:
            return False
    return True


def _unread_reply(stop_reason: str, cap: int, call: str) -> str:
    """Why a model reply could not be read as the JSON asked for; `cap` is the call's
    max_tokens and `call` its LLM_MAX_TOKENS entry."""
    if stop_reason in ("length", "max_tokens"):
        return (
            f"reply cut at the token limit ({cap}); lower --window-chars "
            "(LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS) or --max-per-section, or raise "
            f"{call} in LOOM_LLM_MAX_TOKENS_OVERRIDES"
        )
    reason = "the model's reply is not the JSON asked for"
    return f"{reason} (stop reason: {stop_reason})" if stop_reason else reason


_FAKE_ANSWER = (
    "answered by the fake model; set the fallback model's key, or LOOM_LLM_PROVIDER=fake "
    "to run on it on purpose"
)


def _fake_chosen() -> bool:
    """LOOM_LLM_PROVIDER names the fake model: the run reads with it on purpose."""
    from server.settings import settings

    return settings.llm_provider == "fake" and not settings.llm_provider_inferred()


def no_model_error() -> str:
    """Why the run must not start: it would read the document with the fake model, whose
    decisions pass grounding by construction and would be recorded as the
    document's. That is so when a keyed provider has no key (the client falls back to the
    fake) or when nothing chose a provider (it resolves to `fake`). A provider set to
    `fake` explicitly, with LOOM_LLM_PROVIDER, runs."""
    from server.llm.client import provider_key_missing
    from server.settings import settings

    provider = settings.llm_provider
    if provider_key_missing(provider):
        return (
            f"LOOM_LLM_PROVIDER={provider} has no API key set, so the run would read the "
            "document with the fake model; set the key, or LOOM_LLM_PROVIDER=fake to run on "
            "it on purpose"
        )
    if provider == "fake" and not _fake_chosen():
        return (
            "no LLM provider is configured, so the run would read the document with the "
            "fake model; set LOOM_LLM_PROVIDER and its key, or LOOM_LLM_PROVIDER=fake to run "
            "on it on purpose"
        )
    return ""


# What a read or write the run makes may raise, reported per candidate or row (or as the
# run's error before any model call): a server refusal, or the embedding provider's own
# error (memory.set and memory.search embed in-process and let an HTTP error or a timeout
# through as raised).
_REPORTED_ERRORS = (HTTPException, LoomServerError, httpx.HTTPError, TimeoutError)


# Refusals a write of the same candidate gets on every run: a blacklisted decision, a
# pinned peer it would contradict, a body the op will not take.
PERMANENT_REFUSALS = frozenset({"BLACKLISTED", "PINNED_CONFLICT", "VALIDATION_FAILED"})


def _code(e: BaseException) -> str:
    """The server's refusal code, or '' for a provider error or a timeout."""
    if isinstance(e, LoomServerError):
        return e.code
    if isinstance(e, HTTPException):
        detail: dict[str, Any] = e.detail if isinstance(e.detail, dict) else {}
        return str(detail.get("code") or e.status_code)
    return ""


def _refusal(e: BaseException) -> str:
    code = _code(e)
    return f"refused: {code}" if code else f"failed: {type(e).__name__}"


async def finish_writes(writes: TaskSet) -> None:
    """Wait for the units of one run (`writes`, as passed to `extract_document`) still
    running, such as the one a cancelled run left: a caller that bills the run's spend
    drains its accumulator after them. Another run's units are not waited for. A cancel
    of this wait leaves them running."""
    pending = [t for t in writes if not t.done()]
    if pending:
        await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))


async def _whole[T](writes: TaskSet, coro: Coroutine[Any, Any, T]) -> T:
    """Run one candidate's or one row's writes, with their audit rows, as one unit held in
    the run's `writes`, to the end even when the caller is cancelled: no row is left
    without its audit row. Every unit is registered for shutdown to drain
    (`server.core.background.drain_task_sets`)."""
    return await asyncio.shield(writes.spawn(coro, name="decision-extract-write"))


def _may_go_on(report: ExtractReport, may_write: Callable[[], bool] | None) -> bool:
    """Whether the next write may start; once `may_write` says no, the run stops
    writing (`write_stopped`): what it wrote stays, and nothing more is retired."""
    if may_write is None or may_write():
        return True
    report.write_stopped = True
    report.error = "stopped before every write: the run may no longer write"
    return False


@dataclass(frozen=True)
class Target:
    """Where a run reads and writes: the namespace holding the document, the one whose code
    index resolves its names, the one its rows go to, and the repository's `repo:<id>` tag
    (empty with no repository, or one with no remote)."""

    doc_namespace: str
    code_namespace: str
    rows_namespace: str
    repo_tag: str = ""

    @property
    def row_tag(self) -> str:
        """The tag the rows carry: the repository's, unless they are in its own namespace."""
        return "" if self.rows_namespace == self.code_namespace else self.repo_tag


Into = Literal["repo", "source"]


async def _target(
    org: str, namespace: str, repo: str, into: Into | None, dry_run: bool
) -> Target | str:
    """The run's target, or why it may not run. With no `repo`, the document's namespace
    is all three. With one, its names resolve in the repository's trunk code
    namespace, and the rows go there (`into="repo"`) or stay beside the document tagged
    with the repository (`into="source"`, the default unless the document is in a code
    namespace). The caller must read the document and the repository and write where the
    rows go (read, for a dry run); with no auth context (the CLI) the operator may."""
    from server.code.app.index.repo_registry import refresh_db_repos, registry_read
    from server.code.app.repo_scope import repo_trunk, trunk_namespace_of
    from server.code.domain.index.repos import repo_for_namespace

    # A repository added from the dashboard is known once the snapshot is read; a run
    # without --repo needs it too, for the trunk of a branch namespace (`_read_own`).
    await refresh_db_repos()
    if not registry_read():
        # Without it a repository's trunk would read as none, and a check be skipped.
        return "could not read the repositories added from the dashboard; try again"
    # First, so nothing about the document's namespace reaches a caller who cannot read it.
    if not await _may(org, namespace, Role.VIEWER):
        return f"not allowed to read {org}/{namespace}"
    if into not in (None, "repo", "source"):
        return f"--into is repo or source, not {into!r}"
    if not repo:
        if into:
            return "--into needs --repo"
        target = Target(namespace, namespace, namespace)
    else:
        trunk, tag = repo_trunk(org, repo)
        # One answer for a repository that is not there and one the caller may not read.
        if not trunk or not await _may(org, trunk, Role.VIEWER):
            return f"no repository {repo!r} you may read is configured in {org}"
        if into is None:
            into = "repo" if repo_for_namespace(org, namespace) else "source"
        home = trunk_namespace_of(org, namespace)
        if into == "source" and home and home != trunk:
            # Its rows would link to this repository's code and be read in another's.
            return "the document is in another repository's code namespace; use --into repo"
        target = Target(namespace, trunk, trunk if into == "repo" else namespace, tag)
        if not tag and target.rows_namespace != trunk:
            return f"repository {repo!r} has no remote to tag its rows with; use --into repo"
    # The document's namespace and the trunk were read-checked above; a dry run writes nothing.
    if not dry_run and not await _may(org, target.rows_namespace, Role.WRITER):
        return f"not allowed to write {org}/{target.rows_namespace}"
    return target


async def _may(org: str, namespace: str, role: Role) -> bool:
    try:
        # The refusal is the whole answer: an unverifiable grant counts as the server's.
        return await row_in_scope(org, namespace, role)
    except Exception as e:  # noqa: BLE001 — a gate that cannot check refuses
        log.warning("decisions extract: access check on %s failed (closed): %s", namespace, e)
        return False


async def extract_document(
    org: str,
    namespace: str,
    source_uri: str,
    *,
    dry_run: bool,
    repo: str = "",
    into: Into | None = None,
    max_per_section: int = 3,
    window_chars: int | None = None,
    read_deadline: float | None = None,
    may_write: Callable[[], bool] | None = None,
    writes: TaskSet | None = None,
) -> ExtractReport:
    """Mine `source_uri`, a document in `namespace`, into decision records; `repo` names
    the repository its code names resolve in and `into` where the rows go (`_target`).
    `dry_run` writes nothing and reports what a run would write. `read_deadline` (a
    `time.monotonic()` instant) bounds the model reads: a run still reading then stops
    with nothing written, so its writes and their audit rows are never cut short.
    `may_write` is asked before each candidate's or row's writes (the sync worker: its
    pass is not stopped and it holds the source's lease); each such unit runs whole, held
    in `writes` (a fresh set when none is given) for `finish_writes`."""
    from server.knowledge.api.models_knowledge import KnowledgeGetInput
    from server.knowledge.app.document_reads import document_chunks, handle_knowledge_get

    report = ExtractReport(source_uri, dry_run)
    if error := no_model_error():
        report.error = error
        return report
    t = await _target(org, namespace, repo, into, dry_run)
    if isinstance(t, str):
        report.error = t
        return report
    doc = await handle_knowledge_get(
        KnowledgeGetInput(org=org, namespace=namespace, source_uri=source_uri)
    )
    if not doc.found:
        report.error = f"no live document {source_uri!r} in {org}/{namespace}"
        return report
    chunks = await document_chunks(org, namespace, source_uri)
    if not chunks:
        report.error = (
            f"{source_uri!r} has no chunks for the current embedding model; run "
            f"`loom rechunk --org {org} --namespace {namespace} --source-uri {source_uri} "
            "--apply` first"
        )
        return report
    from server.settings import settings

    report.image_only = dx.image_only_sections(doc.content or "")
    chars = window_chars or settings.memory_decision_extract_window_chars
    wins = _windows(chunks, set(report.image_only), chars)
    report.windows = len(wins)
    # One scan of the extractor's rows on this document and their records, for the prompts
    # and the retire step; rows this run writes are skipped by the retire step.
    try:
        own, report.error = await _read_own(org, t, source_uri)
    except _REPORTED_ERRORS as e:
        # Before any model call: a run that cannot see its own rows reads nothing.
        report.error = f"could not read the extractor's rows: {_refusal(e)}"
    if report.error:
        return report
    known = _known_decisions(own)
    statements = {row.memory_key: dx.stored_statement(record) for row, record in own}
    state, answered = await _candidates(
        org,
        t,
        source_uri,
        wins,
        max_per_section,
        report,
        known,
        statements,
        doc.title or "",
        read_deadline,
    )
    if wins and not answered and not report.error:
        report.error = (
            "no window was read: every model call failed, was answered by the fake model, "
            "came back unreadable, or answered nothing and failed when read again"
        )
    if report.error:
        # Held, out of time or nothing read: such a run must not write or retire anything.
        return report
    # An empty TaskSet is falsy: the caller's is told apart from none by `is None`.
    units = TaskSet() if writes is None else writes
    written = await _write_kept(org, t, source_uri, state, dry_run, report, may_write, units)
    if written is not None:
        await _retire(report, own, state, written, t.row_tag, may_write, units)
    return report


async def _write_kept(
    org: str,
    t: Target,
    source_uri: str,
    state: _Kept,
    dry_run: bool,
    report: ExtractReport,
    may_write: Callable[[], bool] | None,
    writes: TaskSet,
) -> set[str] | None:
    """Reconcile and write each kept candidate; the keys handled, for the retire step, or
    None when a hold on an embedding or `may_write` stopped the writes: the run then
    retires nothing, and a later run goes on from what it wrote. A candidate that restates
    a stored decision not found gone is not written, under a new key or the key of a
    stored decision not found stated."""
    written: set[str] = set()
    # One under the key of a row the text states is reconciled as that row's candidate.
    kept_by = {
        heir: key
        for key, heir in state.heirs.items()
        if state.still.get(key) is not False and state.still.get(heir) is not True
    }
    for cand, section, texts in state.kept:
        if not _may_go_on(report, may_write):
            return None
        key = svc.decision_key(cand.category, cand.scenario)
        if key in kept_by:
            report.dropped.append(
                Dropped(section, f"restates {kept_by[key]}, not found gone", cand.quote)
            )
            continue
        try:
            rec = await _whole(
                writes,
                _reconcile(
                    org,
                    t,
                    source_uri,
                    cand,
                    section,
                    texts,
                    dry_run,
                    still=state.still.get(key),
                ),
            )
        except customer.HOLDS as e:
            report.held = e
            report.error = _held_error(org, e)
            return None
        except _REPORTED_ERRORS as e:
            # A blacklisted or newly pinned decision, a busy key, a store that timed out:
            # the other candidates and the retire step still run, and a re-run does not
            # stop at the same place.
            reason = f"{key}: {_refusal(e)}"
            permanent = _code(e) in PERMANENT_REFUSALS
            report.dropped.append(
                Dropped(section, reason, cand.quote, refused=True, permanent=permanent)
            )
            # Handled this run: the retire step must not judge the key it just failed on.
            written.add(key)
            continue
        written.add(rec.memory_key)
        if rec.action in ("written", "revised"):
            state.recorded.add(rec.memory_key)
        if rec.race:
            report.races.append(f"{rec.memory_key}: {rec.race} since the run read it")
        if rec.action == "row_changed":
            continue
        report.records.append(rec)
        if rec.action == "flagged":
            report.flagged.append(rec.memory_key)
    return written


def _held_error(org: str, e: LoomServerError) -> str:
    return f"model calls for {org} are held ({e.code}); try again later"


def _windows(chunks: list[CorpusChunk], skipped: set[str], window_chars: int) -> list[Window]:
    """The windows sent to the model. A section judged image-only (`skipped`) is not sent.
    An empty-path chunk of an image-only preamble is skipped only when its own text is
    image-only too."""

    def sent(c: CorpusChunk) -> bool:
        if dx.no_words(c.text):
            return False
        if dx.breadcrumb(c.section_path) not in skipped:
            return True
        return not c.section_path and not dx.image_only(c.text)

    return windows([c for c in chunks if sent(c)], window_chars=window_chars)


async def _read_own(
    org: str, t: Target, source_uri: str
) -> tuple[list[tuple[MemoryRow, dict[str, Any]]], str]:
    """The extractor's rows on this document where the run writes, with their records
    (`_own_records`); and why the run must not go on, or '': its rows for the document are
    in the other namespace a run may write them to, or carry another repository's tag (any,
    for a run without `--repo` or a repository with no remote), which the run would drop or
    add to. Reconcile and retire read one namespace, so going on would leave the document's
    decisions in two places, or linked to the wrong repository."""
    from server.code.app.repo_scope import trunk_namespace_of

    rows = await _extractor_rows(org, t.rows_namespace, t.doc_namespace, source_uri)
    tagged = sorted({x for r in rows for x in r.tags if x.startswith("repo:")} - {t.repo_tag})
    if tagged:
        names = await _readable_names(org, set(tagged))
        linked_to = ", ".join(names.get(x, x) for x in tagged)
        return [], (
            f"this document's decisions are linked to a repository ({linked_to}); run with "
            "its --repo and --into source, or delete them first"
        )
    # Where an earlier run may have written them: beside the document, the named
    # repository's trunk, or the trunk of the branch namespace the document is in.
    places = [t.doc_namespace, t.code_namespace, trunk_namespace_of(org, t.doc_namespace)]
    # _target checked the first two; a branch's trunk is new here.
    checked = {t.doc_namespace, t.code_namespace}
    for other in dict.fromkeys(ns for ns in places if ns and ns != t.rows_namespace):
        if other not in checked and not await _may(org, other, Role.VIEWER):
            # Neither the trunk's name nor whether rows are there is the caller's to learn.
            return [], "this document's namespace belongs to a repository you may not read"
        if await _extractor_rows(org, other, t.doc_namespace, source_uri, first=True):
            return [], (
                f"this document's decisions are in {org}/{other}; run as the run that wrote "
                "them (with no --repo, or its --repo and --into), or delete them there first"
            )
    return await _own_records(org, t.rows_namespace, rows), ""


async def _readable_names(org: str, tags: set[str]) -> dict[str, str]:
    """`repo:<id>` tag -> owner/name of each of `tags`' repositories the caller may read;
    another stays a tag, as `_target` answers for a repository the caller may not read."""
    from server.code.app.repo_scope import repo_names

    out: dict[str, str] = {}
    for tag, (name, trunk) in repo_names(org).items():
        if tag not in tags:
            continue
        try:
            # Only the message's wording rides on it, not the refusal.
            readable = await row_in_scope(org, trunk, Role.VIEWER, hides_answer=False)
        except Exception as e:  # noqa: BLE001 — a name that cannot be checked stays a tag
            log.warning("decisions extract: access check on %s failed (closed): %s", trunk, e)
            readable = False
        if readable:
            out[tag] = name
    return out


async def _own_records(
    org: str, namespace: str, rows: list[MemoryRow]
) -> list[tuple[MemoryRow, dict[str, Any]]]:
    """Each of the extractor's rows with the record its key holds, where that record is
    the extractor's own: another identity's record of the key, active or superseding
    ours, says nothing about whether our row's text still stands."""
    out: list[tuple[MemoryRow, dict[str, Any]]] = []
    for row in rows:
        writer = await live_writer_record(org, namespace, row.memory_key)
        if writer is not None and _is_extractor(writer.agent, writer.user_id, writer.session_id):
            out.append((row, svc.parse_record_json(writer.record_json)))
    return out


def _known_decisions(own: list[tuple[MemoryRow, dict[str, Any]]]) -> list[tuple[str, str, str]]:
    """(key, category, scenario) of the extractor's decisions on this document, pinned or
    not, for the read prompt, whose keys a still-recorded decision keeps, and for the
    support check, which reads each row's statement. At most
    memory_decision_extract_known_max of them, to bound the prompts; a row past the cap
    gets no verdict and stays."""
    from server.settings import settings

    known = [
        (row.memory_key, str(record.get("category") or ""), str(record.get("scenario") or ""))
        for row, record in own
    ]
    return known[: settings.memory_decision_extract_known_max]


async def _call(
    llm: Any,
    system: str,
    user: str,
    section: str,
    report: ExtractReport,
    *,
    cap: int,
    stage: str,
    fake_chosen: bool,
    deadline: float | None,
) -> tuple[str, str] | None:
    """(reply text, stop reason) of one model call of the run under `stage`, a read or a
    support check, capped at `cap` output tokens, or None when it is lost: held for the
    org (`report.held`), past `deadline` (`report.out_of_time`), failed, or answered by
    the fake model for a real provider; the last two are a dropped line marked
    `refused` under `section`."""
    label = "the support check" if stage == "decision_support" else "the model call"

    def late() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    if late():
        report.out_of_time = True
        return None
    left = None if deadline is None else deadline - time.monotonic()
    try:
        with llm_stage(stage):
            async with asyncio.timeout(left):
                res = await llm.complete(
                    system, user, max_tokens=cap, response_format={"type": "json_object"}
                )
    except customer.HOLDS as e:
        # Every later call of the run would be held too: the run stops.
        report.held = e
        return None
    except (httpx.HTTPError, TimeoutError, LoomServerError) as e:
        if late():
            report.out_of_time = True
            return None
        # A provider error, a timeout or a refused call loses this window only.
        report.dropped.append(Dropped(section, f"{label} failed: {type(e).__name__}", refused=True))
        return None
    if not fake_chosen and res.provider == "fake":
        # A fallback with no key answered: its decisions pass grounding by construction.
        report.dropped.append(Dropped(section, _FAKE_ANSWER, refused=True))
        return None
    return res.text, res.stop_reason


async def _read_window(
    llm: Any,
    system: str,
    w: Window,
    section: str,
    report: ExtractReport,
    *,
    fake_chosen: bool,
    title: str,
    deadline: float | None = None,
) -> list[dx.Candidate] | None:
    """The model's candidates for one window, or None when the read is lost (`_call`) or
    its reply is unreadable, a dropped line marked `refused` under `section`. The
    window's text opens with the document's `title` when it has one."""
    from server.llm.json_text import strip_code_fence

    # Read once per call: the override is live, and a cut reply names the cap it used.
    cap = llm_max_tokens("decision_extract")
    got = await _call(
        llm,
        system,
        dx.user_prompt(w, title),
        section,
        report,
        cap=cap,
        stage="decision_extract",
        fake_chosen=fake_chosen,
        deadline=deadline,
    )
    if got is None:
        return None
    text, stop = got
    cands = dx.parse_candidates(strip_code_fence(text))
    if cands is None:
        report.dropped.append(
            Dropped(section, _unread_reply(stop, cap, "decision_extract"), refused=True)
        )
    return cands


async def _read_support(
    llm: Any,
    w: Window,
    grounded: list[tuple[dx.Candidate, str, list[str]]],
    stored: list[tuple[str, str]],
    section: str,
    report: ExtractReport,
    *,
    fake_chosen: bool,
    title: str,
    deadline: float | None = None,
) -> dx.Verdicts | None:
    """The support check's verdicts on one window's grounded candidates and the stored
    rows, or None when the call is lost (`_call`) or its reply is unreadable, a dropped
    line marked `refused` under `section`."""
    from server.llm.json_text import strip_code_fence

    user = dx.support_prompt(dx.user_prompt(w, title), [c for c, _s, _t in grounded], stored)
    cap = llm_max_tokens("decision_support")
    got = await _call(
        llm,
        dx.support_system(bool(stored)),
        user,
        section,
        report,
        cap=cap,
        stage="decision_support",
        fake_chosen=fake_chosen,
        deadline=deadline,
    )
    if got is None:
        return None
    text, stop = got
    verdicts = dx.parse_support(strip_code_fence(text), {key for key, _s in stored})
    if verdicts is None:
        reason = "support check: " + _unread_reply(stop, cap, "decision_support")
        report.dropped.append(Dropped(section, reason, refused=True))
    return verdicts


@dataclass
class _Kept:
    """The candidates kept so far in a run, and the support check's verdicts on the
    extractor's stored rows."""

    max_per_section: int
    # (candidate, its section, the texts it was checked against)
    kept: list[tuple[dx.Candidate, str, list[str]]] = field(default_factory=list)
    per_section: dict[str, int] = field(default_factory=dict)
    keys: set[str] = field(default_factory=set)
    # Kept outcome, whitespace and case aside -> its key: windows that overlap read one
    # decision twice.
    outcomes: dict[str, str] = field(default_factory=dict)
    # stored key -> each window's verdict, in the order the windows were checked
    verdicts: dict[str, list[bool]] = field(default_factory=dict)
    # stored key -> the verdict over the document (`_verdict`): True stated, False no
    # longer stated (every window, twice), None not judged
    still: dict[str, bool | None] = field(default_factory=dict)
    # stored key -> the key of a candidate that restates it
    heirs: dict[str, str] = field(default_factory=dict)
    # stored key -> (a window's verdict, the candidate key that window named), settled
    # into `heirs` with the verdict (`_confirm_gone`)
    heir_votes: dict[str, list[tuple[bool, str]]] = field(default_factory=dict)
    # Keys this run recorded (written or revised), which can take a retired row's place.
    recorded: set[str] = field(default_factory=set)


def _verdict(verdicts: list[bool], windows: int) -> bool | None:
    """One stored row's verdict over a document read in `windows` windows: stated when any
    window states it (one window may hold what another does not), no longer stated only
    when every window said so, else not judged."""
    if any(verdicts):
        return True
    # No window read is no verdict, never a "gone" from no check at all.
    return False if windows and len(verdicts) == windows else None


async def _confirm_gone(
    read_support: Callable[..., Coroutine[Any, Any, dx.Verdicts | None]],
    wins: list[Window],
    stored: list[tuple[str, str]],
    state: _Kept,
    report: ExtractReport,
) -> None:
    """Settle each stored row's verdict (`_verdict`), and read the rows found no longer
    stated once more, without candidates: a row stays no longer stated only when this
    second check agrees in every window, since the retire step deletes on that verdict. A
    second check that is lost, or leaves a row out, leaves it not judged. A row's heir is
    named by a window that states it when one does: a window without its text may name
    another decision."""
    for key, _text in stored:
        state.still[key] = _verdict(state.verdicts.get(key, []), len(wins))
        votes = state.heir_votes.get(key, [])
        if state.still[key]:
            # A window that states the row names its restatement; another may not hold it.
            named = [heir for still, heir in votes if still]
        else:
            named = [heir for _still, heir in votes]
        if named:
            state.heirs[key] = named[0]
    gone = [(key, text) for key, text in stored if state.still[key] is False]
    if not gone:
        return
    again: dict[str, list[bool]] = {}
    for w in wins:
        verdicts = await read_support(w, [], gone, dx.breadcrumb(w.chunks[0].section_path), report)
        if report.reads_stopped():
            break
        if verdicts is None:
            continue
        for key, (still, _heir) in verdicts.stored.items():
            again.setdefault(key, []).append(still)
    for key, _text in gone:
        # Gone only when the second check agrees; a split between the two is no verdict.
        if _verdict(again.get(key, []), len(wins)) is not False:
            state.still[key] = None


_EMPTY_REPLY = "the model's reply held no decision; the window was read once more"


async def _candidates(
    org: str,
    t: Target,
    source_uri: str,
    wins: list[Window],
    max_per_section: int,
    report: ExtractReport,
    known: list[tuple[str, str, str]],
    statements: dict[str, str],
    title: str,
    deadline: float | None = None,
) -> tuple[_Kept, int]:
    """The run's kept candidates and verdicts (`_Kept`), and how many windows the model
    answered with a readable reply. Each window is read, its candidates grounded in its
    text (`dx.ground`), and the support check run over them and over the stored rows
    (`statements`, key -> statement); a candidate kept passes the check, is no
    duplicate, fits decision.record, and is within `max_per_section` for its section.

    A window gets at most one extra read: when its reply holds no decision though its
    text is more than a caption. A window whose support check is lost keeps nothing and
    gives no verdict."""
    from server.llm.client import get_llm_client

    llm = get_llm_client()
    system = dx.system_prompt(max_per_section, [(c, s) for _k, c, s in known])
    read = functools.partial(
        _read_window, llm, system, fake_chosen=_fake_chosen(), title=title, deadline=deadline
    )
    read_support = functools.partial(
        _read_support, llm, fake_chosen=_fake_chosen(), title=title, deadline=deadline
    )
    state = _Kept(max_per_section)
    stored = [(key, statements[key]) for key, _c, _s in known]
    answered = 0
    # Not gated here by the namespace's cost cap: the operator's command spends as asked,
    # and the sync worker checks the cap before each document.
    for w in wins:
        first = dx.breadcrumb(w.chunks[0].section_path)
        cands = await read(w, first, report)
        if report.reads_stopped():
            break
        if cands == [] and dx.more_than_a_caption("\n\n".join(c.text for c in w.chunks)):
            report.retried.append(Retried(first, _EMPTY_REPLY))
            cands = await read(w, first, report)
        if report.reads_stopped():
            break
        if cands is None:
            continue
        answered += 1
        grounded = _ground_window(w, cands, report)
        if not grounded and not stored:
            continue
        verdicts = await read_support(w, grounded, stored, first, report)
        if report.reads_stopped():
            break
        if verdicts is not None:
            _check_window(org, t, source_uri, grounded, verdicts, state, report)
    if not report.reads_stopped():
        await _confirm_gone(read_support, wins, stored, state, report)
    if report.held is not None:
        report.error = _held_error(org, report.held)
    elif report.out_of_time:
        report.error = "the run's time to read the document ran out; nothing was written"
    return state, answered


def _ground_window(
    w: Window, cands: list[dx.Candidate], report: ExtractReport
) -> list[tuple[dx.Candidate, str, list[str]]]:
    """(candidate, its section, the window's texts) for each candidate whose outcome has
    a quote in the window's text outside a recap section, its claims grounded
    (`dx.ground`); each dropped candidate and left-out claim reported."""
    texts = [c.text for c in w.chunks]

    def section_of(quote: str) -> str:
        # Located by the text's own span: a stored quote is on one line, without labels.
        return dx.quote_section(w, dx.find_span(quote, texts) or quote)

    out: list[tuple[dx.Candidate, str, list[str]]] = []
    for cand in cands:
        grounded, notes = dx.ground(cand, texts)
        section = section_of(grounded.quote if grounded else cand.quote)
        for note in notes:
            report.dropped.append(Dropped(section, note, cand.quote))
        if grounded is None:
            continue
        # A quote in a recap restates a decision made elsewhere: it locates none.
        evidence = [q for q in grounded.evidence if not dx.in_recap(section_of(q))]
        if not evidence:
            report.dropped.append(Dropped(section, dx.RECAP, cand.quote))
            continue
        grounded = replace(grounded, evidence=evidence)
        out.append((grounded, section_of(grounded.quote), texts))
    return out


def _check_window(
    org: str,
    t: Target,
    source_uri: str,
    grounded: list[tuple[dx.Candidate, str, list[str]]],
    verdicts: dx.Verdicts,
    state: _Kept,
    report: ExtractReport,
) -> None:
    """Keep each grounded candidate of one window that the support check supports, with
    only its supported claims, in `state`, reporting each dropped one and each left-out
    claim; and record the window's verdicts on the stored rows."""
    keys: dict[int, str] = {}
    for i, (cand, section, texts) in enumerate(grounded):
        checked, notes = dx.supported(i, cand, verdicts)
        for note in notes:
            report.dropped.append(Dropped(section, note, cand.quote))
        if checked is None:
            continue
        reason, key = _duplicate_or_unfit(org, t, source_uri, checked, section, state)
        outcome = collapse_ws(checked.outcome).casefold()
        if reason:
            report.dropped.append(Dropped(section, reason, cand.quote))
            # A restatement names a kept decision: the earlier one with this key or outcome.
            if key in state.keys:
                keys[i] = key
            elif outcome in state.outcomes:
                keys[i] = state.outcomes[outcome]
            continue
        keys[i] = key
        state.per_section[section] = state.per_section.get(section, 0) + 1
        state.keys.add(key)
        state.outcomes[outcome] = key
        state.kept.append((checked, section, texts))
    for key, (still, heir) in verdicts.stored.items():
        state.verdicts.setdefault(key, []).append(still)
        if heir is not None and heir in keys and keys[heir] != key:
            state.heir_votes.setdefault(key, []).append((still, keys[heir]))


def _duplicate_or_unfit(
    org: str, t: Target, source_uri: str, cand: dx.Candidate, section: str, state: _Kept
) -> tuple[str, str]:
    """(why a candidate the support check kept is still dropped, or ''; its key)."""
    try:
        key = svc.decision_key(cand.category, cand.scenario)
    except ValueError:
        # No letter or digit (punctuation, symbols or marks alone): nothing to key on.
        return "no letters or digits to build a key from", ""
    if key in state.keys:
        return "same key as an earlier candidate", key
    if collapse_ws(cand.outcome).casefold() in state.outcomes:
        return "same decision as an earlier candidate", key
    if not _fits(org, t, source_uri, cand, section):
        # Checked before it takes a slot, a key or a code lookup.
        return "does not fit decision.record", key
    if state.per_section.get(section, 0) >= state.max_per_section:
        return f"the section already has --max-per-section {state.max_per_section}", key
    return "", key


def _record_input(
    org: str, t: Target, source_uri: str, cand: dx.Candidate, section: str, files: list[str]
) -> DecisionRecordInput:
    """The decision.record input for a candidate; raises ValidationError when it does not
    fit the op's field limits (`_fits` checks first). A decision the document gives no
    reason for is recorded with its outcome as its reasoning, which its statement reads
    as no stated reason."""
    tags = [DESIGN_DOC_TAG, *([CONSTRAINT_TAG] if cand.is_constraint else [])]
    if t.row_tag:
        tags.append(t.row_tag)
    return DecisionRecordInput(
        org=org,
        namespace=t.rows_namespace,
        agent=EXTRACTOR_AGENT,
        category=cand.category,
        scenario=cand.scenario,
        reasoning=cand.reasoning or cand.outcome,
        outcome=cand.outcome,
        # As decision.record will keep them (deduplicated once cut, at most
        # MAX_ALTERNATIVES), but uncut.
        alternatives=svc.clean_alternatives(cand.rejected(), cut=False),
        confidence=EXTRACTED_CONFIDENCE,
        stage=cand.stage,
        tags=tags,
        files=files,
        source_class="derived",
        source_resource_id=source_resource_id(t.doc_namespace, source_uri, section),
    )


def _fits(org: str, t: Target, source_uri: str, cand: dx.Candidate, section: str) -> bool:
    """The candidate fits decision.record's field limits. Checked with no files: the files
    a run adds pass through normalize_files, capped at the FileRefs limits, so they never
    make a fitting candidate unfit."""
    try:
        _record_input(org, t, source_uri, cand, section, [])
    except ValidationError:
        return False
    return True


def record_body(inp: DecisionRecordInput) -> dict[str, Any]:
    """The decision.record request a human can send as it is."""
    body = inp.model_dump(mode="json", exclude={"caused_by", "influenced_by", "valid_at"})
    return {
        k: v for k, v in body.items() if not (k in ("run_id", "decision_maker") and v in ("-", ""))
    }


def _same_row(row: MemoryRow, inp: DecisionRecordInput) -> bool:
    """The row carries the files, source and tags the candidate would write."""
    return (
        sorted(row.files) == sorted(inp.files or [])
        and row.source_resource_id == inp.source_resource_id
        and _own_tags(row.tags) == _own_tags(inp.tags)
    )


def _same_words(record: dict[str, Any], inp: DecisionRecordInput) -> bool:
    """The candidate words the decision as the record stores it, whitespace and case
    aside."""
    stored = [record.get(k) or "" for k in ("category", "scenario", "outcome", "reasoning")]
    given = [inp.category, inp.scenario, inp.outcome, inp.reasoning]
    flat = [collapse_ws(x).casefold() for x in [*stored, *given]]
    alts = [collapse_ws(a).casefold() for a in alternatives(record)]
    # As decision.record stores them: a long one cut and marked.
    given_alts = svc.clean_alternatives(inp.alternatives, cut=True)
    return flat[:4] == flat[4:] and alts == [collapse_ws(a).casefold() for a in given_alts]


def _in_stored_words(
    inp: DecisionRecordInput, record: dict[str, Any], row: MemoryRow
) -> DecisionRecordInput:
    """`inp` with the stored record's words and stage: a decision the text still states
    is recorded again for its row fields only, never reworded."""
    return inp.model_copy(
        update={
            "category": str(record.get("category") or inp.category),
            "scenario": str(record.get("scenario") or inp.scenario),
            "outcome": str(record.get("outcome") or inp.outcome),
            "reasoning": str(record.get("reasoning") or inp.reasoning),
            "alternatives": alternatives(record),
            "stage": svc.stage_of(PROPOSED_TAG in row.tags, record),
        }
    )


def _own_tags(tags: list[str]) -> set[str]:
    """The tags a run sets from the document and the repository, not the ones the record
    derives: a row extracted before it was linked to a repository gains its tag."""
    return {t for t in tags if t == CONSTRAINT_TAG or t.startswith("repo:")}


async def _reconcile(
    org: str,
    t: Target,
    source_uri: str,
    cand: dx.Candidate,
    section: str,
    texts: list[str],
    dry_run: bool,
    *,
    still: bool | None,
) -> RecordReport:
    """Decide what the candidate does to the target namespace, and do it unless `dry_run`.
    `still`: the support check's verdict on the key's stored row, when it is the
    extractor's on this document (None: not judged, and a frozen or acted-on row is then
    left as it is)."""
    namespace = t.rows_namespace
    files, signals, unresolved, complete = await resolve_mentions(
        org, t.code_namespace, cand, texts
    )
    # A path the document writes out (`src/Storages/MergeTreeData.cpp`) is a file too; the
    # explicit list turns off decision.record's own detection, so it is merged in here.
    cited = "\n".join(cand.quotes())
    files = normalize_files([*files, *detect_value_files(cited)])
    inp = _record_input(org, t, source_uri, cand, section, files)
    key = svc.decision_key(inp.category, inp.scenario)
    rec = RecordReport(key, "written", record_body(inp), signals, unresolved)
    writer = await live_writer_record(org, namespace, key)
    if writer is None:
        peer = await _frozen_restatement(org, namespace, key, inp)
        if peer is not None:
            rec.action = "already_recorded"
            rec.note = f"already recorded by {peer.agent} under {peer.memory_key}"
            return rec
    else:
        row = await get_by_key(
            Identity(
                org=org,
                namespace=namespace,
                agent=writer.agent,
                user_id=writer.user_id,
                session_id=writer.session_id,
            ),
            key,
            scope=WHOLE_NAMESPACE,
            fresh=True,
            strong=True,
        )
        if row is None:
            # A writer record with no live row under it: nothing of the extractor's to revise.
            return _not_ours(rec, writer, None)
        if not complete:
            # The code index did not answer for every name: keep the files the row has
            # rather than drop one an earlier run resolved.
            inp = inp.model_copy(update={"files": list(row.files)})
            rec.body["files"] = list(row.files)
        this_doc = await cites_document(org, row.source_resource_id, t.doc_namespace, source_uri)
        ours = this_doc and _is_extractor(writer.agent, writer.user_id, writer.session_id)
        record = svc.parse_record_json(writer.record_json)
        same = ours and still is True
        if same:
            read_as = inp.stage
            inp = _in_stored_words(inp, record, row)
            rec.body = record_body(inp)
            if read_as != inp.stage and not svc.has_outcome(record):
                rec.note = f"the document reads as {read_as}; the row keeps its stage {inp.stage}"
        # Our own row is revised for its files, source, constraint or repository tag too: a
        # document extracted before its code was indexed gains its files on a later run.
        stale_row = ours and not _frozen(row) and not _same_row(row, inp)
        if same and not stale_row:
            return await _unchanged(rec, row, t, dry_run=dry_run)
        if ours and still is None:
            again = await _unjudged(rec, row, record, inp, t, dry_run=dry_run)
            if again is None:
                return rec
            inp = again
        if _frozen(row) and ours:
            return await _flag_frozen(rec, row, t, dry_run)
        if not ours:
            return _not_ours(rec, writer, row)
        if svc.has_outcome(record):
            # decision.record rebuilds the record from the body, so a re-record would drop
            # the outcome.
            rec.note = "its outcome is recorded; re-recording would lose it"
            rec.action = "outcome_known" if same else await _flag(rec, row, dry_run, t.row_tag)
            if same:
                # Only its row fields are stale: the document says what it says again.
                await _reconcile_tags(rec, row, dry_run, t.row_tag)
            return rec
        rec.action = "revised"
    if not dry_run:
        from server.memory.app.decision import handle_decision_record

        # decision.record takes no precondition: a decision.outcome that lands between the
        # read above and this write is replaced by the new record, outcome and all.
        await handle_decision_record(inp)
        await _audit("decision.record", inp)
    return rec


async def _unjudged(
    rec: RecordReport,
    row: MemoryRow,
    record: dict[str, Any],
    inp: DecisionRecordInput,
    t: Target,
    *,
    dry_run: bool,
) -> DecisionRecordInput | None:
    """The extractor's row the support check gave no verdict on: None when it is left as
    it is (`rec` unchanged, linked to the repository as the retire step links a row it
    keeps), else the input to record again, in the stored words when only the row's fields
    changed. Either way its `source-changed` tag stays: no verdict clears nothing."""
    if _left_unjudged(row, record, inp):
        rec.action = "unchanged"
        rec.note = "the support check gave no verdict on the row; left as it is"
        await _reconcile_tags(rec, row, dry_run, t.row_tag, judged=False)
        return None
    if _same_words(record, inp):
        inp = _in_stored_words(inp, record, row)
    if SOURCE_CHANGED_TAG in row.tags:
        inp = inp.model_copy(update={"tags": [*inp.tags, SOURCE_CHANGED_TAG]})
    rec.body = record_body(inp)
    return inp


def _left_unjudged(row: MemoryRow, record: dict[str, Any], inp: DecisionRecordInput) -> bool:
    """A row the support check gave no verdict on stays as it is when it is frozen or acted
    on (flagged only on a verdict), or when the read words it as stored."""
    if _frozen(row) or svc.has_outcome(record):
        return True
    return _same_words(record, inp) and _same_row(row, inp)


def _not_ours(rec: RecordReport, writer: Any, row: MemoryRow | None) -> RecordReport:
    """Another writer's record or another document's row under the key: printed, never
    written."""
    rec.body.update(agent=writer.agent, user_id=writer.user_id, session_id=writer.session_id)
    rec.action = "not_ours"
    rec.note = (
        f"recorded by {writer.agent} from {row.source_resource_id or 'no document'}; "
        "the extractor revises only its own rows on this document"
        if row is not None
        else f"recorded by {writer.agent}; the extractor revises only its own rows"
    )
    return rec


async def _unchanged(
    rec: RecordReport, row: MemoryRow, t: Target, *, dry_run: bool
) -> RecordReport:
    """The extractor's own row on this document, still stated: nothing is written but
    tags. A row that is not frozen reached here with its tags the same already."""
    rec.action = "unchanged"
    await _reconcile_tags(rec, row, dry_run, t.row_tag if _frozen(row) else "")
    return rec


async def _flag_frozen(rec: RecordReport, row: MemoryRow, t: Target, dry_run: bool) -> RecordReport:
    """A pinned or attested (frozen) row of this document the document now contradicts:
    flagged, and the new body printed in the run's report under the row's identity (a
    pinned writer row refuses decision.record)."""
    rec.body.update(agent=row.agent, user_id=row.user_id, session_id=row.session_id)
    rec.note = (
        "pinned; the document now says otherwise; to accept this body, unpin the row "
        "with memory.unpin, then send it"
        if row.pinned
        else "attested; the document now says otherwise; to accept this body, send it "
        "and attest the new decision again with memory.verify"
    )
    rec.action = await _flag(rec, row, dry_run, t.row_tag)
    return rec


async def _frozen_restatement(
    org: str, namespace: str, key: str, inp: DecisionRecordInput
) -> MemoryRow | None:
    """A frozen decision row (pinned or attested) under another key that reads as this
    decision: of the decision hits at or above the namespace's conflict threshold, the
    most similar one with a frozen row."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.conflicts import resolve_conflict_floor
    from server.memory.app.search import handle_memory_search
    from server.memory.app.search.scoring import one_per_key, per_key_fetch
    from server.settings import settings

    # Cosine only, so cheap; read once so the fetch and the walk agree.
    top_k = settings.memory_decision_extract_restatement_top_k
    statement = memory_value(
        {
            "scenario": inp.scenario,
            "outcome": inp.outcome,
            "reasoning": inp.reasoning,
            "alternatives": inp.alternatives,
        },
        proposed=inp.stage == "proposed",
    )
    res = await handle_memory_search(
        MemorySearchInput(
            org=org,
            namespace=namespace,
            agent="*",
            query=statement,
            # a row per writer and session: enough of them for top_k keys
            top_k=per_key_fetch(top_k),
            memory_type="decision",
            namespace_scope="this",
            auto_touch=False,
            # Cosine alone, no model call: the similarity is compared to the conflict
            # threshold, which is a cosine, and the run pays for no rerank or HyDE.
            search_mode="semantic",
            rerank="none",
            hyde=False,
            retrieval_budget="fast",
        )
    )
    threshold, _ = await resolve_conflict_floor(org, namespace, "decision")
    close = [h for h in res.results if h.memory_key != key and h.similarity >= threshold]
    close.sort(key=lambda h: h.similarity, reverse=True)
    # _rows_of_key reads every row of a key: read each key once, at its most similar hit
    for hit in one_per_key(close, namespace, top_k):
        # Every identity's row of the key, read exactly: a frozen one may sit beside a
        # newer one that is not.
        for row in await _rows_of_key(org, namespace, hit.memory_key):
            if row.memory_type == "decision" and _frozen(row):
                return row
    return None


async def _rows_of_key(org: str, namespace: str, key: str) -> list[MemoryRow]:
    """The live rows of `key` under every identity: who holds it, then those rows whole.
    A row with no recorded actor (written before rows carried one) is not listed."""
    holders = await actors_by_key(org, [(namespace, key)], scope=WHOLE_NAMESPACE)
    idents = [
        (org, namespace, h["agent"], h["user_id"], h["session_id"], key)
        for h in holders.get((namespace, key), [])
    ]
    return list((await memory_rows_by_identity_keys(idents)).values())


async def resolve_mentions(
    org: str, namespace: str, cand: dx.Candidate, texts: list[str]
) -> tuple[list[str], list[str], list[str], bool]:
    """(files, signal lines, unresolved names, complete): each code name the document text
    holds, looked up with code.symbol; a name with exactly one defining file adds that file,
    unless the file is a test or a Markdown page. `complete` is False when a lookup could not
    read the index."""
    mentions = dx.sort_mentions(cand.mentions, "\n".join(texts))
    files: list[str] = []
    signals: list[str] = []
    complete = True
    unresolved = [f"{name} ({kind})" for name, kind in mentions.unresolved]
    for name, signal in mentions.symbols:
        definitions = await symbol_definitions(org, namespace, name)
        path = dx.definition_path(definitions or [])
        if definitions is None:
            complete = False
            signals.append(f"{name}: {signal}, unknown (code index unavailable)")
        elif definitions and not any(dx.governs(p) for p, _ in definitions):
            line = f"{name}: {signal}, only a test or a page defines it"
            if not dx.looks_like_setting(name):
                # A test or a page names code the index does not show: a human adds the file.
                unresolved.append(f"{name} (only tests or pages)")
            elif any(is_markdown_path(p) for p, _ in definitions):
                # A page writes about a setting (a test fixture may share its name).
                unresolved.append(f"{name} (setting)")
                line += "; reported as a setting"
            # else: a snake_case name only tests define is a helper.
            signals.append(line)
        elif path and dx.governs(path):
            signals.append(f"{name}: {signal}, exact -> {path}")
            if path not in files:
                files.append(path)
        elif definitions:
            # Defined in several files: code all the same, for a human to pick the file.
            unresolved.append(f"{name} (several definitions)")
            signals.append(f"{name}: {signal}, no single definition")
        elif dx.looks_like_setting(name):
            # A backticked snake_case name the index does not define: likely a setting.
            unresolved.append(f"{name} (setting)")
            signals.append(f"{name}: {signal}, no definition; reported as a setting")
        else:
            signals.append(f"{name}: {signal}, no definition")
    return files, signals, unresolved, complete


async def symbol_definitions(org: str, namespace: str, name: str) -> list[tuple[str, str]] | None:
    """The (path, qualified name) definitions of `name` in the namespace's code index,
    [] when none, None when the index could not be read."""
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import handle_code_symbol

    try:
        out = await handle_code_symbol(
            CodeSymbolInput(
                org=org,
                namespace=namespace,
                name=name,
                include_body=False,
                include_history=False,
                include_examples=False,
                include_lessons=False,
                consumer=EXTRACTOR_AGENT,
            )
        )
    except Exception as e:  # noqa: BLE001 — a lookup failure leaves the file out, never the record
        log.warning("decisions extract: code.symbol %s failed: %s", name, e)
        return None
    return [(u.path, u.symbol) for u in out.definitions]


def _tags_for(row: MemoryRow, *, source_changed: bool, repo_tag: str) -> list[str]:
    """The row's tags with `source-changed` set or dropped and `repo_tag` added, unless the
    row is linked to another repository already (another writer's row may be)."""
    tags = [t for t in row.tags if t != SOURCE_CHANGED_TAG]
    if repo_tag and not any(t.startswith("repo:") for t in tags):
        tags.append(repo_tag)
    return [*tags, SOURCE_CHANGED_TAG] if source_changed else tags


def _tag_note(row: MemoryRow, tags: list[str], dry_run: bool) -> str:
    """What a tag write on a kept row does, for its report line."""
    notes = []
    if SOURCE_CHANGED_TAG in row.tags and SOURCE_CHANGED_TAG not in tags:
        notes.append("would clear source-changed" if dry_run else "source-changed cleared")
    for tag in tags:
        if tag.startswith("repo:") and tag not in row.tags:
            notes.append(f"would tag {tag}" if dry_run else f"tagged {tag}")
    return "; ".join(notes)


async def _reconcile_tags(
    rec: RecordReport, row: MemoryRow, dry_run: bool, repo_tag: str, *, judged: bool = True
) -> None:
    """On a row the document still states: drop its `source-changed` tag and add the
    repository's tag. A row the run does not record again gets the tag this way, so the
    pre-edit block serves it too. One not `judged` keeps its `source-changed` tag."""
    flag = not judged and SOURCE_CHANGED_TAG in row.tags
    tags = _tags_for(row, source_changed=flag, repo_tag=repo_tag)
    if set(tags) == set(row.tags):
        return
    if not dry_run and (moved := await _set_tags(row, tags)):
        rec.race = moved  # reported once, in `races`
        return
    note = _tag_note(row, tags, dry_run)
    rec.note = f"{rec.note}; {note}" if rec.note else note


async def _flag(rec: RecordReport, row: MemoryRow, dry_run: bool, repo_tag: str) -> Action:
    """Tag a row the document now contradicts `source-changed`, and add the repository's
    tag; the action it reports."""
    tags = _tags_for(row, source_changed=True, repo_tag=repo_tag)
    if set(tags) == set(row.tags):
        return "already_flagged"
    if not dry_run and (moved := await _set_tags(row, tags)):
        rec.race = moved
        return "row_changed"
    if note := _tag_note(row, tags, dry_run):
        rec.note = f"{rec.note}; {note}" if rec.note else note
    # The tag was there already when only the repository's tag was missing.
    return "already_flagged" if SOURCE_CHANGED_TAG in row.tags else "flagged"


async def _set_tags(row: MemoryRow, tags: list[str]) -> str:
    """Write a row's tags, tags only, so the pin and the value stay: '' when written,
    "deleted" or "rewritten" when the row was deleted or rewritten since the run read it (a
    race). The write is a new version by the extractor: the row's actor and age move to
    it. By its memory_id: memory.update reads a `-` user or session as any, so a key two
    identities hold would be ambiguous there."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.app.write import served_sha256

    # Conditional on the value read, so a value changed since is not tagged from the old one;
    # the hash covers the value only, so a tags-only edit since the read is overwritten.
    try:
        out = await handle_memory_update_by_id(
            MemoryUpdateByIdInput(
                org=row.org,
                memory_id=row.memory_id,
                tags=tags,
                char_limit=row.char_limit,  # None would drop the row's cap
                if_match_sha256=served_sha256(row),
            )
        )
    except HTTPException as e:
        # A hash mismatch carries the live hash; a busy key's refusal does not.
        if isinstance(e.detail, dict) and "current_sha256" in e.detail:
            return "rewritten"
        raise
    return "" if out.updated else "deleted"


async def _audit(op_name: str, inp: Any) -> None:
    """The audit row the transport writes after a successful call of an op declaring
    `audit` with no `audit_subject` (decision.record, memory.delete_version), written here
    since an in-process call has no transport: the op's name, the writer actor
    (`process:cli:decisions`) and the input. Best effort, as there."""
    from server.memory.app.provenance import writer_actor
    from server.observability import audit

    await audit.emit(
        op_name,
        org=inp.org,
        namespace=inp.namespace,
        actor=writer_actor("-"),
        subject=inp.model_dump(mode="json"),
    )


async def _extractor_rows(
    org: str, namespace: str, doc_namespace: str, source_uri: str, *, first: bool = False
) -> list[MemoryRow]:
    """The extractor's live decision rows in `namespace` citing this document (in
    `doc_namespace`), or only the first one found with `first`, whole: listed a page at a
    time (the listing carries no source), each page read back in one bulk read. The cost
    grows with every decision the extractor holds in the namespace, not with this
    document's: no store read filters on `source_resource_id`."""
    ident = Identity(
        org=org, namespace=namespace, agent=EXTRACTOR_AGENT, user_id="-", session_id="-"
    )
    out: list[MemoryRow] = []
    offset = 0
    while True:
        page = await list_by_namespace(
            ident,
            scope=WHOLE_NAMESPACE,
            memory_type="decision",
            order_by="memory_key_asc",
            limit=_PAGE,
            offset=offset,
            preview=True,  # only the keys are used; the rows are read whole below
        )
        offset += len(page)
        rows = await get_many_by_keys(
            ident, [r.memory_key for r in page], scope=WHOLE_NAMESPACE, strong=True
        )
        for row in rows:
            if await cites_document(org, row.source_resource_id, doc_namespace, source_uri):
                out.append(row)
                if first:
                    return out
        if len(page) < _PAGE:
            return out


async def _retire(
    report: ExtractReport,
    own: list[tuple[MemoryRow, dict[str, Any]]],
    state: _Kept,
    written: set[str],
    repo_tag: str,
    may_write: Callable[[], bool] | None,
    writes: TaskSet,
) -> None:
    """Contract the extractor's own rows on this document that the support check found no
    longer stated: delete one that is not frozen; tag a frozen one (pinned or attested),
    or one with a recorded outcome, `source-changed`. An unfrozen one that this run
    recorded under another key (its heir) is deleted too, so the decision is not served
    twice. A row still stated stays, even when this run's model did not emit it again,
    and so does a row the check gave no verdict on. The step never drops a
    `source-changed` tag; reconcile drops it when the row is still stated again. A row it
    keeps gains `repo_tag`, as a row reconcile keeps does."""
    for row, record in own:
        if row.memory_key in written:
            continue
        if not _may_go_on(report, may_write):
            return
        try:
            await _whole(writes, _retire_one(report, row, record, state, repo_tag))
        except _REPORTED_ERRORS as e:
            report.refused.append(f"{row.memory_key}: {_refusal(e)}")


async def _retire_one(
    report: ExtractReport,
    row: MemoryRow,
    record: dict[str, Any],
    state: _Kept,
    repo_tag: str,
) -> None:
    """The retire step for one of the extractor's rows and its own record; its writes
    raise to the caller, which reports them."""
    key = row.memory_key
    still = state.still.get(key)
    heir = state.heirs.get(key, "")
    # Replaced: no longer stated, and this run wrote the decision under another key in its
    # place. A row with a recorded outcome is kept: deleting it would lose the outcome.
    if (
        still is False
        and heir in state.recorded
        and not _frozen(row)
        and not svc.has_outcome(record)
    ):
        if await _delete_row(report, row):
            report.replaced.append(f"{key} -> {heir}")
        return
    if still is False:
        # A row with a recorded outcome is tagged, never deleted, like a pinned one.
        keep = _frozen(row) or svc.has_outcome(record)
        await _retire_row(report, row, keep=keep, repo_tag=repo_tag)
    elif repo_tag:
        # Still stated, or not judged, though the model did not emit it: it stays, linked.
        await _retire_tags(report, row, SOURCE_CHANGED_TAG in row.tags, repo_tag)


async def _retire_row(report: ExtractReport, row: MemoryRow, *, keep: bool, repo_tag: str) -> None:
    """Delete the extractor's row by its writer, or tag it `source-changed` (and
    `repo_tag`) when it is to be kept. Reported only once written: a refused write is
    reported as refused instead."""
    if not keep:
        if await _delete_row(report, row):
            report.retired.append(row.memory_key)
        return
    await _retire_tags(report, row, True, repo_tag)


async def _retire_tags(
    report: ExtractReport, row: MemoryRow, source_changed: bool, repo_tag: str
) -> None:
    """Set the tags of a row the retire step keeps (`_tags_for`), if they change. Once
    written, its key is reported in `flagged` when it gained `source-changed` and in
    `linked` when it gained a repository's tag; a write that lost a race, in `races`."""
    tags = _tags_for(row, source_changed=source_changed, repo_tag=repo_tag)
    if set(tags) == set(row.tags):
        return
    if not report.dry_run and (moved := await _set_tags(row, tags)):
        report.races.append(f"{row.memory_key}: {moved} since the run read it")
        return
    if SOURCE_CHANGED_TAG in tags and SOURCE_CHANGED_TAG not in row.tags:
        report.flagged.append(row.memory_key)
    if any(t.startswith("repo:") and t not in row.tags for t in tags):
        report.linked.append(row.memory_key)


async def _delete_row(report: ExtractReport, row: MemoryRow) -> bool:
    """Delete the extractor's row at the version read (nothing in a dry run); False when
    it was deleted or rewritten since, which is reported."""
    from server.memory.api.models_crud import MemoryDeleteVersionInput
    from server.memory.app.handlers import handle_memory_delete_version

    if report.dry_run:
        return True
    inp = MemoryDeleteVersionInput(
        org=row.org,
        namespace=row.namespace,
        agent=EXTRACTOR_AGENT,
        memory_key=row.memory_key,
        version=row.version,
    )
    out = await handle_memory_delete_version(inp)
    if out.tombstoned:
        await _audit("memory.delete_version", inp)
    else:
        # Gone, or rewritten since it was read: the next run judges what is there.
        moved = "deleted" if not out.found else "rewritten"
        report.races.append(f"{row.memory_key}: {moved} since the run read it")
    return bool(out.tombstoned)
