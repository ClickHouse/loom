"""`loom decisions extract`: mine one knowledge document into decision records.

Each window of the document's live chunks goes to the model once, and once more when its
reply holds no decision or a reason that is not one span of the text. A candidate that is
not the document's own words, whose reasoning only restates its outcome (a ban stated
with no reason aside), or whose quote is in a recap section, is dropped
(`server.memory.domain.decision_extract`). A kept candidate is reconciled against what
the namespace already holds, then written through `decision.record` under one fixed
identity, so a re-run revises the extractor's own rows instead of writing beside them:

- the key's active record has the same verbatim fields (reasoning, rejected options'
  reasons; the rest is the model's wording) and its row carries the same files, source
  and constraint tag: nothing is written;
- it differs and its row is the extractor's, unconfirmed and from this document:
  recorded again (Revise), unless its outcome is recorded: then it is tagged
  `source-changed`;
- it differs and its row is confirmed (pinned, or attested in its current form) and
  from this document: nothing is written, the body is printed for the human who
  confirmed it, and the row is tagged `source-changed`, a tag a later run drops once the
  document says the same again;
- it differs and its row is another writer's or another document's: nothing is written;
- no record under the key, but a confirmed decision row (pinned or attested) reads the
  same at or above the conflict threshold: skipped as already recorded.

After the writes, the extractor deletes its unconfirmed rows citing this document whose
reasoning (a span of the document, whitespace aside) is no longer in its text, or whose
outcome and reasoning this run recorded under another key. A row it keeps (confirmed, or
with a recorded outcome) whose reasoning left is tagged `source-changed` instead. A
refused write is reported and the run goes on. Runs in-process, with no auth context:
operator only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from server.core.errors import LoomServerError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_decision import DecisionRecordInput
from server.memory.app import decisions as svc
from server.memory.domain import decision_extract as dx
from server.memory.domain.corpus_synthesis import CorpusChunk, Window, windows
from server.memory.domain.decision_schema import SOURCE_CHANGED_TAG
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
from server.settings.budgets import llm_max_tokens

log = logging.getLogger(__name__)

# One identity for every row the extractor writes: `loom.memory` keys rows by identity, so
# a key written from another would be a second live row beside the first.
EXTRACTOR_AGENT = "loom-decisions"
DESIGN_DOC_TAG = "source-kind:design_doc"
CONSTRAINT_TAG = "constraint"
# A model's reading of a document, until a human pins it.
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
    # The extractor's own row on this document now holds the decision (written, revised,
    # or unchanged and ours), so the retire step may count it as the reworded one's heir.
    holds: bool = False
    # The outcome that row's record holds: the candidate's when written or revised, the
    # stored one when unchanged (the model may word it anew).
    held_outcome: str = ""
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


@dataclass
class Retried:
    """A second read of a window, or its loss: the section it is reported under and why."""

    section: str
    reason: str


@dataclass
class ExtractReport:
    source_uri: str
    dry_run: bool
    # Why the run read nothing and retired nothing: no document, no chunks for the current
    # embedding model, no usable model, the extractor's rows unreadable, or no window read.
    error: str = ""
    windows: int = 0
    records: list[RecordReport] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    image_only: list[str] = field(default_factory=list)
    # A second read of a window, or its loss, and why: its reply held no decision, or a
    # reason that is not one span of the text. A window gets at most one extra call.
    retried: list[Retried] = field(default_factory=list)
    # Keys deleted (or, in a dry run, to delete): the extractor's rows whose text left.
    retired: list[str] = field(default_factory=list)
    # "<old key> -> <key>" for each extractor row deleted (or to delete) because this run
    # recorded its reasoning under another key: the model worded the scenario anew.
    replaced: list[str] = field(default_factory=list)
    # Keys tagged (or to tag) `source-changed`: confirmed rows (pinned or attested), and
    # rows with a recorded outcome, whose document now says otherwise.
    flagged: list[str] = field(default_factory=list)
    # The retire step's "<key>: refused: <code>" for a read or write the server refused,
    # "<key>: failed: <error>" for one a provider error stopped. A candidate's is its
    # `dropped` line, marked `refused`.
    refused: list[str] = field(default_factory=list)
    # "<key>: deleted since the run read it" or "<key>: rewritten since the run read it":
    # a write that found its row gone or moved on, a race the next run settles.
    races: list[str] = field(default_factory=list)


def _confirmed(row: MemoryRow) -> bool:
    """A person or a service account stood behind the row: pinned, or attested in its
    current form, read as memory.get reads it; the rule memory.for_file marks
    `(unconfirmed)` by."""
    current = row_content_sha256(row)
    state = verification_state(row.verified_by, row.verified_sha256, current)
    return confirmed(row.pinned, state)


def _has_outcome(record: dict[str, Any]) -> bool:
    """decision.outcome recorded what happened: a re-record or a delete would lose it."""
    return bool(str(record.get("realized_outcome") or "").strip())


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


def _unread_reply(stop_reason: str) -> str:
    """Why a model reply could not be read as the candidates' JSON."""
    if stop_reason in ("length", "max_tokens"):
        return "reply cut at the token limit; lower --window-chars or --max-per-section"
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


