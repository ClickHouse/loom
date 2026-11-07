"""A document section is labelled noise at ingest (meeting notes, status reports,
bookkeeping) unless it records a decision; noise ranks below the knowledge hits close to
the query and stays out of a context pack, and is still stored and found."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from server.knowledge.api.models_knowledge import _SearchHit
from server.knowledge.app.knowledge_search import noise_last
from server.knowledge.domain.chunk_metadata import read_structure
from server.knowledge.domain.chunking import Chunk
from server.knowledge.domain.extract.artifact import Locator
from server.knowledge.domain.extract.structural import StructuralChunk
from server.knowledge.domain.section_value import is_noise
from server.knowledge.store.chunks import build_chunk_rows
from server.settings import settings

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "section_values.json"


@pytest.fixture(autouse=True)
def _uncalibrated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No embedder calibration another test left set moves the "close" cut."""
    from server.memory.app import embedder_calibration

    monkeypatch.setattr(embedder_calibration, "_calibration", None)


def _scores(items: list[dict[str, Any]]) -> tuple[float, float, int, int]:
    """(noise precision, noise recall, decisions in notes kept, decisions in notes), the
    shares rounded to two places."""
    tp = fp = fn = kept = notes = 0
    for it in items:
        noise = is_noise(it["title"], tuple(it["section_path"]), it["text"])
        gold = it["label"] == "noise"
        tp += noise and gold
        fp += noise and not gold
        fn += gold and not noise
        if it["kind"] == "decision_in_notes":
            notes += 1
            kept += not noise
    return round(tp / (tp + fp), 2), round(tp / (tp + fn), 2), kept, notes


# The labelled set's scores, pinned: a rule change that moves them updates them here and
# says why in its pull request. The rules prefer a missed noise section (ranked higher
# than it should be) to a design section labelled noise (left out of a context pack).
def test_the_labelled_set_scores_as_measured() -> None:
    halves = json.loads(FIXTURE.read_text())
    assert _scores(halves["dev"]) == (1.0, 0.19, 10, 10)
    # The noise missed is template text, chatter, logistics or status in a document whose
    # title does not tie a meeting word to a date: left as content, it only ranks higher.
    assert _scores(halves["held_out"]) == (1.0, 0.17, 10, 10)


@pytest.mark.parametrize(
    ("title", "path", "text"),
    [
        ("Storage sync 2026-03-04", ("Attendees",), "- Ana\n- Tomas"),
        ("Storage sync 2026-03-04", ("Revision history",), "| v2 | Ana | split blocks |"),
        ("Storage sync 2026-03-04", (), "- @ana to check the bench by Friday\n- @tomas to sync"),
        ("Storage sync 2026-03-04", ("Cache",), "Discussed dropping the cache; no conclusion."),
        ("Storage sync 2026-03-04", ("Cache",), "We haven't decided on the TTL yet."),
        ("Storage sync 2026-03-04", ("Cache",), "Global IN right away? Not concluded."),
        (
            "Planner sync 2026-10-01",
            ("Pass",),
            "- Idea: once a column's nulls are rejected, push it.",
        ),
        ("Planning sync - 2026/03/04 10:00 GMT - Notes", ("Cache",), "Talked about the TTL."),
        ("Storage sync 4 March", ("Cache",), "Talked about the TTL."),
        ("2026-03-04 Meeting notes", ("Cache",), "Talked about the TTL."),
        ("2026-03-04-storage-sync.md", ("Cache",), "Talked about the TTL."),
        ("Storage weekly sync", ("2026-03-04", "Cache"), "Talked about the TTL."),
        ("Storage weekly sync", ("Storage weekly sync", "2026-03-04", "Cache"), "Talked."),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: TBD"),
        ("Storage sync 2026-03-04", ("Decision: TBD",), "- Redis or Memcached, revisit."),
        ("Storage sync 2026-03-04", ("Decisions pending", "Cache"), "- Revisit next week."),
        ("Storage sync 2026-03-04", ("Decision: none",), "- Revisit next week."),
        ("Storage sync 2026-03-04", ("Decision: deferred",), "- Revisit next week."),
        ("Storage sync 2026-03-04", ("Cache",), "Outcome: no decision"),
        ("Storage sync 2026-03-04", ("Outcome: no decision",), "- Revisit next week."),
        ("Storage sync 2026-03-04", ("Cache",), "Outcome: no decision yet"),
        ("Storage sync 2026-03-04", ("Cache",), "Conclusion: no consensus, revisit next week"),
        ("Storage sync 2026-03-04", ("Cache",), "Final outcome: no decision"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: none yet."),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: TBD - revisit next week"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: deferred to next sync"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: open - revisit next week"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: open -- revisit next week"),
        ("Storage sync 2026-03-04", ("Cache",), "**Decision:** deferred to next sync"),
        ("Storage sync 2026-03-04", ("Cache",), "**Decision**: deferred"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision - deferred to Q3"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision (deferred)"),
        ("Storage sync 2026-03-04", ("Cache",), "**Decision:** none"),
        ("Storage sync 2026-03-04", ("Cache",), "**Outcome:** no decision yet"),
        ("Storage sync 2026-03-04", ("Cache",), "__Decision:__ deferred to next sync"),
        ("Vector search design", ("__Open questions__",), "- Who owns it?"),
        ("Storage weekly sync", ("__2026-03-04__", "Cache"), "Talked about the TTL."),
        ("__Storage sync__ 2026-03-04", ("Cache",), "Talked about the TTL."),
        ("", ("__Storage sync 2026-03-04__", "Cache"), "Talked about the TTL."),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: not decided yet"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: no decision"),
        ("Storage sync 2026-03-04", ("Decision: not decided",), "- Revisit next week."),
        (
            "Session store RFC",
            ("Decision: TBD", "Open questions"),
            "- Keep sessions?\n- Who owns it?",
        ),
        ("Vector search design", ("Open questions",), "- Are we going with Redis?"),
        # no title: the first heading names the document
        ("", ("Storage sync 2026-03-04", "Cache"), "Talked about the TTL."),
        ("Planner sync notes 2026-09-24", ("Fallbacks",), "Talked about the fallback list."),
        ("Storage sync 2026-03-04", ("Scheduling",), "Next sync moves to Thursday 10:00."),
        ("Session store RFC", ("Open questions",), "- Keep sessions across it?\n- Who owns it?"),
        # a running notes page that dates each meeting with its year
        ("Storage weekly sync", ("Aug 13, 2026", "Cache"), "Talked about the TTL."),
        ("Storage weekly sync", ("4 March 2026", "Cache"), "Talked about the TTL."),
    ],
)
def test_bookkeeping_and_discussion_without_a_decision_are_noise(
    title: str, path: tuple[str, ...], text: str
) -> None:
    assert is_noise(title, path, text)


