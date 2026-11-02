"""`loom decisions extract`: mine one knowledge document into decision records.

Each window of the document's live chunks goes to the model once; a candidate that is
not the document's own words is dropped (`server.memory.domain.decision_extract`). A
kept candidate is reconciled against what the namespace already holds, then written
through `decision.record` under one fixed identity, so a re-run revises the
extractor's own rows instead of writing beside them:

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
  same above the conflict threshold: skipped as already recorded.

After the writes, the extractor deletes its unconfirmed rows citing this document whose
reasoning (a verbatim span of the document) is no longer in its text, or whose outcome
and reasoning this run recorded under another key. A row it keeps (confirmed, or with a
recorded outcome) whose reasoning left is tagged `source-changed` instead. A refused
write is reported and the run goes on. Runs in-process, with no auth context: operator
only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_decision import DecisionRecordInput
from server.memory.app import decisions as svc
from server.memory.domain import decision_extract as dx
from server.memory.domain.corpus_synthesis import Window, windows
from server.memory.domain.decision_statement import (
    SOURCE_CHANGED_TAG,
    alternatives,
    memory_value,
)
from server.memory.domain.trust import confirmed, verification_state
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

log = logging.getLogger(__name__)

# One identity for every row the extractor writes: `loom.memory` keys rows by identity, so
# a key written from another would be a second live row beside the first.
EXTRACTOR_AGENT = "loom-decisions"
DESIGN_DOC_TAG = "source-kind:design_doc"
CONSTRAINT_TAG = "constraint"
# A model's reading of a document, until a human pins it.
EXTRACTED_CONFIDENCE = 0.5
_REPLY_TOKENS = 4000
_PAGE = 500


@dataclass
class RecordReport:
    memory_key: str
    # written | revised | unchanged | flagged | already_flagged | not_ours | outcome_known |
    # already_recorded (a refused tag write, row_changed, goes to `dropped` instead)
    action: str
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


@dataclass
class Dropped:
    section: str
    reason: str
    quote: str = ""


@dataclass
class ExtractReport:
    org: str
    namespace: str
    source_uri: str
    dry_run: bool
    # Why nothing was extracted: no document, or no chunks for the current embedding model.
    error: str = ""
    windows: int = 0
    records: list[RecordReport] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    image_only: list[str] = field(default_factory=list)
    # Keys deleted (or, in a dry run, to delete): the extractor's rows whose text left.
    retired: list[str] = field(default_factory=list)
    # "<old key> -> <key>" for each extractor row deleted (or to delete) because this run
    # recorded its reasoning under another key: the model worded the scenario anew.
    replaced: list[str] = field(default_factory=list)
    # Keys tagged (or to tag) `source-changed`: confirmed rows (pinned or attested), and
    # rows with a recorded outcome, whose document now says otherwise.
    flagged: list[str] = field(default_factory=list)
    # "<key>: refused: <code>" for each retire write the server refused, or
    # "<key>: changed while the run read it" for one that found the row moved on.
    refused: list[str] = field(default_factory=list)


def _identity(org: str, namespace: str) -> Identity:
    return Identity(
        org=org, namespace=namespace, agent=EXTRACTOR_AGENT, user_id="-", session_id="-"
    )


def _confirmed(row: MemoryRow) -> bool:
    """A human stands behind the row: pinned, or attested in its current form; the rule
    memory.for_file marks `(unconfirmed)` by."""
    state = verification_state(row.verified_by, row.verified_sha256, row.content_sha256)
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


def _unread_reply(stop_reason: str) -> str:
    """Why a model reply could not be read as the candidates' JSON."""
    if stop_reason in ("length", "max_tokens"):
        return "reply cut at the token limit; lower --window-chars or --max-per-section"
    reason = "the model's reply is not the JSON asked for"
    return f"{reason} (stop reason: {stop_reason})" if stop_reason else reason