def _no_model_error() -> str:
    """Why the run must not start: it would read the document with the fake model, whose
    decisions pass the verbatim check by construction and would be recorded as the
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


def _refusal(e: BaseException) -> str:
    if isinstance(e, LoomServerError):
        return f"refused: {e.code}"
    if isinstance(e, HTTPException):
        detail: dict[str, Any] = e.detail if isinstance(e.detail, dict) else {}
        return f"refused: {detail.get('code') or e.status_code}"
    return f"failed: {type(e).__name__}"


async def extract_document(
    org: str,
    namespace: str,
    source_uri: str,
    *,
    dry_run: bool,
    max_per_section: int = 3,
    window_chars: int = 6000,
) -> ExtractReport:
    """Mine `source_uri` into decision records in `namespace`; `dry_run` writes nothing
    and reports what a run would write."""
    from server.knowledge.api.models_knowledge import KnowledgeGetInput
    from server.knowledge.app.document_reads import document_chunks, handle_knowledge_get

    report = ExtractReport(source_uri, dry_run)
    if error := _no_model_error():
        report.error = error
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
    live_texts = [doc.content or "", *(c.text for c in chunks)]
    report.image_only = dx.image_only_sections(doc.content or "")
    # A section judged image-only is not sent. An empty-path chunk of an image-only
    # preamble is skipped only when its own text is image-only too.
    skipped = set(report.image_only)

    def sent(c: CorpusChunk) -> bool:
        if dx.no_words(c.text):
            return False
        if dx.breadcrumb(c.section_path) not in skipped:
            return True
        return not c.section_path and not dx.image_only(c.text)

    wins = windows([c for c in chunks if sent(c)], window_chars=window_chars)
    report.windows = len(wins)
    # One scan of the extractor's rows on this document and their records, for the prompt
    # and the retire step; rows this run writes are skipped by the retire step.
    try:
        own = await _own_records(org, namespace, await _extractor_rows(org, namespace, source_uri))
    except _REPORTED_ERRORS as e:
        # Before any model call: a run that cannot see its own rows reads nothing.
        report.error = f"could not read the extractor's rows: {_refusal(e)}"
        return report
    known = _known_decisions(own)
    kept, answered = await _candidates(
        org, namespace, source_uri, wins, max_per_section, report, known
    )
    if wins and not answered:
        # A run that read nothing must not retire anything.
        report.error = (
            "no window was read: every model call failed, was answered by the fake model, "
            "came back unreadable, or answered nothing and failed when read again"
        )
        return report
    written: set[str] = set()
    # (outcome, reasoning) -> key of each decision this document now holds, for the retire
    # step: the same outcome on the same verbatim reasoning is the same decision, however
    # the model worded its scenario and category.
    held: dict[tuple[str, str], str] = {}
    for cand, section, texts in kept:
        try:
            rec = await _reconcile(org, namespace, source_uri, cand, section, texts, dry_run)
        except _REPORTED_ERRORS as e:
            # A blacklisted or newly pinned decision, a busy key, a store that timed out:
            # the other candidates and the retire step still run, and a re-run does not
            # stop at the same place.
            key = svc.decision_key(cand.category, cand.scenario)
            reason = f"{key}: {_refusal(e)}"
            report.dropped.append(Dropped(section, reason, cand.quote, refused=True))
            # Handled this run: the retire step must not judge the key it just failed on.
            written.add(key)
            continue
        written.add(rec.memory_key)
        if rec.race:
            report.races.append(f"{rec.memory_key}: {rec.race} since the run read it")
        if rec.action == "row_changed":
            continue
        report.records.append(rec)
        if rec.action == "flagged":
            report.flagged.append(rec.memory_key)
        if rec.holds:
            held.setdefault((rec.held_outcome, collapse_ws(cand.reasoning)), rec.memory_key)
    await _retire(report, own, live_texts, written, held)
    return report


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
    """(category, scenario, outcome) of the extractor's decisions on this document, pinned
    or not, for the prompt: a model told how a decision was worded before reuses the
    wording, so its key and its outcome stay put across runs. At most
    memory_decision_extract_known_max of them, to bound the prompt."""
    from server.settings import settings

    known = [
        (
            str(record.get("category") or ""),
            str(record.get("scenario") or ""),
            str(record.get("outcome") or ""),
        )
        for _row, record in own
    ]
    return known[: settings.memory_decision_extract_known_max]


async def _read_window(
    llm: Any,
    system: str,
    w: Window,
    section: str,
    report: ExtractReport,
    *,
    fake_chosen: bool,
    note: str = "",
    answered_already: bool = False,
) -> list[dx.Candidate] | None:
    """The model's candidates for one window, or None when the read is lost: the call
    failed, the fake model answered for a real provider, or the reply is unreadable; each
    is a dropped line marked `refused`, a read the run needed, reported under `section`.
    `note` goes before the window's text. When the window is `answered_already` (a second
    read for what the first reply got wrong), a lost read is a `read again` line under
    `section`, never refused."""
    from server.llm.json_text import strip_code_fence

    user = f"{note}\n\n{dx.user_prompt(w)}" if note else dx.user_prompt(w)

    def lost(reason: str) -> None:
        if answered_already:
            report.retried.append(Retried(section, f"the second read was lost: {reason}"))
        else:
            report.dropped.append(Dropped(section, reason, refused=True))

    try:
        with llm_stage("decision_extract"):
            res = await llm.complete(
                system,
                user,
                max_tokens=llm_max_tokens("decision_extract"),
                response_format={"type": "json_object"},
            )
    except (httpx.HTTPError, TimeoutError, LoomServerError) as e:
        # A provider error, a timeout or a refused call loses this window only.
        lost(f"the model call failed: {type(e).__name__}")
        return None
    if not fake_chosen and res.provider == "fake":
        # A fallback with no key answered: its decisions pass the check by construction.
        lost(_FAKE_ANSWER)
        return None
    cands = dx.parse_candidates(strip_code_fence(res.text))
    if cands is None:
        lost(_unread_reply(res.stop_reason))
    return cands


@dataclass
class _Kept:
    """The candidates kept so far in a run, and what a later one is checked against."""

    max_per_section: int
    # (candidate, its section, the texts it was checked against)
    kept: list[tuple[dx.Candidate, str, list[str]]] = field(default_factory=list)
    per_section: dict[str, int] = field(default_factory=dict)
    keys: set[str] = field(default_factory=set)
    pairs: set[tuple[str, str]] = field(default_factory=set)  # (outcome, reasoning)


_EMPTY_REPLY = "the model's reply held no decision; the window was read once more"
_STITCHED = "a reason is not one span of the text; the window was read once more for those"


async def _candidates(
    org: str,
    namespace: str,
    source_uri: str,
    wins: list[Window],
    max_per_section: int,
    report: ExtractReport,
    known: list[tuple[str, str, str]],
) -> tuple[list[tuple[dx.Candidate, str, list[str]]], int]:
    """(candidate, its section, the texts it was checked against) for every candidate
    that passes the checks of `_check_window` (verbatim spans, restatement with a
    reason-less ban kept, recap sections, duplicates, fit), its spans the document's own
    and its inverted rejected options removed, at most `max_per_section` per section;
    and how many windows the model answered with a readable reply.

    A window gets at most one extra call: when its reply holds no decision though its
    text is more than a caption (the second reply is the window's), or when a reason is
    not one span of the text while its quote is (the second reply adds to the first)."""
    from server.llm.client import get_llm_client

    llm = get_llm_client()
    fake_chosen = _fake_chosen()
    system = dx.system_prompt(max_per_section, known)
    state = _Kept(max_per_section)
    answered = 0
    # An operator command: its calls are not gated by the namespace's cost cap.
    for w in wins:
        first = dx.breadcrumb(w.chunks[0].section_path)
        cands = await _read_window(llm, system, w, first, report, fake_chosen=fake_chosen)
        retried = cands == [] and dx.more_than_a_caption("\n\n".join(c.text for c in w.chunks))
        if retried:
            report.retried.append(Retried(first, _EMPTY_REPLY))
            cands = await _read_window(llm, system, w, first, report, fake_chosen=fake_chosen)
        if cands is None:
            continue
        answered += 1
        before = len(state.kept)
        failed = _check_window(org, namespace, source_uri, w, cands, state, report)
        if failed and not retried:
            # Named by the first failed decision's section, not the window's first.
            section = failed[0][2]
            report.retried.append(Retried(section, _STITCHED))
            note = dx.stitched_note([(outcome, span) for outcome, span, _ in failed])
            again = await _read_window(
                llm,
                system,
                w,
                section,
                report,
                fake_chosen=fake_chosen,
                note=note,
                answered_already=True,
            )
            repaired = _repairs(w, again or [], failed, state.kept[before:], report)
            _check_window(org, namespace, source_uri, w, repaired, state, report)
    return state.kept, answered


_REPEATED = "the second read repeats a decision the first reply gave"


def _repairs(
    w: Window,
    again: list[dx.Candidate],
    failed: list[tuple[str, str, str]],
    kept: list[tuple[dx.Candidate, str, list[str]]],
    report: ExtractReport,
) -> list[dx.Candidate]:
    """The second read's candidates that repair a failed one: a candidate with a failed
    outcome, or one whose reasoning none of the window's kept candidates has. A kept
    decision given again, its scenario reworded, would be written twice."""
    outcomes = {collapse_ws(outcome) for outcome, _span, _section in failed}
    reasonings = {collapse_ws(c.reasoning) for c, _section, _texts in kept}
    texts = [c.text for c in w.chunks]
    out: list[dx.Candidate] = []
    for cand in again:
        if collapse_ws(cand.outcome) in outcomes or collapse_ws(cand.reasoning) not in reasonings:
            out.append(cand)
        else:
            section = dx.quote_section(w, dx.find_span(cand.quote, texts) or cand.quote)
            report.dropped.append(Dropped(section, _REPEATED, cand.quote))
    return out


def _check_window(
    org: str,
    namespace: str,
    source_uri: str,
    w: Window,
    cands: list[dx.Candidate],
    state: _Kept,
    report: ExtractReport,
) -> list[tuple[str, str, str]]:
    """Check one reply's candidates, keeping each that passes in `state` and reporting
    each dropped; the (outcome, span, section) of each whose reason span is not in the
    text while its quote is, for a second call."""
    texts = [c.text for c in w.chunks]
    failed: list[tuple[str, str, str]] = []
    for cand in cands:
        reason = dx.verbatim_failure(cand, texts)
        span = dx.unfound_reason(cand, texts) if reason else ""
        if not reason:
            cand = dx.document_spans(cand, texts)
            # A reason-less ban is kept as one; any other restatement is dropped.
            if dx.reasonless_ban(cand):
                cand = dx.without_reason(cand)
            elif dx.restates_outcome(cand):
                reason = dx.RESTATED
        # The quote as the chunk writes it, so a re-wrapped one still names its section.
        section = dx.quote_section(w, dx.find_span(cand.quote, texts) or cand.quote)
        if dx.in_recap(section):
            reason, span = reason or dx.RECAP, ""
        if span:
            failed.append((cand.outcome, span, section))
        key = ""
        if not reason:
            reason, key = _duplicate_or_unfit(org, namespace, source_uri, cand, section, state)
        if reason:
            report.dropped.append(Dropped(section, reason, cand.quote))
            continue
        cand = _drop_inverted(cand, section, report)
        state.per_section[section] = state.per_section.get(section, 0) + 1
        state.keys.add(key)
        state.pairs.add((cand.outcome, collapse_ws(cand.reasoning)))
        state.kept.append((cand, section, texts))
    return failed


def _duplicate_or_unfit(
    org: str, namespace: str, source_uri: str, cand: dx.Candidate, section: str, state: _Kept
) -> tuple[str, str]:
    """(why a candidate that passed the text checks is still dropped, or ''; its key)."""
    try:
        key = svc.decision_key(cand.category, cand.scenario)
    except ValueError:
        # The key keeps letters and digits a-z, 0-9 only (a CJK scenario has none).
        return "no letters or digits a-z, 0-9 to build a key from", ""
    if key in state.keys:
        return "same key as an earlier candidate", key
    if (cand.outcome, collapse_ws(cand.reasoning)) in state.pairs:
        # Overlapping chunks feed one span to two windows, worded apart.
        return "same decision as an earlier candidate", key
    if not _fits(org, namespace, source_uri, cand, section):
        # Checked before it takes a slot, a key or a code lookup.
        return "does not fit decision.record", key
    if state.per_section.get(section, 0) >= state.max_per_section:
        return f"the section already has --max-per-section {state.max_per_section}", key
    return "", key


def _drop_inverted(cand: dx.Candidate, section: str, report: ExtractReport) -> dx.Candidate:
    """`cand` without the rejected options the text states as part of the choice, each
    reported; the decision itself stays."""
    inverted = dx.inverted_options(cand)
    for option in inverted:
        reason = f"rejected option {option!r} left out: the quote states it as part of the choice"
        report.dropped.append(Dropped(section, reason, cand.quote))
    if not inverted:
        return cand
    kept = [(o, r) for o, r in cand.alternatives if o not in inverted]
    return replace(cand, alternatives=kept)


def _record_input(
    org: str, namespace: str, source_uri: str, cand: dx.Candidate, section: str, files: list[str]
) -> DecisionRecordInput:
    """The decision.record input for a candidate; raises ValidationError when it does not
    fit the op's field limits (`_fits` checks first)."""
    tags = [DESIGN_DOC_TAG, *([CONSTRAINT_TAG] if cand.is_constraint else [])]
    return DecisionRecordInput(
        org=org,
        namespace=namespace,
        agent=EXTRACTOR_AGENT,
        category=cand.category,
        scenario=cand.scenario,
        reasoning=cand.reasoning,
        outcome=cand.outcome,
        # As decision.record keeps them: deduplicated, at most MAX_ALTERNATIVES.
        alternatives=svc.clean_alternatives(cand.rejected(), cut=False),
        confidence=EXTRACTED_CONFIDENCE,
        tags=tags,
        files=files,
        source_class="derived",
        source_resource_id=source_resource_id(namespace, source_uri, section),
    )


def _fits(org: str, namespace: str, source_uri: str, cand: dx.Candidate, section: str) -> bool:
    """The candidate fits decision.record's field limits. Checked with no files: the files
    a run adds pass through normalize_files, capped at the FileRefs limits, so they never
    make a fitting candidate unfit."""
    try:
        _record_input(org, namespace, source_uri, cand, section, [])
    except ValidationError:
        return False
    return True


def record_body(inp: DecisionRecordInput) -> dict[str, Any]:
    """The decision.record request a human can send as it is."""
    body = inp.model_dump(mode="json", exclude={"caused_by", "influenced_by", "valid_at"})
    return {
        k: v for k, v in body.items() if not (k in ("run_id", "decision_maker") and v in ("-", ""))
    }


def _same_reasons(stored: list[str], candidate: list[str]) -> bool:
    """Each stored reason is one of the candidate's, the two sets alike. A stored
    alternative cut at ALTERNATIVE_CHARS keeps only the start of its reason, and where the
    cut falls moves with the option's wording, so it matches as a prefix. One cut before
    its reason (inside the option or the marker) holds none, and matches whichever
    candidate reason is left."""
    # `candidate` is the input's list, already kept as decision.record keeps it, uncut.
    remaining = [a.rpartition(dx.REJECTED)[2] for a in candidate]
    if len(remaining) != len(stored):
        return False
    # Reasoned before reasonless, uncut before cut, the longest cut reason first.
    order = sorted(
        stored,
        key=lambda a: (
            dx.REJECTED not in a,
            len(a) >= svc.ALTERNATIVE_CHARS,
            -len(a.rpartition(dx.REJECTED)[2]),
        ),
    )
    for alt in order:
        reason = alt.rpartition(dx.REJECTED)[2]
        cut = len(alt) >= svc.ALTERNATIVE_CHARS
        if dx.REJECTED not in alt:
            match = remaining[0] if cut and remaining else None
        else:
            match = next(
                (r for r in remaining if r == reason or (cut and r.startswith(reason))), None
            )
        if match is None:
            return False
        remaining.remove(match)
    return True


def _same_decision(record: dict[str, Any], inp: DecisionRecordInput) -> bool:
    """The document still says what the record says: the verbatim fields, the reasoning
    and the rejected options' reasons, as decision.record stores them, are the same. The
    model words the category, scenario, outcome and options anew on each run; a change
    there alone is no change in the document. Nor is a change in whitespace: a re-wrapped
    line leaves every row as it is, the stored span still found by the retire step."""
    if collapse_ws(str(record.get("reasoning") or "")) != collapse_ws(inp.reasoning):
        return False
    return _same_reasons(alternatives(record), inp.alternatives)


def _same_row(row: MemoryRow, inp: DecisionRecordInput) -> bool:
    """The row carries the files, source and constraint tag the candidate would write."""
    return (
        sorted(row.files) == sorted(inp.files or [])
        and row.source_resource_id == inp.source_resource_id
        and (CONSTRAINT_TAG in row.tags) == (CONSTRAINT_TAG in inp.tags)
    )


async def _reconcile(
    org: str,
    namespace: str,
    source_uri: str,
    cand: dx.Candidate,
    section: str,
    texts: list[str],
    dry_run: bool,
) -> RecordReport:
    """Decide what the candidate does to the namespace, and do it unless `dry_run`."""
    files, signals, unresolved, complete = await resolve_mentions(org, namespace, cand, texts)
    # A path the document writes out (`src/Storages/MergeTreeData.cpp`) is a file too; the
    # explicit list turns off decision.record's own detection, so it is merged in here.
    files = normalize_files([*files, *detect_value_files(f"{cand.quote}\n{cand.reasoning}")])
    inp = _record_input(org, namespace, source_uri, cand, section, files)
    key = svc.decision_key(inp.category, inp.scenario)
    rec = RecordReport(key, "written", record_body(inp), signals, unresolved)
    writer = await live_writer_record(org, namespace, key)
    if writer is None:
        peer = await _confirmed_restatement(org, namespace, key, inp)
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
        if row is not None and not complete:
            # The code index did not answer for every name: keep the files the row has
            # rather than drop one an earlier run resolved.
            inp = inp.model_copy(update={"files": list(row.files)})
            rec.body["files"] = list(row.files)
        this_doc = row is not None and await cites_document(
            org, row.source_resource_id, namespace, source_uri
        )
        ours = this_doc and _is_extractor(writer.agent, writer.user_id, writer.session_id)
        record = svc.parse_record_json(writer.record_json)
        same = _same_decision(record, inp)
        # Our own row is revised for its files, source or constraint tag too: a document
        # extracted before its code was indexed gains its files on a later run.
        stale_row = row is not None and ours and not _confirmed(row) and not _same_row(row, inp)
        if same and not stale_row:
            rec.action = "unchanged"
            rec.holds = ours
            rec.held_outcome = str(record.get("outcome") or "").strip()
            if row is not None and this_doc:
                await _clear_if_tagged(rec, row, dry_run)
            return rec
        if row is not None and _confirmed(row) and this_doc:
            # The human who confirmed it decides; a pinned writer row refuses decision.record.
            rec.body.update(agent=row.agent, user_id=row.user_id, session_id=row.session_id)
            rec.note = (
                "pinned; the document now says otherwise; to accept this body, unpin the row "
                "with memory.unpin, then send it"
                if row.pinned
                else "attested; the document now says otherwise; to accept this body, send it "
                "and attest the new decision again with memory.verify"
            )
            rec.action = await _flag(rec, row, dry_run)
            return rec
        if not ours:
            rec.body.update(
                agent=writer.agent, user_id=writer.user_id, session_id=writer.session_id
            )
            rec.action = "not_ours"
            rec.note = (
                f"recorded by {writer.agent} from {row.source_resource_id or 'no document'}; "
                "the extractor revises only its own rows on this document"
                if row is not None
                else f"recorded by {writer.agent}; the extractor revises only its own rows"
            )
            return rec
        if row is not None and _has_outcome(record):
            # decision.record rebuilds the record from the body, so a re-record would drop
            # the outcome.
            rec.note = "its outcome is recorded; re-recording would lose it"
            rec.action = "outcome_known" if same else await _flag(rec, row, dry_run)
            if same:
                # Only its row fields are stale: the document says what it says again.
                await _clear_if_tagged(rec, row, dry_run)
            return rec
        rec.action = "revised"
    rec.holds = True
    rec.held_outcome = inp.outcome.strip()
    if not dry_run:
        from server.memory.app.decision import handle_decision_record

        # decision.record takes no precondition: a decision.outcome that lands between the
        # read above and this write is replaced by the new record, outcome and all.
        await handle_decision_record(inp)
        await _audit("decision.record", inp)
    return rec


async def _confirmed_restatement(
    org: str, namespace: str, key: str, inp: DecisionRecordInput
) -> MemoryRow | None:
    """A confirmed decision row (pinned or attested) under another key that reads as this
    decision: of the decision hits at or above the namespace's conflict threshold, the
    most similar one with a confirmed row."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.conflicts import resolve_conflict_floor
    from server.memory.app.search import handle_memory_search
    from server.memory.app.search.scoring import one_per_key, per_key_fetch
    from server.settings import settings

    # Cosine only, so cheap; read once so the fetch and the walk agree.
    top_k = settings.memory_decision_extract_restatement_top_k
    statement = memory_value(
        {
            "category": inp.category,
            "scenario": inp.scenario,
            "outcome": inp.outcome,
            "reasoning": inp.reasoning,
            "alternatives": inp.alternatives,
        }
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
        # Every identity's row of the key, read exactly: a confirmed one may sit beside a
        # newer unconfirmed one.
        for row in await _rows_of_key(org, namespace, hit.memory_key):
            if row.memory_type == "decision" and _confirmed(row):
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
    holds, looked up with code.symbol; a name with exactly one defining file adds that
    file. `complete` is False when a lookup could not read the index."""
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
        elif path:
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


async def _clear_if_tagged(rec: RecordReport, row: MemoryRow, dry_run: bool) -> None:
    """Drop a row's `source-changed` tag once the document says what the row says again."""
    if SOURCE_CHANGED_TAG not in row.tags:
        return
    note = "would clear source-changed" if dry_run else "source-changed cleared"
    if not dry_run and (moved := await _set_source_changed(row, False)):
        rec.race = moved  # reported once, in `races`
        return
    rec.note = f"{rec.note}; {note}" if rec.note else note


async def _flag(rec: RecordReport, row: MemoryRow, dry_run: bool) -> Action:
    """Tag a row the document now contradicts `source-changed`; the action it reports."""
    if SOURCE_CHANGED_TAG in row.tags:
        return "already_flagged"
    if not dry_run and (moved := await _set_source_changed(row, True)):
        rec.race = moved
        return "row_changed"
    return "flagged"


async def _set_source_changed(row: MemoryRow, on: bool) -> str:
    """Add or drop the `source-changed` tag of a row, tags only, so the pin and the value
    stay: '' when written, "deleted" or "rewritten" when the row was deleted or rewritten
    since the run read it (a race). The write is a new version by the extractor: the row's
    actor and age move to it. By its memory_id: memory.update reads a `-` user or session
    as any, so a key two identities hold would be ambiguous there."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.app.write import served_sha256

    tags = [t for t in row.tags if t != SOURCE_CHANGED_TAG]
    # Conditional on the value read, so a value changed since is not tagged from the old one;
    # the hash covers the value only, so a tags-only edit since the read is overwritten.
    try:
        out = await handle_memory_update_by_id(
            MemoryUpdateByIdInput(
                org=row.org,
                memory_id=row.memory_id,
                tags=[*tags, SOURCE_CHANGED_TAG] if on else tags,
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


async def _extractor_rows(org: str, namespace: str, source_uri: str) -> list[MemoryRow]:
    """The extractor's live decision rows citing this document, whole: listed a page at a
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
            if await cites_document(org, row.source_resource_id, namespace, source_uri):
                out.append(row)
        if len(page) < _PAGE:
            return out


async def _retire(
    report: ExtractReport,
    own: list[tuple[MemoryRow, dict[str, Any]]],
    live_texts: list[str],
    written: set[str],
    held: dict[tuple[str, str], str],
) -> None:
    """Contract the extractor's own rows on this document whose reasoning left its text:
    delete an unconfirmed one; tag a confirmed one (pinned or attested), or one with a
    recorded outcome, `source-changed`. An unconfirmed one whose outcome and reasoning
    this run holds under another key (`held`) is deleted too: the model worded the
    scenario anew, and the decision is not served twice. Another decision resting on the
    same reasoning has another outcome and stays. A row whose reasoning is still there
    stays, even when this run's model did not emit it again. The step never drops a
    `source-changed` tag: it sees the reasoning alone, and the tag may stand for another
    field the document still contradicts; reconcile drops it when the verbatim fields
    match again."""
    for row, record in own:
        if row.memory_key in written:
            continue
        try:
            await _retire_one(report, row, record, live_texts, held)
        except _REPORTED_ERRORS as e:
            report.refused.append(f"{row.memory_key}: {_refusal(e)}")


async def _retire_one(
    report: ExtractReport,
    row: MemoryRow,
    record: dict[str, Any],
    live_texts: list[str],
    held: dict[tuple[str, str], str],
) -> None:
    """The retire step for one of the extractor's rows and its own record; its writes
    raise to the caller, which reports them."""
    key = row.memory_key
    reasoning = str(record.get("reasoning") or "")
    if not reasoning:
        return
    restated = held.get((str(record.get("outcome") or "").strip(), collapse_ws(reasoning)))
    # A row with a recorded outcome is kept: deleting it would lose the outcome.
    if not _confirmed(row) and restated is not None and not _has_outcome(record):
        if await _delete_row(report, row):
            report.replaced.append(f"{key} -> {restated}")
        return
    # Whitespace aside, as the verbatim check reads it: a re-wrapped line is the same text.
    if not dx.find_span(reasoning, live_texts):
        # A row with a recorded outcome is tagged, never deleted, like a pinned one.
        keep = _confirmed(row) or _has_outcome(record)
        await _retire_row(report, row, keep=keep)


async def _retire_row(report: ExtractReport, row: MemoryRow, *, keep: bool) -> None:
    """Delete the extractor's row by its writer, or tag it `source-changed` when it is to
    be kept. Reported only once written: a refused write is reported as refused instead."""
    if keep:
        if SOURCE_CHANGED_TAG in row.tags:
            return
        if not report.dry_run and (moved := await _set_source_changed(row, True)):
            report.races.append(f"{row.memory_key}: {moved} since the run read it")
            return
        report.flagged.append(row.memory_key)
    elif await _delete_row(report, row):
        report.retired.append(row.memory_key)


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