@pytest.mark.parametrize(
    ("title", "path", "text"),
    [
        # a decision recorded in meeting notes, under a bookkeeping heading
        ("Storage sync 2026-03-04", ("Action items",), "- Agreed to drop the LRU cache."),
        ("Storage sync 2026-03-04", ("Decisions",), "- New services use the shared cluster."),
        ("Storage sync 2026-03-04", ("Decisions", "Cache"), "- Drop the block cache."),
        (
            "Storage sync 2026-03-04",
            ("Slide 4: Decisions",),
            "- New services use the shared cluster.",
        ),
        ("Storage sync 2026-03-04", ("Cache: agreed to drop it",), "- Memory cost too high."),
        # design text, and a design text that names a meeting
        ("Disk set", ("Lookups",), "Each block keeps a Bloom filter, so a miss reads no block."),
        ("Disk set", ("Writes",), "As agreed in the 3/4 sync, writes go through the queue."),
        # a title slide carries nothing to hide
        ("Disk set", ("Slide 1: Disk set",), ""),
        # a design text that will be revisited, and a decision with a follow-up
        (
            "Disk set",
            ("Lookups",),
            "Each block keeps a Bloom filter, so a miss reads no block.\n"
            "We may revisit the filter size once we have production data.",
        ),
        ("Storage sync 2026-03-04", ("Cache",), "Agreed: drop the cache; revisit the TTL later."),
        ("Storage sync 2026-03-04", ("Cache",), "Agreed: drop the cache; the TTL is not decided."),
        ("Storage sync 2026-03-04", ("Cache",), "Agreed: drop the block cache. Who owns cleanup?"),
        ("Storage sync 2026-03-04", ("Cache",), "- Decided to drop Redis; revisit the TTL in Q3?"),
        ("Storage sync 2026-03-04", ("Cache",), "We\u2019re dropping the block cache."),
        ("Storage sync 2026-03-04", ("Locks",), "We'll use one lock per tenant."),
        ("Storage sync 2026-03-04", ("Locks",), "We'll go with one lock per tenant."),
        ("Storage sync 2026-03-04", ("Ledger",), "We picked Postgres for the ledger."),
        ("Storage sync 2026-03-04", ("Ledger",), "Approved: move the ledger to Postgres."),
        ("Storage sync 2026-03-04", ("Queue",), "Let's use the shared queue."),
        ("Storage sync 2026-03-04", ("Queue",), "We're switching to the sharded queue."),
        ("Storage sync 2026-03-04", ("Ledger",), "We're going to use Postgres for the ledger."),
        ("Storage sync 2026-03-04", ("Queue",), "We'll be using the sharded queue."),
        ("Storage sync 2026-03-04", ("Ledger",), "We will migrate the ledger to Postgres."),
        ("Storage sync 2026-03-04", ("Ledger",), "The team picked Postgres."),
        ("Storage sync 2026-03-04", ("Queue",), "We'll go ahead with the sharded queue."),
        ("Storage sync 2026-03-04", ("Queue",), "Proceeding with option B."),
        ("Storage sync 2026-03-04", ("Ledger",), "**Outcome:** keep Postgres for the ledger."),
        ("Storage sync 2026-03-04", ("Ledger",), "Outcome:\n- keep Postgres for the ledger"),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: open the replica to analytics reads."),
        (
            "Storage sync 2026-03-04",
            ("Cache",),
            "No consensus on the TTL, outcome: drop the cache.",
        ),
        (
            "Storage sync 2026-03-04",
            ("Cache",),
            "Not agreed on the TTL - decision: keep 5 minutes.",
        ),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: pending writes go to the WAL."),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: open-source the parser."),
        (
            "Storage sync 2026-03-04",
            ("Cache",),
            "Decision: no consensus on Kafka, so we stay on Redis.",
        ),
        ("Storage sync 2026-03-04", ("Cache",), "Decision: pending-write queue goes first."),
        # A label over a topic keeps the label: content when unsure.
        ("Storage sync 2026-03-04", ("Cache",), "Decision: no consensus on Kafka."),
        ("Storage sync 2026-03-04", ("Cache",), "__Decision:__ drop the cache."),
        ("Storage sync 2026-03-04", ("Cache",), "__Agreed:__ drop the block cache."),
        ("Storage sync 2026-03-04", ("Cache",), "___Agreed:___ drop the block cache."),
        ("Storage sync 2026-03-04", ("__Decisions__",), "- New services use the shared cluster."),
        # A deferral to a target of more than 40 characters is read as a topic: content when unsure.
        (
            "Storage sync 2026-03-04",
            ("Cache",),
            "Decision: deferred until the vendor confirms pricing for the enterprise tier",
        ),
        (
            "Storage sync 2026-03-04",
            ("Cache",),
            "Decision: deferred the cache rewrite until after the freeze",
        ),
        (
            "Storage sync 2026-03-04",
            ("Cache",),
            "Decision: none of the options fit; we keep Postgres.",
        ),
        ("Storage sync 2026-03-04", ("Decision: open the replica to analytics",), "- Ana to do it"),
        ("Storage sync 2026-03-04", ("Ledger",), "We opted for Postgres."),
        ("Storage sync 2026-03-04", ("Cache",), "We rejected Redis: its memory cost is too high."),
        ("Storage sync 2026-03-04", ("Options",), "Option B accepted."),
        ("Storage sync 2026-03-04", ("Ledger",), "Postgres was picked for the ledger."),
        ("Storage sync 2026-03-04", ("Cache: we\u2019re dropping it",), "- Memory cost too high."),
        (
            "Storage sync 2026-03-04",
            ("Cache",),
            "No consensus on the TTL; agreed to drop the cache.",
        ),
        # a design document whose title holds a word meeting notes use
        ("Order status state machine", ("Transitions",), "An order moves to PAID on the webhook."),
        ("Retrofitting the parser", ("Tokens",), "The lexer emits one token per identifier."),
        # code in a fence is not template text
        (
            "Chart",
            ("Chart values",),
            "```yaml\nimage: {{ .Values.image }}\nreplicas: {{ .Values.n }}\n```",
        ),
        # a design document's risks, explained
        ("Disk set", ("Risks",), "A cold cache doubles the reads of a lookup until it warms."),
        # headings that are bookkeeping in notes and design text elsewhere
        (
            "Worker supervisor",
            ("Scheduling",),
            "The supervisor runs each worker pass every 30 s and skips a pass that overlaps.",
        ),
        ("Storage engine", ("Updates",), "An update writes a new part; merges fold them later."),
        # a list's lead-in, a comparison and a generic type are not template text
        ("Rate limiter", ("Config",), "Config:\n- `rl.scope`"),
        ("Buffer", ("Flush",), "Flush when lag < 5 s and the buffer > 1 MB."),
        ("Cache", ("Keys",), "The cache is a `Map<String, Integer>` keyed by tenant."),
        # spec lines that read like logistics or action items
        ("Partitions", ("Layout",), "Time-based partitions keep each day's rows together."),
        ("Gateway", ("Writes",), "Owner-only writes are checked at the gateway."),
        (
            "Tokens",
            ("Flow",),
            "The client will send the token in the header.\n"
            "The server will check it against the revocation list.",
        ),
        ("Rows", ("Types",), "Rows use @dataclass(frozen=True)."),
        ("Views", ("FAQ",), "Why not a materialized view?\nA view recomputes on every insert."),
        # design titles that hold a meeting or progress word
        ("Order status", ("States",), "An order is NEW, PAID or SHIPPED."),
        ("Calendar sync design 2026-01-15", ("Conflicts",), "The newer edit of an event wins."),
        ("Meeting room booking service", ("Holds",), "A hold expires after ten minutes."),
        ("Daily backups: 7/30 retention", ("Retention",), "Seven daily copies are kept."),
        ("Status page with 24/7 paging", ("Paging",), "An incident pages the on-call."),
        ("Sync engine for the March launch", ("Merges",), "The newer edit wins."),
        ("Retro-compatible wire format", ("Fields",), "A reader skips unknown fields."),
        ("1:1 chat delivery", ("Receipts",), "A receipt is sent once per message."),
        ("Standup reminder bot", ("Schedule",), "The bot posts at 09:30 in each team's channel."),
        ("Status update webhook", ("Payload",), "Each update carries the job id and its state."),
        ("Office hours booking service", ("Slots",), "A slot holds one visitor."),
        ("Retro console emulator", ("CPU",), "The CPU runs one instruction per cycle."),
        ("Weekly update digest mailer", ("Digest",), "The digest lists a week's merged changes."),
        # a meeting product's design, and a dated plan
        ("Event service", ("Attendees",), "An event stores its attendees in a separate table."),
        ("Migration planning: cutover on Oct 12", ("Cutover steps",), "Stop writes, copy, switch."),
        # notes words with no date, and bookkeeping headings in a document about them
        ("Upgrade notes from 1.x", ("Breaking changes",), "The limit argument is removed."),
        ("Meeting notes export service", ("Format",), "Each export is one file per meeting."),
        ("Release tooling", ("Changelog",), "The release job collects each entry."),
        ("Docs renderer", ("Table of contents",), "The renderer lists each heading."),
        # a design document dated in its title
        ("Status page 2026-03-04", ("Paging",), "An incident pages the on-call."),
        ("Sync protocol v2 2026-03-04", ("Frames",), "Each frame carries a sequence number."),
        # dated release notes
        ("Release notes 2026-03-04", ("Breaking changes",), "The limit argument is removed."),
        # date-first design titles, and words that hold a meeting word
        ("2026-03-04 Status page", ("Paging",), "An incident pages the on-call."),
        ("2026-03-04 Synchronous replication", ("Lag",), "A replica acknowledges each write."),
        ("2026-03-04-order-status-state-machine.md", ("States",), "An order is NEW or PAID."),
        ("Backups: daily 7/30/365 retention", ("Tiers",), "Three tiers keep copies."),
        # the questions a system answers are its design
        ("Code index", ("Queries the index serves",), "- Which files import it?\n- Who calls it?"),
        ("Code index", ("Questions it answers",), "- Which files import it?\n- Who calls it?"),
        # a dated subheading under a meeting word is not a meeting
        ("Calendar sync service", ("Rollout", "Week 1"), "Enable for 5% of tenants."),
        # a date does not start inside a word
        ("Codec 2 sync", ("Frames",), "Each frame carries a sequence id."),
        ("Hawk 2 status", ("Frames",), "Each frame carries a sequence id."),
        ("Order status service", ("Rollout", "March 4"), "Enable for 5% of tenants."),
        ("Calendar sync 1/2: recurring events", ("Rules",), "Each instance is expanded."),
        ("Order sync 24/7 availability", ("Replicas",), "Two replicas serve every region."),
        ("Order sync 24/7", ("Replicas",), "Two replicas serve every region."),
        ("Replication sync 1/2", ("Lag",), "A replica reports its lag every second."),
        # a topic followed by a record word and a date
        ("Implementation notes 2026-03-04", ("Tokens",), "The lexer emits one token per name."),
        ("Wire protocol update 2026-03-04", ("Frames",), "Each frame carries a sequence id."),
        # template-shaped design text
        (
            "Disk set",
            ("Cold reads",),
            "**Problem:**\nReads time out when the disk set is cold.\n"
            "**Approach:**\nWarm the block index at startup.",
        ),
        ("Blobs", ("Layout",), "Blobs are stored under /blobs/<org>/<date>/<sha>."),
        ("Scanner", ("Headers",), "The scanner reads the copyright header of each file."),
        (
            "Exports",
            ("Questions",),
            "Can a tenant export twice a day?\nYes, the job is idempotent.",
        ),
    ],
)
def test_a_decision_or_design_text_is_content(title: str, path: tuple[str, ...], text: str) -> None:
    assert not is_noise(title, path, text)