def _refusal(e: HTTPException) -> str:
    detail: dict[str, Any] = e.detail if isinstance(e.detail, dict) else {}
    return f"refused: {detail.get('code') or e.status_code}"


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

    report = ExtractReport(org, namespace, source_uri, dry_run)
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
    # A chunk with no words (an image under its heading) carries no rationale to read.
    wins = windows([c for c in chunks if not dx.image_only(c.text)], window_chars=window_chars)
    report.windows = len(wins)
    # One scan of the extractor's rows on this document, for the prompt and the retire
    # step; rows this run writes are skipped by the retire step.
    own_rows = await _extractor_rows(org, namespace, source_uri)
    known = await _known_decisions(org, namespace, own_rows)
    kept = await _candidates(org, namespace, source_uri, wins, max_per_section, report, known)
    written: set[str] = set()
    # (outcome, reasoning) -> key of each decision this document now holds, for the retire
    # step: the same outcome on the same verbatim reasoning is the same decision, however
    # the model worded its scenario and category.
    held: dict[tuple[str, str], str] = {}
    for cand, section, texts in kept:
        try:
            rec = await _reconcile(org, namespace, source_uri, cand, section, texts, dry_run)
        except HTTPException as e:
            # A blacklisted or newly pinned decision: the other candidates and the retire
            # step still run, and a re-run does not stop at the same place.
            report.dropped.append(Dropped(section, _refusal(e), cand.quote))
            continue
        if rec is None:
            report.dropped.append(Dropped(section, "does not fit decision.record", cand.quote))
            continue
        written.add(rec.memory_key)
        if rec.action == "row_changed":
            reason = "refused: the row changed while the run read it"
            report.dropped.append(Dropped(section, reason, cand.quote))
            continue
        report.records.append(rec)
        if rec.action == "flagged":
            report.flagged.append(rec.memory_key)
        if rec.holds:
            held.setdefault((cand.outcome.strip(), cand.reasoning.strip()), rec.memory_key)
    await _retire(report, own_rows, live_texts, written, held)
    return report


async def _known_decisions(
    org: str, namespace: str, rows: list[MemoryRow]
) -> list[tuple[str, str, str]]:
    """(category, scenario, outcome) of the extractor's decisions on this document, pinned
    or not, for the prompt: a model told how a decision was worded before reuses the
    wording, so its key and its outcome stay put across runs."""
    known: list[tuple[str, str, str]] = []
    for row in rows:
        writer = await live_writer_record(org, namespace, row.memory_key)
        if writer is None or not _is_extractor(writer.agent, writer.user_id, writer.session_id):
            continue
        record = svc.parse_record_json(writer.record_json)
        category, scenario, outcome = (
            str(record.get(f) or "") for f in ("category", "scenario", "outcome")
        )
        known.append((category, scenario, outcome))
        if len(known) >= dx.MAX_KNOWN_DECISIONS:
            break
    return known


async def _candidates(
    org: str,
    namespace: str,
    source_uri: str,
    wins: list[Window],
    max_per_section: int,
    report: ExtractReport,
    known: list[tuple[str, str, str]],
) -> list[tuple[dx.Candidate, str, list[str]]]:
    """(candidate, its section, the texts it was checked against) for every candidate
    that passes the verbatim check, at most `max_per_section` per section."""
    from server.llm.client import get_llm_client
    from server.llm.json_text import strip_code_fence

    llm = get_llm_client()
    system = dx.system_prompt(max_per_section, known)
    kept: list[tuple[dx.Candidate, str, list[str]]] = []
    per_section: dict[str, int] = {}
    keys: set[str] = set()
    pairs: set[tuple[str, str]] = set()  # (outcome, reasoning) of each kept candidate
    # An operator command: its calls are not gated by the namespace's cost cap.
    for w in wins:
        with llm_stage("decision_extract"):
            res = await llm.complete(
                system,
                dx.user_prompt(w),
                max_tokens=_REPLY_TOKENS,
                response_format={"type": "json_object"},
            )
        first = dx.breadcrumb(w.chunks[0].section_path)
        cands = dx.parse_candidates(strip_code_fence(res.text))
        if cands is None:
            report.dropped.append(Dropped(first, _unread_reply(res.stop_reason)))
            continue
        texts = [c.text for c in w.chunks]
        for cand in cands:
            section = dx.quote_section(w, cand.quote) if cand.quote else first
            reason = dx.verbatim_failure(cand, texts)
            try:
                key = svc.decision_key(cand.category, cand.scenario)
            except ValueError:
                # The key keeps letters and digits a-z, 0-9 only (a CJK scenario has none).
                key = ""
                reason = reason or "no letters or digits a-z, 0-9 to build a key from"
            pair = (cand.outcome.strip(), cand.reasoning.strip())
            if not reason and key in keys:
                reason = "same key as an earlier candidate"
            elif not reason and pair in pairs:
                # Overlapping chunks feed one span to two windows, worded apart.
                reason = "same decision as an earlier candidate"
            elif (
                not reason and _record_input(org, namespace, source_uri, cand, section, []) is None
            ):
                # Checked before it takes a slot, a key or a code lookup.
                reason = "does not fit decision.record"
            elif not reason and per_section.get(section, 0) >= max_per_section:
                reason = f"the section already has --max-per-section {max_per_section}"
            if reason:
                report.dropped.append(Dropped(section, reason, cand.quote))
                continue
            per_section[section] = per_section.get(section, 0) + 1
            keys.add(key)
            pairs.add(pair)
            kept.append((cand, section, texts))
    return kept