_RECORDED = "Planner sync - 2026/03/04 10:00 CET - Notes by Assistant"
_TRANSCRIPT = "Planner sync — Transcript & notes — 2026-03-04 10.00.12 CET"


@pytest.mark.parametrize(
    ("title", "path", "text"),
    [
        (_RECORDED, ("Planner sync - Transcript", "00:12:30"), "**Ana:** uh so the cache is big"),
        (_RECORDED, ("Transcript",), "**Tomas:** right, next one"),
        (_RECORDED, ("Next steps",), "- [ ] [Ana] Task: check the bench by Friday"),
        (_RECORDED, (), "Invited [Ana](mailto:ana@example.com) [Tomas](mailto:t@example.com)"),
        (_RECORDED, (), "Mar 4, 2026 [Ana](mailto:ana@example.com)"),
        (_RECORDED, ("Planner sync",), "Mar 4, 2026\n[Ana](mailto:ana@example.com)"),
        (
            _RECORDED,
            (),
            "This editable transcript was computer generated and might contain errors.",
        ),
        (_RECORDED, (), "Please rate these notes."),
        (_RECORDED, ("Planner sync",), "Invited [Ana](mailto:ana@example.com)"),
        (_RECORDED, (), "You should review Assistant's notes for accuracy."),
        (_RECORDED, (), "Please rate the new notes tab."),
        (_RECORDED, ("Transcription ended after 00:41:12",), "**Ana:** bye"),
        (_TRANSCRIPT, ("Transcript", "00:01:02"), "**Ana:** okay let's start"),
        (_RECORDED, ("Transcript", "00:12:30"), "**Ana:** I'll move on to the next slide"),
        (_RECORDED, ("Transcript", "00:12:40"), "**Ana:** I'll raise that with Tomas"),
        (_RECORDED, ("Transcript", "00:12:50"), "**Ana:** we'll raise that with the vendor"),
        ("Release sync — Transcript — 2026-03-04 10.00.12 CET", ("00:01:02",), "**Ana:** hi"),
        (_RECORDED, ("Planner sync \\- Transcript", "00:02:10"), "**Ana:** so, the numbers"),
        (_TRANSCRIPT, (_TRANSCRIPT, "Transcript", "00:01:02"), "**Ana:** okay, next one"),
        ("", (_TRANSCRIPT, "00:01:02"), "**Ana:** okay, next one"),
    ],
)
def test_a_recorded_meetings_transcript_and_template_are_noise(
    title: str, path: tuple[str, ...], text: str
) -> None:
    assert is_noise(title, path, text)


@pytest.mark.parametrize(
    ("title", "path", "text"),
    [
        # the tool's account of the meeting is content, a decision or not
        (_RECORDED, ("Summary",), "The team reviewed the fallback list and the cache budget."),
        (_RECORDED, ("Details",), "Ana walked through the cache budget per tenant."),
        (_RECORDED, ("Cache budget per tenant",), "Ana showed the per-tenant numbers."),
        (_RECORDED, ("Decisions", "Needs Further Discussion"), "- The TTL for cold tenants."),
        # a decisions heading keeps every section under it
        (_RECORDED, ("Decisions", "Aligned", "Suggested next steps"), "- [ ] [Ana] check it"),
        (_TRANSCRIPT, ("Summary",), "The team reviewed the fallback list."),
        (_TRANSCRIPT, (_TRANSCRIPT, "Summary"), "The team reviewed the fallback list."),
        ("", (_TRANSCRIPT, "Summary"), "The team reviewed the fallback list."),
        (_RECORDED, ("Transcript search latency",), "Ana showed the p99 numbers."),
        (_RECORDED, ("Speaker labels in the transcript",), "Ana showed the labels."),
        (_RECORDED, ("Details",), "Invited the vendor to demo the parser."),
        (_RECORDED, ("Details",), "March 4, 2026 cutover: Ana walked through the steps."),
        (_RECORDED, ("Details",), "You should review the cache numbers before Friday."),
        (_RECORDED, ("Details",), "Please rate-limit the export API per tenant."),
        (_RECORDED, ("Details",), "You should review the release notes before the cutover."),
        # a person's incident notes keep their timeline and action items
        (
            "Incident 2026-03-04 14:00 UTC - notes by Ana",
            ("Timeline", "14:05"),
            "Errors passed 5%.",
        ),
        ("Incident 2026-03-04 14:00 UTC - notes by Ana", ("Action items",), "- Ana: add an alert"),
        (
            "Incident 2026-03-04 14:00 UTC - transcript export failing",
            ("Timeline", "14:05"),
            "Errors passed 5%.",
        ),
        (_RECORDED, ("Details",), "Please rate the three vendors by cost before Friday."),
        (_RECORDED, ("Details",), "You should review Ana's notes on the cache budget first."),
        # a transcript segment that records a decision or a plan
        (_RECORDED, ("Transcript", "00:14:10"), "**Ana:** so the plan is to drop the cache"),
        (_RECORDED, ("Transcript", "00:15:02"), "**Ana:** I'm going to disallow warning alerts."),
        # a clock time without a transcript or notes-by mark names no recording
        ("Cutover 2026-03-04 10:00", ("Transcript",), "Ana walked through the steps."),
    ],
)
def test_a_recorded_meetings_account_is_content(
    title: str, path: tuple[str, ...], text: str
) -> None:
    assert not is_noise(title, path, text)