def _record_input(
    org: str, namespace: str, source_uri: str, cand: dx.Candidate, section: str, files: list[str]
) -> DecisionRecordInput | None:
    tags = [DESIGN_DOC_TAG, *([CONSTRAINT_TAG] if cand.is_constraint else [])]
    try:
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
    except ValidationError:
        return None


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
    # The alternatives holding a reason first, so a reasonless one takes what is left.
    for alt in sorted(stored, key=lambda a: dx.REJECTED not in a):
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
    there alone is no change in the document."""
    if str(record.get("reasoning") or "") != inp.reasoning.strip():
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
) -> RecordReport | None:
    """Decide what the candidate does to the namespace, and do it unless `dry_run`."""
    files, signals, unresolved, complete = await resolve_mentions(org, namespace, cand, texts)
    inp = _record_input(org, namespace, source_uri, cand, section, files)
    if inp is None:
        return None
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
            if row is not None and this_doc:
                await _clear_if_tagged(rec, row, dry_run)
            return rec
        if row is not None and _confirmed(row) and this_doc:
            # The human who confirmed it decides, sending this body from their own token: a
            # pinned writer row refuses decision.record, so they unpin it with memory.unpin
            # first; an attested one takes it, and its attestation then reads stale.
            rec.body.update(agent=row.agent, user_id=row.user_id, session_id=row.session_id)
            rec.note = (
                "pinned; the document now says otherwise; to accept this body, unpin the row "
                "with memory.unpin, then send it"
                if row.pinned
                else "attested; the document now says otherwise; to accept this body, send it "
                "and attest the new decision again with memory.verify"
            )
            rec.action = await _flag(row, dry_run)
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
        if row is not None and str(record.get("realized_outcome") or "").strip():
            # decision.record rebuilds the record and the decision tags from the body alone,
            # so a re-record would drop the outcome decision.outcome wrote. Re-applying it
            # afterwards would credit the run's memories a second time. A changed verbatim
            # field is flagged, as on a pinned row; changed row fields alone are not.
            rec.note = "its outcome is recorded; re-recording would lose it"
            rec.action = "outcome_known" if same else await _flag(row, dry_run)
            if same:
                # Only its row fields are stale: the document says what it says again.
                await _clear_if_tagged(rec, row, dry_run)
            return rec
        rec.action = "revised"
    rec.holds = True
    if not dry_run:
        from server.memory.app.decision import handle_decision_record

        await handle_decision_record(inp)
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
            top_k=5,
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
    for hit in sorted(close, key=lambda h: h.similarity, reverse=True):
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
    unresolved_settings: list[str] = []
    for name, signal in mentions.symbols:
        path = await symbol_path(org, namespace, name)
        if path is None:
            complete = False
            signals.append(f"{name}: {signal}, unknown (code index unavailable)")
        elif path:
            signals.append(f"{name}: {signal}, exact -> {path}")
            if path not in files:
                files.append(path)
        elif dx.looks_like_setting(name):
            # A backticked snake_case name the index does not define: likely a setting.
            unresolved_settings.append(name)
            signals.append(f"{name}: {signal}, no definition; reported as a setting")
        else:
            signals.append(f"{name}: {signal}, no single definition")
    unresolved = [f"{name} ({kind})" for name, kind in mentions.unresolved]
    unresolved += [f"{name} (setting)" for name in unresolved_settings]
    return files, signals, unresolved, complete


async def symbol_path(org: str, namespace: str, name: str) -> str | None:
    """The one file defining `name` in the namespace's code index, '' when none or
    several do, None when the index could not be read."""
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
    return dx.definition_path([(u.path, u.symbol) for u in out.definitions])


async def _clear_if_tagged(rec: RecordReport, row: MemoryRow, dry_run: bool) -> None:
    """Drop a row's `source-changed` tag once the document says what the row says again."""
    if SOURCE_CHANGED_TAG not in row.tags:
        return
    note = "would clear source-changed" if dry_run else "source-changed cleared"
    if not dry_run and not await _set_source_changed(row, False):
        note = "source-changed not cleared: the row changed while the run read it"
    rec.note = f"{rec.note}; {note}" if rec.note else note


async def _flag(row: MemoryRow, dry_run: bool) -> str:
    """Tag a row the document now contradicts `source-changed`; the action it reports."""
    if SOURCE_CHANGED_TAG in row.tags:
        return "already_flagged"
    if not dry_run and not await _set_source_changed(row, True):
        return "row_changed"
    return "flagged"