@pytest.mark.parametrize(
    "text",
    [
        "We'll enable the cache by default.",
        "We are rolling out the new router on Monday.",
        "The plan is to lower the alert to info.",
        "I'll remove the old flag in the next release.",
    ],
)
def test_a_stated_plan_is_a_decision_in_notes(text: str) -> None:
    assert not is_noise("Storage sync 2026-03-04", ("Cache",), text)


def _rows(chunks: list[Any], doc_format: str = "") -> list[Any]:
    """The chunk rows of a document titled as meeting notes."""
    return build_chunk_rows(
        org="o",
        namespace="ns",
        source_uri="kb://sync",
        title="Storage sync 2026-03-04",
        chunks=chunks,
        contexts=[""] * len(chunks),
        embeddings=[[0.0]] * len(chunks),
        tags=[],
        metadata="{}",
        doc_version=1,
        revision=1,
        doc_format=doc_format,
        embedding_model="m",
        embedding_dim=1,
        now=datetime(2026, 1, 1),
        author={"agent": "-", "user_id": "-", "session_id": "-", "source_class": "asserted"},
    )


def test_a_chunk_row_records_the_label_and_a_row_without_one_reads_as_content() -> None:
    rows = _rows(
        [
            Chunk(idx=0, text="- Ana\n- Tomas", section_path=("Attendees",), section_depth=1),
            Chunk(idx=1, text="Agreed: drop the cache.", section_path=("Cache",), section_depth=1),
        ],
    )
    attendees, cache = (json.loads(r["chunk_metadata"]) for r in rows)
    assert attendees["noise"] is True
    assert "noise" not in cache
    assert read_structure(rows[0]["chunk_metadata"]).noise
    assert not read_structure(rows[1]["chunk_metadata"]).noise
    # Written before the label existed, or by a build that does not write it.
    assert not read_structure(json.dumps({"section_path": ["Attendees"]})).noise


def test_the_windows_of_one_long_section_share_its_verdict() -> None:
    decision = "Agreed: keep the disk set's blocks sorted, so a lookup reads one block."
    windows = [decision, " The blocks are 64 KiB each.", " A lookup reads one directory entry."]
    starts = [0, len(windows[0]), len(windows[0]) + len(windows[1])]
    rows = _rows(
        [
            Chunk(idx=i, text=t, section_path=("Disk set",), section_depth=1, char_start=start)
            for i, (t, start) in enumerate(zip(windows, starts, strict=True))
        ],
    )
    assert not any(read_structure(r["chunk_metadata"]).noise for r in rows)
    # Alone, a later window of the section would read as notes with no decision.
    assert is_noise("Storage sync 2026-03-04", ("Disk set",), windows[1])


def test_touching_windows_are_read_as_one_text() -> None:
    # With the stride equal to the size, a window can end inside a word.
    section = "Notes from the bench. Agreed: drop the cache."
    cut = section.index("eed:")
    rows = _rows(
        [
            Chunk(idx=0, text=section[:cut], section_path=("Cache",), section_depth=1),
            Chunk(
                idx=1, text=section[cut:], section_path=("Cache",), section_depth=1, char_start=cut
            ),
        ],
    )
    assert not any(read_structure(r["chunk_metadata"]).noise for r in rows)


def test_a_fence_cut_at_a_window_edge_is_read_once() -> None:
    # Window 1 repeats window 0's closing fence: read twice, it would open a new fence
    # and hide the decision after it.
    section = "```\nrun()\n```\nAgreed: the runner retries twice."
    cut = section.index("Agreed")
    windows = [(0, section[:cut]), (cut - 4, section[cut - 4 :])]
    rows = _rows(
        [
            Chunk(idx=i, text=t, section_path=("Runner",), section_depth=1, char_start=start)
            for i, (start, t) in enumerate(windows)
        ],
    )
    assert not any(read_structure(r["chunk_metadata"]).noise for r in rows)


def test_the_blocks_of_one_section_of_a_file_are_read_on_their_own_lines() -> None:
    # Two packed groups of one section with a gap between them in the source: the
    # second's first line is not glued to the first's closing fence.
    groups = ["```\nrun()\n```", "Agreed: the runner retries twice."]
    rows = _rows(
        [
            StructuralChunk(
                idx=i,
                text=t,
                block_ids=(f"b{i}",),
                block_type="paragraph",
                section_path=("Runner",),
                locator=Locator(char_start=start, char_end=start + len(t)),
            )
            for i, (t, start) in enumerate(zip(groups, [0, len(groups[0]) + 2], strict=True))
        ],
        doc_format="markdown",
    )
    assert not any(read_structure(r["chunk_metadata"]).noise for r in rows)


def _hit(uri: str, score: float, noise: bool = False) -> _SearchHit:
    return _SearchHit(
        source_uri=uri,
        chunk_idx=0,
        content=uri,
        tags=[],
        distance=1 - score,
        similarity=score,
        namespace="ns",
        score=score,
        noise=noise,
    )


def test_noise_ranks_after_the_close_hits_only_with_the_filter_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [_hit("a", 0.9, noise=True), _hit("b", 0.8), _hit("c", 0.7, noise=True), _hit("d", 0.6)]
    assert [h.source_uri for h in noise_last(hits)] == ["b", "d", "a", "c"]
    # Noise moves below the last close hit, so a weak hit ranked above that one goes above
    # it too; every other hit keeps its order.
    weak = [_hit("notes", 0.6, noise=True), _hit("far", 0.2), _hit("near", 0.5)]
    assert [h.source_uri for h in noise_last(weak)] == ["far", "near", "notes"]
    monkeypatch.setattr(settings, "knowledge_noise_filter", False)
    assert [h.source_uri for h in noise_last(hits)] == ["a", "b", "c", "d"]