async def _set_source_changed(row: MemoryRow, on: bool) -> bool:
    """Add or drop the `source-changed` tag of a row, tags only, so the pin and the value
    stay; False when the row is gone (deleted since the read). The write is a new version
    by the extractor: the row's actor and age move to it. By its memory_id:
    memory.update reads a `-` user or session as any, so a key two identities hold would
    be ambiguous there."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id

    tags = [t for t in row.tags if t != SOURCE_CHANGED_TAG]
    # memory.update_by_id has no precondition on tags (`if_match_sha256` covers the value
    # alone), so a tag written between the read and this call is lost.
    out = await handle_memory_update_by_id(
        MemoryUpdateByIdInput(
            org=row.org,
            memory_id=row.memory_id,
            tags=[*tags, SOURCE_CHANGED_TAG] if on else tags,
            char_limit=row.char_limit,  # None would drop the row's cap
        )
    )
    return bool(out.updated)


async def _extractor_rows(org: str, namespace: str, source_uri: str) -> list[MemoryRow]:
    """The extractor's live decision rows citing this document, whole: listed a page at a
    time (the listing carries no source), each page read back in one bulk read."""
    ident = _identity(org, namespace)
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
    rows: list[MemoryRow],
    live_texts: list[str],
    written: set[str],
    held: dict[tuple[str, str], str],
) -> None:
    """Contract the extractor's own rows on this document whose reasoning left its text:
    delete an unconfirmed one; tag a confirmed one (pinned or attested), or one with a
    recorded outcome, `source-changed`. An unconfirmed one whose outcome and reasoning this
    run holds under
    another key (`held`) is deleted too: the model worded the scenario anew, and the
    decision is not served twice. Another decision resting on the same reasoning has
    another outcome and stays. A row whose reasoning is still there stays, even when this
    run's model did not emit it again. The step never drops a `source-changed` tag: it
    sees the reasoning alone, and the tag may stand for another field the document still
    contradicts; reconcile drops it when the verbatim fields match again."""
    org, namespace = report.org, report.namespace
    for row in rows:
        key = row.memory_key
        if key in written:
            continue
        # The extractor's own record: another identity's record of the key, active or
        # superseding ours, says nothing about whether our row's text still stands.
        writer = await live_writer_record(org, namespace, key)
        if writer is None or not _is_extractor(writer.agent, writer.user_id, writer.session_id):
            continue
        record = svc.parse_record_json(writer.record_json)
        reasoning = str(record.get("reasoning") or "")
        if not reasoning:
            continue
        restated = held.get((str(record.get("outcome") or "").strip(), reasoning.strip()))
        try:
            # A row with a recorded outcome is kept: deleting it would lose the outcome.
            if not _confirmed(row) and restated is not None and not record.get("realized_outcome"):
                if await _delete_row(report, row):
                    report.replaced.append(f"{key} -> {restated}")
                continue
            if not any(reasoning in t for t in live_texts):
                # A row with a recorded outcome is tagged, never deleted, like a pinned one.
                keep = _confirmed(row) or bool(record.get("realized_outcome"))
                await _retire_row(report, row, keep=keep)
        except HTTPException as e:
            report.refused.append(f"{key}: {_refusal(e)}")


async def _retire_row(report: ExtractReport, row: MemoryRow, *, keep: bool) -> None:
    """Delete the extractor's row by its writer, or tag it `source-changed` when it is to
    be kept. Reported only once written: a refused write is reported as refused instead."""
    if keep:
        if SOURCE_CHANGED_TAG in row.tags:
            return
        if not report.dry_run and not await _set_source_changed(row, True):
            report.refused.append(f"{row.memory_key}: changed while the run read it")
            return
        report.flagged.append(row.memory_key)
    elif await _delete_row(report, row):
        report.retired.append(row.memory_key)


async def _delete_row(report: ExtractReport, row: MemoryRow) -> bool:
    """Delete the extractor's row at the version read (nothing in a dry run); False when
    it was rewritten since, which is reported."""
    from server.memory.api.models_crud import MemoryDeleteVersionInput
    from server.memory.app.handlers import handle_memory_delete_version

    if report.dry_run:
        return True
    out = await handle_memory_delete_version(
        MemoryDeleteVersionInput(
            org=row.org,
            namespace=row.namespace,
            agent=EXTRACTOR_AGENT,
            memory_key=row.memory_key,
            version=row.version,
        )
    )
    if not out.tombstoned:
        # Rewritten since it was read: the next run judges the new version.
        report.refused.append(f"{row.memory_key}: changed while the run read it")
    return bool(out.tombstoned)