def test_a_close_hit_clears_the_calibrated_noise_floor_where_that_is_higher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import embedder_calibration

    hits = [_hit("notes", 0.9, noise=True), _hit("design", 0.35)]
    monkeypatch.setattr(embedder_calibration, "effective_noise_floor", lambda fallback: 0.4)
    # 0.35 is under this embedder's floor: it does not answer the query.
    assert [h.source_uri for h in noise_last(hits)] == ["notes", "design"]
    monkeypatch.setattr(embedder_calibration, "effective_noise_floor", lambda fallback: 0.2)
    assert [h.source_uri for h in noise_last(hits)] == ["design", "notes"]


def test_the_repository_docs_merge_keeps_noise_after_the_close_hits() -> None:
    from server.knowledge.app.repo_docs import merge_hits

    base = [_hit("kb://sync", 0.9, noise=True), _hit("kb://design", 0.5)]
    extra = [_hit("repo://adr", 0.7)]
    assert [h.source_uri for h in merge_hits(base, extra, 3)] == [
        "repo://adr",
        "kb://design",
        "kb://sync",
    ]


def test_a_reranked_page_keeps_noise_below_an_admitted_repository_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput, KnowledgeSearchOutput
    from server.knowledge.app import repo_docs

    async def _search(inp: KnowledgeSearchInput) -> KnowledgeSearchOutput:
        if inp.namespace == "code-kj":
            return KnowledgeSearchOutput(results=[_hit("doc://adr", 0.9)], embedding_model="m")
        hits = [_hit("kb://sync", 0.95, noise=True), _hit("kb://design", 0.6)]
        return KnowledgeSearchOutput(results=hits, embedding_model="m", reranked_by="builtin:x")

    monkeypatch.setattr("server.knowledge.app.knowledge_search.handle_knowledge_search", _search)
    page = KnowledgeSearchInput(org="o", namespace="ns", query="the cache", top_k=3)
    out = asyncio.run(repo_docs.knowledge_with_repo_docs(page, repo_namespace="code-kj"))
    assert [h.source_uri for h in out.results] == ["kb://design", "doc://adr", "kb://sync"]


@pytest.mark.parametrize("filter_on", [True, False])
def test_a_noise_hit_ends_a_private_first_search_only_with_the_filter_off(
    monkeypatch: pytest.MonkeyPatch, filter_on: bool
) -> None:
    import server.identity.app.namespace_visibility as nv
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search
    from server.knowledge.app.search import ChunkHit

    async def _visibility(org: str, namespaces: list[str]) -> dict[str, str]:
        return {"home": "private", "team": "public"}

    async def _search(namespaces: list[str]) -> list[tuple[str, list[ChunkHit]]]:
        read.append(namespaces)
        return [
            (ns, [ChunkHit(ns, "sync.md", 0, "", [], distance=0.1, noise=True)])
            for ns in namespaces
        ]

    read: list[list[str]] = []
    monkeypatch.setattr(nv, "visibility_map", _visibility)
    monkeypatch.setattr(settings, "knowledge_noise_filter", filter_on)
    search = KnowledgeSearchInput(org="o", namespace="home", query="q", namespace_scope="all_mine")
    asyncio.run(knowledge_search._search_private_first(search, ["home", "team"], _search))
    assert read == ([["home"], ["team"]] if filter_on else [["home"]])


@pytest.fixture
def _only_knowledge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A context pack whose other sections fail: only its knowledge section answers."""

    async def _dead(*a: Any, **k: Any) -> Any:
        raise RuntimeError("not in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.app.search.handle_memory_search",
        "server.memory.app.procedure.handle_procedure_match",
        "server.knowledge.app.conversations.handle_conversation_search",
    ):
        monkeypatch.setattr(target, _dead)


@pytest.mark.usefixtures("_only_knowledge")
@pytest.mark.parametrize("filter_on", [True, False])
def test_a_context_pack_leaves_noise_out_with_the_filter_on(
    monkeypatch: pytest.MonkeyPatch, filter_on: bool
) -> None:
    from types import SimpleNamespace

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    async def _knowledge(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(results=[_hit("kb://design", 0.9), _hit("kb://sync", 0.8, True)])

    monkeypatch.setattr("server.knowledge.app.knowledge_search.handle_knowledge_search", _knowledge)
    monkeypatch.setattr(settings, "knowledge_noise_filter", filter_on)
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o", namespace="ns", agent="a", query="the cache", include_knowledge=True
            )
        )
    )
    cites = out.section_meta["knowledge"]["citations"]
    assert "knowledge:ns/kb://design" in cites
    assert ("knowledge:ns/kb://sync" in cites) is not filter_on


@pytest.mark.usefixtures("_only_knowledge")
def test_a_context_pack_fills_the_places_noise_took(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    hits = [_hit("kb://sync", 0.9, True), _hit("kb://a", 0.8), _hit("kb://b", 0.7)]

    async def _knowledge(inp: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(results=hits[: inp.top_k])

    monkeypatch.setattr("server.knowledge.app.knowledge_search.handle_knowledge_search", _knowledge)
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o", namespace="ns", agent="a", query="q", include_knowledge=True, top_k=2
            )
        )
    )
    cites = out.section_meta["knowledge"]["citations"]
    assert cites == ["knowledge:ns/kb://a", "knowledge:ns/kb://b"]


@pytest.mark.usefixtures("_only_knowledge")
def test_a_failed_wider_read_still_serves_the_first_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    async def _knowledge(inp: Any) -> Any:
        if inp.top_k > 2:
            raise RuntimeError("the wider read failed")
        return SimpleNamespace(results=[_hit("kb://sync", 0.9, True), _hit("kb://a", 0.8)])

    monkeypatch.setattr("server.knowledge.app.knowledge_search.handle_knowledge_search", _knowledge)
    out = asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o", namespace="ns", agent="a", query="q", include_knowledge=True, top_k=2
            )
        )
    )
    assert out.section_meta["knowledge"]["citations"] == ["knowledge:ns/kb://a"]


@pytest.mark.parametrize("filter_on", [True, False])
def test_a_memory_stack_leaves_noise_out_with_the_filter_on(
    monkeypatch: pytest.MonkeyPatch, filter_on: bool
) -> None:
    # The stack reads chunks itself (and context.pack serves its knowledge entries).
    from server.knowledge.app import search as knowledge_store
    from server.knowledge.app.search import ChunkHit
    from server.memory.api.models_lifecycle import MemoryStackInput
    from server.memory.app import stack

    class _Embedder:
        model_name = "m"

        async def embed(self, _text: str) -> list[float]:
            return [0.0]

    ranked = [("kb://sync", True), ("kb://a", False), ("kb://b", False)]

    async def _search(q: Any) -> list[ChunkHit]:
        return [
            ChunkHit("ns", uri, 0, uri, [], distance=0.1, noise=noise)
            for uri, noise in ranked[: q.top_k]
        ]

    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Embedder())
    monkeypatch.setattr(knowledge_store, "hybrid_search", _search)
    monkeypatch.setattr(settings, "knowledge_noise_filter", filter_on)
    fill = stack._StackFill(budget=10_000)
    asyncio.run(
        stack._stack_knowledge(
            MemoryStackInput(
                org="o", namespace="ns", agent="a", include_knowledge=True, knowledge_top_k=2
            ),
            fill,
            "the cache",
        )
    )
    served = [e.memory_key for e in fill.entries]
    assert served == (["kb://a", "kb://b"] if filter_on else ["kb://sync", "kb://a"])


def test_loom_search_hands_the_model_a_noise_label_only_when_it_is_set() -> None:
    from server.transport.lean_results import lean_knowledge_hits

    section = {"results": [{"source_uri": "a", "noise": True}, {"source_uri": "b", "noise": False}]}
    lean_knowledge_hits(section, "excerpt")
    assert section["results"] == [{"source_uri": "a", "noise": True}, {"source_uri": "b"}]


def test_knowledge_search_serves_a_close_hit_above_a_noise_hit_ranked_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search
    from server.knowledge.app.search import ChunkHit

    class _Embedder:
        model_name = "m"

        async def embed(self, _text: str) -> list[float]:
            return [0.0]

    async def _search(*_a: Any, **_k: Any) -> list[tuple[str, list[ChunkHit]]]:
        notes = ChunkHit("ns", "kb://sync", 0, "", [], distance=0.05, score=0.9, noise=True)
        design = ChunkHit("ns", "kb://design", 0, "", [], distance=0.4, score=0.5)
        return [("ns", [notes, design])]

    monkeypatch.setattr(knowledge_search, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(knowledge_search, "_search_private_first", _search)
    inp = KnowledgeSearchInput(org="o", namespace="ns", query="q", namespace_scope="this")
    out = asyncio.run(knowledge_search.handle_knowledge_search(inp))
    assert [h.source_uri for h in out.results] == ["kb://design", "kb://sync"]
    monkeypatch.setattr(settings, "knowledge_noise_filter", False)
    out = asyncio.run(knowledge_search.handle_knowledge_search(inp))
    assert [h.source_uri for h in out.results] == ["kb://sync", "kb://design"]


def test_a_long_line_is_judged_in_linear_time() -> None:
    import time

    # Quadratic backtracking takes tens of seconds on this line; linear, a few ms.
    started = time.perf_counter()
    is_noise("Disk set", ("X",), "option " * 20_000)
    is_noise("Storage sync 2026-03-04", ("X",), "outcome: decision: " * 20_000)
    is_noise("Storage sync 2026-03-04", ("X",), "Decision" + " " * 20_000 + "x")
    is_noise("Storage sync 2026-03-04", ("X",), "Decision" + "*:" * 20_000 + "x")
    is_noise("Storage sync 2026-03-04", ("X",), "_" * 40_000 + "x")
    is_noise("Storage sync 2026-03-04", ("X",), "Decision: deferred to " + "x " * 20_000)
    is_noise(_RECORDED, ("00:12:30",), "we " * 20_000)
    is_noise("Jan 4, " * 20_000 + "notes by", ("X",), "x")
    is_noise(_RECORDED, ("transcript " * 20_000,), "x")
    is_noise(_RECORDED, ("Notes",), "Invited " + "a " * 20_000)
    assert time.perf_counter() - started < 5.0
