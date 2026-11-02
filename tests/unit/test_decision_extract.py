"""`loom decisions extract`: the verbatim check, mention resolution and the reconcile and
retire branches, over a fake model and stubbed planes."""

from __future__ import annotations

import argparse
import json
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from server.cli import decisions as cli
from server.memory.app import decision_extract as de
from server.memory.app import decisions as svc
from server.memory.app.search.scoring import per_key_fetch
from server.memory.domain import decision_extract as dx
from server.memory.domain.corpus_synthesis import CorpusChunk, Window
from server.memory.store import WriterRecord

DOC = (
    "# Spilling\n\n"
    "## Final DISTINCT\n\n"
    "We spill into sorted runs like ORDER BY. A plain set on disk costs random reads. "
    "ExternalDistinctTransform does the merge, see #4821 and max_bytes_before_external_group_by.\n\n"
    "## Architecture\n\n"
    "![diagram](img.png)\n"
)
SECTION = "Spilling > Final DISTINCT"
HEADER = "src/Processors/ExternalDistinctTransform.h"
BODY = (
    "We spill into sorted runs like ORDER BY. A plain set on disk costs random reads. "
    "ExternalDistinctTransform does the merge, see #4821 and max_bytes_before_external_group_by."
)


def _candidate(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "category": "design:spilling",
        "scenario": "how the final DISTINCT spills to disk",
        "outcome": "spill into sorted runs",
        "reasoning": "We spill into sorted runs like ORDER BY.",
        "alternatives": [{"option": "a plain set on disk", "reason": "costs random reads"}],
        "quote": "We spill into sorted runs like ORDER BY. A plain set on disk costs random reads.",
        "is_constraint": False,
        "mentions": ["ExternalDistinctTransform", "#4821", "max_bytes_before_external_group_by"],
    }
    base.update(over)
    return base


def _key(c: dict[str, Any]) -> str:
    return svc.decision_key(c["category"], c["scenario"])


class _Row(SimpleNamespace):
    pass


def _row(key: str, **over: Any) -> _Row:
    base: dict[str, Any] = dict(
        org="o",
        namespace="ns",
        agent=de.EXTRACTOR_AGENT,
        user_id="-",
        session_id="-",
        memory_key=key,
        pinned=False,
        tags=["decision"],
        files=[],
        char_limit=None,
        verified_by="",
        verified_sha256="",
        content_sha256="sha-1",
        value="Decided spill.",
        version=7,
        source_resource_id=f"knowledge:ns/doc.md#{SECTION}",
    )
    base.update(over)
    base.setdefault("memory_id", f"id-{key}-{base['agent']}")
    base.setdefault("memory_type", "decision")
    return _Row(**base)


class _World:
    """The planes the extractor reads and writes, in memory."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, reply: list[dict[str, Any]]) -> None:
        self.reply = reply
        self.records: dict[str, tuple[WriterRecord, dict[str, Any]]] = {}
        self.rows: list[_Row] = []
        self.search_hits: list[Any] = []
        self.symbols: dict[str, list[tuple[str, str]]] = {
            "ExternalDistinctTransform": [
                ("src/Processors/ExternalDistinctTransform.h", "DB::ExternalDistinctTransform"),
                (
                    "src/Processors/ExternalDistinctTransform.cpp",
                    "DB::ExternalDistinctTransform::ExternalDistinctTransform",
                ),
            ]
        }
        self.recorded: list[Any] = []
        self.updates: list[Any] = []
        self.refuse_record: Any = None
        # Every document ever ingested in the namespace; the first is the one extracted.
        self.docs = {"doc.md"}
        self.writer_reads: list[str] = []
        # Keys rewritten between the run's read and its delete.
        self.moved_on: set[str] = set()
        self.deleted_before: set[str] = set()
        self.index_down = False
        # memory_ids deleted between the run's read and its tag write.
        self.gone: set[str] = set()
        self.bulk_reads = 0
        self.audits: list[tuple[str, str, dict[str, Any]]] = []
        # memory_ids whose value moved on since the run read them.
        self.value_moved: set[str] = set()
        self.refuse_update: Any = None
        self.refuse_delete: Any = None
        self.deletes: list[Any] = []
        self.prompts: list[tuple[str, str]] = []
        # Replies served before `reply`, one per call: a candidate list, or an error raised.
        self.replies: list[Any] = []
        # The live document's text, which the recap divider rule reads.
        self.content = DOC
        self._patch(monkeypatch)
        from server.settings import Settings, settings

        # LOOM_LLM_PROVIDER=fake set on purpose: the run does not refuse the fake model.
        monkeypatch.setattr(settings, "llm_provider", "fake")
        monkeypatch.setattr(Settings, "llm_provider_inferred", lambda self: False)

    def add(self, key: str, **row: Any) -> _Row:
        r = _row(key, **row)
        self.rows.append(r)
        return r

    def record(self, key: str, fields: dict[str, Any], **row: Any) -> None:
        """A row and the key's active record, written by that row's identity."""
        r = self.add(key, **row)
        writer = WriterRecord(r.agent, r.user_id, r.session_id, json.dumps(fields), None)  # type: ignore[arg-type]
        self.records[key] = (writer, fields)

    def _patch(self, mp: pytest.MonkeyPatch) -> None:
        from server.code.app import symbol as code_symbol
        from server.knowledge.app import document_reads
        from server.llm import client as llm_client
        from server.memory.app import conflicts, decision, handlers, search, update

        world = self

        class _LLM:
            async def complete(self, system, user, max_tokens=0, response_format=None):
                world.prompts.append((system, user))
                reply = world.replies.pop(0) if world.replies else world.reply
                if isinstance(reply, Exception):
                    raise reply
                return SimpleNamespace(text=json.dumps({"decisions": reply}), stop_reason="")

        async def knowledge_get(inp):
            return SimpleNamespace(found=inp.source_uri in world.docs, content=world.content)

        async def chunks(org, ns, uri):
            return [CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT"))]

        async def writer(org, ns, key, ch=None):
            world.writer_reads.append(key)
            return world.records[key][0] if key in world.records else None

        def same(ident, r):
            return all(
                getattr(ident, a) in ("*", getattr(r, a))
                for a in ("agent", "user_id", "session_id")
            )

        async def get_by_key(ident, key, *, scope, fresh=False, strong=False):
            # As the store: a `*` agent drops the identity filter and the newest row wins.
            if ident.agent == "*":
                hits = [r for r in world.rows if r.memory_key == key]
            else:
                hits = [r for r in world.rows if r.memory_key == key and same(ident, r)]
            return hits[-1] if hits else None

        async def many(ident, keys, *, scope, strong=False):
            world.bulk_reads += 1
            return [r for r in world.rows if r.memory_key in keys and same(ident, r)]

        async def list_rows(ident, *, scope, memory_type, order_by="", limit=200, offset=0, **kw):
            rows = [r for r in world.rows if same(ident, r)]
            if kw.get("pinned_only"):
                rows = [r for r in rows if r.pinned]
            if kw.get("key_prefix"):
                rows = [r for r in rows if r.memory_key.startswith(kw["key_prefix"])]
            return rows[offset : offset + limit]

        async def mem_search(inp):
            return SimpleNamespace(results=world.search_hits)

        async def floor(org, ns, memory_type):
            return 0.85, 0.15

        async def decision_record(inp):
            if world.refuse_record:
                raise world.refuse_record
            world.recorded.append(inp)

        async def mem_update(inp):
            if world.refuse_update:
                raise world.refuse_update
            if inp.memory_id in world.value_moved:
                assert inp.if_match_sha256  # the write is conditional on the value read
                from fastapi import HTTPException

                # As memory.update_by_id answers a hash mismatch: with the live hash.
                raise HTTPException(
                    status_code=409,
                    detail={"code": "PRECONDITION_FAILED", "current_sha256": "sha-new"},
                )
            if inp.memory_id in world.gone:
                return SimpleNamespace(updated=False)
            world.updates.append(inp)
            return SimpleNamespace(updated=True)

        async def delete_version(inp):
            if world.refuse_delete:
                raise world.refuse_delete
            world.deletes.append(inp)
            if inp.memory_key in world.deleted_before:
                return SimpleNamespace(tombstoned=False, found=False, reason="")
            moved = inp.memory_key in world.moved_on
            return SimpleNamespace(
                tombstoned=not moved, found=True, reason="version_changed" if moved else ""
            )

        async def code_symbol_handler(inp):
            if world.index_down:
                raise RuntimeError("code index unreachable")
            defs = world.symbols.get(inp.name, [])
            return SimpleNamespace(definitions=[SimpleNamespace(path=p, symbol=s) for p, s in defs])

        mp.setattr(llm_client, "get_llm_client", lambda: _LLM())
        mp.setattr(document_reads, "handle_knowledge_get", knowledge_get)
        mp.setattr(document_reads, "document_chunks", chunks)
        mp.setattr(de, "live_writer_record", writer)
        mp.setattr(de, "get_by_key", get_by_key)
        mp.setattr(de, "list_by_namespace", list_rows)
        mp.setattr(de, "get_many_by_keys", many)

        async def actors(org, keys, *, scope):
            return {
                (ns, k): [
                    {"agent": r.agent, "user_id": r.user_id, "session_id": r.session_id}
                    for r in world.rows
                    if r.memory_key == k
                ]
                for ns, k in keys
            }

        async def by_identity(idents):
            wanted = {(i[2], i[3], i[4], i[5]) for i in idents}
            return {
                (r.org, r.namespace, r.agent, r.user_id, r.session_id, r.memory_key): r
                for r in world.rows
                if (r.agent, r.user_id, r.session_id, r.memory_key) in wanted
            }

        from server.observability import audit

        async def emit(event_type, *, org, namespace, actor, subject=None, **kw):
            world.audits.append((event_type, actor, subject or {}))
            return "h"

        mp.setattr(audit, "emit", emit)
        mp.setattr(de, "actors_by_key", actors)
        mp.setattr(de, "memory_rows_by_identity_keys", by_identity)
        mp.setattr(search, "handle_memory_search", mem_search)
        mp.setattr(conflicts, "resolve_conflict_floor", floor)
        mp.setattr(decision, "handle_decision_record", decision_record)
        mp.setattr(update, "handle_memory_update_by_id", mem_update)
        mp.setattr(handlers, "handle_memory_delete_version", delete_version)
        mp.setattr(code_symbol, "handle_code_symbol", code_symbol_handler)


async def _run(dry_run: bool = False, uri: str = "doc.md", **kw: Any) -> de.ExtractReport:
    return await de.extract_document("o", "ns", uri, dry_run=dry_run, **kw)


def _same_fields(c: dict[str, Any]) -> dict[str, Any]:
    """The record decision.record stores for the candidate."""
    return {
        "category": c["category"],
        "scenario": c["scenario"],
        "outcome": c["outcome"],
        "reasoning": c["reasoning"],
        "alternatives": "\n".join(
            f"{a['option']} — rejected: {a['reason']}" for a in c["alternatives"]
        ),
    }


# ------------------------------------------------------------------ verbatim check


def _cand(**over: Any) -> dx.Candidate:
    (cand,) = dx.parse_candidates(json.dumps({"decisions": [_candidate(**over)]})) or []
    return cand


def test_a_candidate_resting_on_the_documents_words_passes() -> None:
    assert dx.verbatim_failure(_cand(), [BODY]) == ""


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"quote": "We spill into sorted runs, like ORDER BY."}, "quote is not in"),
        ({"quote": "   "}, "quote too short"),
        ({"reasoning": "Sorted runs are faster."}, "reasoning is not in"),
        (
            {"alternatives": [{"option": "a hash set", "reason": "it uses too much memory"}]},
            "rejecting 'a hash set'",
        ),
    ],
)
def test_a_paraphrase_is_dropped(over: dict[str, Any], reason: str) -> None:
    assert reason in dx.verbatim_failure(_cand(**over), [BODY])


def test_a_quote_across_two_chunks_is_not_the_documents() -> None:
    cand = _cand(quote="ORDER BY.\n\nA plain set", reasoning="ORDER BY.")
    assert dx.verbatim_failure(cand, ["We spill like ORDER BY.", "A plain set costs."])


def test_a_reply_that_is_not_json_or_lacks_fields_reads_as_such() -> None:
    from server.llm.json_text import strip_code_fence

    assert dx.parse_candidates("I found two decisions") is None
    assert dx.parse_candidates('{"other": []}') is None
    assert dx.parse_candidates(json.dumps([_candidate(outcome="")])) == []
    fenced = "```json\n" + json.dumps({"decisions": [_candidate()]}) + "\n```"
    (cand,) = dx.parse_candidates(strip_code_fence(fenced)) or []
    assert cand.rejected() == ["a plain set on disk — rejected: costs random reads"]


def test_image_only_sections_are_the_leaves_with_no_words() -> None:
    assert dx.image_only_sections(DOC) == ["Spilling > Architecture"]
    assert dx.image_only_sections("# A\n\n## B\n\ntext\n\n## C\n\n[image]\n") == ["A > C"]


def test_the_quote_names_its_section() -> None:
    w = Window(
        [
            CorpusChunk("d", 0, "First slide text.", ("Deck", "One")),
            CorpusChunk("d", 1, "Second slide text.", ("Deck", "Two")),
        ]
    )
    assert dx.quote_section(w, "Second slide") == "Deck > Two"
    assert dx.quote_section(w, "nowhere") == "Deck > One"
    assert "[section: Deck > Two]" in dx.user_prompt(w)


# ------------------------------------------------------------------ mentions


def test_mentions_are_sorted_by_what_resolves_them() -> None:
    text = "`DB::JoinStep` and JoinStep, ExternalDistinctTransform, #4821, max_threads, `merge`"
    m = dx.sort_mentions(
        ["DB::JoinStep", "ExternalDistinctTransform", "#4821", "max_threads", "Invented", "merge"],
        text,
    )
    assert m.symbols == [
        ("DB::JoinStep", "qualified"),
        ("ExternalDistinctTransform", "camelcase"),
        ("merge", "qualified"),
    ]
    assert m.unresolved == [("#4821", "pr"), ("max_threads", "setting")]


def test_a_class_wins_over_its_constructor_and_homonyms_link_nothing() -> None:
    assert dx.definition_path([("a.h", "DB::Foo"), ("a.cpp", "DB::Foo::Foo")]) == "a.h"
    assert dx.definition_path([("a.cpp", "DB::f"), ("b.cpp", "DB::g::f")]) == ""
    assert dx.definition_path([]) == ""


# ------------------------------------------------------------------ reconcile


@pytest.mark.asyncio
async def test_a_new_decision_is_written_with_the_extractor_identity_and_its_source(
    monkeypatch,
) -> None:
    world = _World(monkeypatch, [_candidate(is_constraint=True)])
    report = await _run()
    (inp,) = world.recorded
    assert (inp.agent, inp.user_id, inp.session_id) == ("loom-decisions", "-", "-")
    assert inp.confidence == 0.5 and inp.source_class == "derived"
    assert inp.source_resource_id == f"knowledge:ns/doc.md#{SECTION}"
    assert inp.tags == ["source-kind:design_doc", "constraint"]
    assert inp.files == ["src/Processors/ExternalDistinctTransform.h"]
    assert inp.alternatives == ["a plain set on disk — rejected: costs random reads"]
    (rec,) = report.records
    assert rec.action == "written" and rec.memory_key == _key(_candidate())
    assert rec.signals == [
        "ExternalDistinctTransform: camelcase, exact -> src/Processors/ExternalDistinctTransform.h"
    ]
    assert rec.unresolved == ["#4821 (pr)", "max_bytes_before_external_group_by (setting)"]
    assert report.image_only == ["Spilling > Architecture"]
    assert "At most 3 items per section" in world.prompts[0][0]


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing_and_prints_the_bodies(monkeypatch, capsys) -> None:
    world = _World(monkeypatch, [_candidate(), _candidate(quote="not in the deck")])
    report = await _run(dry_run=True)
    assert world.recorded == [] and world.updates == [] and world.deletes == []
    cli.print_report(report)
    out = capsys.readouterr().out
    assert "(dry run)" in out and f"would record {_key(_candidate())}" in out
    assert '"agent": "loom-decisions"' in out and '"source_class": "derived"' in out
    assert "unresolved: #4821 (pr)" in out
    assert f"dropped [{SECTION}] quote is not in the document text" in out
    assert "image-only, rationale not extracted: Spilling > Architecture" in out


@pytest.mark.asyncio
async def test_an_identical_record_is_left_alone(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER])
    report = await _run()
    assert world.recorded == [] and report.records[0].action == "unchanged"


@pytest.mark.asyncio
async def test_a_file_that_resolves_on_a_later_run_revises_the_row(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    symbols = world.symbols
    world.symbols = {}  # the code is not indexed yet
    await _run()
    (first,) = world.recorded
    assert first.files == []
    world.record(_key(c), _same_fields(c), files=[])
    world.symbols = symbols
    report = await _run()
    assert report.records[0].action == "revised"
    assert world.recorded[-1].files == [HEADER]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        {"files": [HEADER], "source_resource_id": "knowledge:ns/doc.md#Old heading"},
        {"files": [HEADER], "tags": ["decision", "constraint"]},
    ],
)
async def test_a_moved_section_or_a_dropped_constraint_revises_the_row(monkeypatch, row) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), **row)
    report = await _run()
    assert report.records[0].action == "revised" and len(world.recorded) == 1


@pytest.mark.asyncio
async def test_a_pinned_row_with_the_same_text_is_left_alone_whatever_its_files(
    monkeypatch,
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[], pinned=True)
    report = await _run()
    assert report.records[0].action == "unchanged"
    assert world.recorded == [] and world.updates == []


@pytest.mark.asyncio
async def test_a_row_from_another_document_is_not_revised(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    other = "knowledge:ns/other.md#Final DISTINCT"
    world.record(
        _key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, source_resource_id=other
    )
    report = await _run()
    (rec,) = report.records
    assert rec.action == "not_ours" and other in rec.note
    assert world.recorded == []


@pytest.mark.asyncio
async def test_the_extractors_unpinned_row_is_revised(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."})
    report = await _run()
    assert report.records[0].action == "revised" and len(world.recorded) == 1


@pytest.mark.asyncio
async def test_a_pinned_row_is_tagged_and_its_body_printed_never_written(
    monkeypatch, capsys
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set."},
        agent="alice-agent",
        user_id="alice",
        pinned=True,
        char_limit=900,
    )
    report = await _run()
    assert world.recorded == []
    (upd,) = world.updates
    assert upd.tags == ["decision", "source-changed"] and upd.value is None
    assert upd.memory_id == "id-" + _key(c) + "-alice-agent"
    assert upd.char_limit == 900  # the row's own cap, passed back
    (rec,) = report.records
    assert rec.action == "flagged" and rec.body["agent"] == "alice-agent"
    assert rec.body["user_id"] == "alice"
    cli.print_report(report)
    assert '"agent": "alice-agent"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_another_writers_unpinned_record_is_not_revised(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, agent="claude")
    report = await _run()
    assert world.recorded == [] and world.updates == []
    assert report.records[0].action == "not_ours"


@pytest.mark.asyncio
async def test_a_confirmed_restatement_under_another_key_is_skipped(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.add("design/spilling/final", agent="alice-agent", pinned=True)
    # A newer unpinned row of the key beside it does not hide the pinned one.
    world.add("design/spilling/final", agent="bob-agent")
    world.search_hits = [SimpleNamespace(memory_key="design/spilling/final", similarity=0.91)]
    report = await _run()
    assert world.recorded == []
    (rec,) = report.records
    assert rec.action == "already_recorded"
    assert rec.note == "already recorded by alice-agent under design/spilling/final"


@pytest.mark.asyncio
async def test_a_confirmed_decision_in_another_category_is_a_restatement(monkeypatch) -> None:
    """The statement leaves the category out, so the same decision filed under another
    category reads the same and is skipped."""
    from server.memory.app import search
    from server.memory.domain.decision_statement import memory_value

    c = _candidate()
    filed_elsewhere = {**_same_fields(c), "category": "design:distinct"}
    other_key = svc.decision_key("design:distinct", c["scenario"])
    world = _World(monkeypatch, [c])
    pinned = world.add(other_key, agent="alice-agent", pinned=True)
    pinned.value = memory_value(filed_elsewhere)

    async def mem_search(inp):
        hits = [r for r in world.rows if r.value == inp.query]
        return SimpleNamespace(
            results=[SimpleNamespace(memory_key=r.memory_key, similarity=1.0) for r in hits]
        )

    monkeypatch.setattr(search, "handle_memory_search", mem_search)
    report = await _run()
    assert world.recorded == [] and world.updates == []
    (rec,) = report.records
    assert rec.action == "already_recorded"
    assert rec.note == f"already recorded by alice-agent under {other_key}"


@pytest.mark.asyncio
async def test_a_close_unpinned_or_distant_pinned_peer_does_not_stop_the_write(
    monkeypatch,
) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.add("design/a", agent="alice-agent", pinned=False)
    world.add("design/b", agent="alice-agent", pinned=True)
    world.search_hits = [SimpleNamespace(memory_key="design/a", similarity=0.95)]
    await _run()
    world.search_hits = [SimpleNamespace(memory_key="design/b", similarity=0.6)]
    await _run()
    assert len(world.recorded) == 2


@pytest.mark.asyncio
async def test_the_section_cap_and_a_repeated_key_drop_candidates(monkeypatch) -> None:
    c = _candidate()
    other = _candidate(scenario="how the merge reads its runs", outcome="merge runs by key")
    world = _World(monkeypatch, [c, c, other])
    report = await _run(max_per_section=1)
    assert [d.reason for d in report.dropped] == [
        "same key as an earlier candidate",
        "the section already has --max-per-section 1",
    ]
    assert len(world.recorded) == 1


# A sentence a converted slide splits with box labels on lines of their own.
SPLIT = "Each shard keeps one replica per region\n{labels}so a read never leaves its region."
REPLICA_SPAN = "Each shard keeps one replica per region so a read never leaves its region."


@pytest.mark.parametrize("labels", ["Shard A\n", "Shard A\nShard B\n", "\nA\n\nB b\n  C c c\n\n"])
def test_a_span_is_read_across_up_to_three_label_lines(labels) -> None:
    text = SPLIT.format(labels=labels)
    found = dx.find_span(REPLICA_SPAN, ["Another chunk.", f"Intro. {text} Next."])
    assert found == text and found in f"Intro. {text} Next."


@pytest.mark.parametrize(
    "text",
    [
        SPLIT.format(labels="A\nB\nC\nD\n"),
        SPLIT.format(labels="Shard A of four\n"),
        "Each shard keeps one replica per region Shard A so a read never leaves its region.",
        "Each shard keeps one replica per region\nShard A so a read never leaves its region.",
        "Each shard keeps one replica per region Shard A\nso a read never leaves its region.",
    ],
)
def test_four_label_lines_a_long_one_or_one_inside_a_line_is_not_read_across(text) -> None:
    assert dx.find_span(REPLICA_SPAN, [text]) == ""


def test_a_span_is_matched_in_any_letter_case_and_found_as_written() -> None:
    text = "intro. each shard keeps one REPLICA per region so a read never leaves its region."
    assert dx.find_span(REPLICA_SPAN, [text]) == text[len("intro. ") :]


def test_a_span_split_across_two_chunks_is_not_found() -> None:
    first, second = SPLIT.format(labels="Shard A\n").split("Shard A\n")
    assert dx.find_span(REPLICA_SPAN, [first + "Shard A\n", second]) == ""


@pytest.mark.parametrize(
    ("text", "span"),
    [
        # Each line a word of the span or a label: the final word never matches.
        ("a\nx\n" * 100_000, " ".join(["a"] * 10 + ["b"])),
        # Every match reads across a `not` line and is rejected, one start at a time.
        ("a\nnot\n" * 100_000, "a a a"),
    ],
    ids=["labels", "rejected"],
)
def test_lines_that_all_look_like_labels_cost_linear_time(text, span) -> None:
    # 200,000 lines against a tenth of them: a matcher that tried every way to read each
    # gap would take exponential time on the final mismatch, and one that rescanned the
    # text per start, quadratic time. Linear time is ten times the small input's.
    def best(t: str) -> float:
        runs = []
        for _ in range(3):
            started = time.perf_counter()
            assert dx.find_span(span, [t]) == ""
            runs.append(time.perf_counter() - started)
        return min(runs)

    assert best(text) < 30 * best(text[: len(text) // 10])


@pytest.mark.parametrize(
    "text",
    [
        # A label that starts like the next word: `so` is not the start of `Something`.
        SPLIT.format(labels="Something\n"),
        "Each shard keeps one replica per region so\nAPI\na read never leaves its region.",
        "Each shard keeps one replica per region so\nIndex\na read never leaves its region.",
    ],
)
def test_a_label_starting_like_the_next_word_is_read_across(text) -> None:
    assert dx.find_span(REPLICA_SPAN, [text]) == text


@pytest.mark.parametrize(
    "label",
    [
        "not\n",
        "don't\n",
        "never twice\n",
        "- a list item\n",
        "+ a list item\n",
        "* a list item\n",
        "1. first\n",
        "1) first\n",
        "```\n",
        "~~~\n",
        "| a | b |\n",
        "## Heading\n",
        "> quoted\n",
        ": a definition\n",
        "===\n",
        "---\n",
        "<h2>Shards</h2>\n",
        "Shards differ.\n",
        "Why shard?\n",
        "One copy;\n",
        "分片。\n",
    ],
)
def test_a_line_that_is_structure_a_sentence_end_or_a_negation_is_no_label(label) -> None:
    assert dx.find_span(REPLICA_SPAN, [SPLIT.format(labels=label)]) == ""


def test_a_negation_line_is_not_skipped() -> None:
    assert dx.find_span("Retries are safe", ["Retries are\nnot\nsafe"]) == ""


def test_a_wrapped_paragraph_end_is_not_read_across() -> None:
    text = (
        "We cache plans per tenant because\nplanning is costly.\n\nEach plan expires after an hour."
    )
    span = "We cache plans per tenant because each plan expires after an hour."
    assert dx.find_span(span, [text]) == ""


@pytest.mark.parametrize(
    ("span", "text"),
    [
        (REPLICA_SPAN, SPLIT.format(labels="So\n")),
        (
            "We route each read to the nearest replica.",
            "We route each read to\nThe proxy\nthe nearest replica.",
        ),
    ],
)
def test_a_label_line_whose_first_word_reads_as_the_next_word_ends_the_match(span, text) -> None:
    # The word after the whitespace is tried first, so a stored span finds itself; the
    # cost is this one shape, which is not found.
    assert dx.find_span(span, [text]) == ""


def test_the_fewest_label_lines_that_reach_the_next_word_are_read() -> None:
    # A later "so a read" is not swallowed with the `not` line between as labels.
    text = "region\nL\nso a read\nnot\nso a read"
    assert dx.find_span("region so a read", [text]) == "region\nL\nso a read"


@pytest.mark.parametrize("labels", ["Shard A\nShard B\n", "Shard\nShard A\n", "So\n"])
def test_a_found_span_with_labels_finds_itself(labels) -> None:
    text = SPLIT.format(labels=labels)
    assert dx.find_span(text, [text]) == text


@pytest.mark.asyncio
async def test_a_span_with_labels_and_another_case_is_stored_as_the_documents(monkeypatch) -> None:
    text = SPLIT.format(labels="Shard A\nShard B\n")
    world = _World(
        monkeypatch,
        [
            _candidate(
                quote=REPLICA_SPAN,
                reasoning=REPLICA_SPAN.lower(),
                outcome="one replica per region",
                alternatives=[],
            )
        ],
    )
    _chunks(monkeypatch, (text, ("Spilling", "Final DISTINCT")))
    world.content = f"# Spilling\n\n## Final DISTINCT\n\n{text}\n"
    await _run()
    (inp,) = world.recorded
    # The document's words and case, its labels left out.
    assert inp.reasoning == REPLICA_SPAN
    # The retire step reads the stored span across the labels again: on the unchanged
    # document the row stays.
    world.reply = []
    world.record("decision:replica", {"reasoning": inp.reasoning})
    world.record("decision:gone", {"reasoning": "Each shard keeps two replicas per region."})
    report = await _run()
    assert report.retired == ["decision:gone"]
    # Once the document says otherwise, it retires.
    world.deletes.clear()
    changed = SPLIT.format(labels="Shard A\n").replace("one replica", "two replicas")
    _chunks(monkeypatch, (changed, ("Spilling", "Final DISTINCT")))
    world.content = f"# Spilling\n\n## Final DISTINCT\n\n{changed}\n"
    report = await _run()
    assert "decision:replica" in report.retired


@pytest.mark.asyncio
async def test_label_words_inside_a_reasoning_are_no_reason(monkeypatch) -> None:
    text = "We spill into sorted runs\nRun file\nlike ORDER BY."
    world = _World(
        monkeypatch,
        [
            _candidate(
                quote=text,
                reasoning="We spill into sorted runs like ORDER BY.",
                outcome="spill into sorted runs like ORDER BY",
                alternatives=[],
            )
        ],
    )
    _chunks(monkeypatch, (text, ("Spilling", "Final DISTINCT")))
    report = await _run()
    assert world.recorded == []
    assert [d.reason for d in report.dropped] == [dx.RESTATED]


# ------------------------------------------------------------------ retire


@pytest.mark.asyncio
async def test_rows_whose_reasoning_left_the_document_are_retired_or_flagged(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    gone = {"reasoning": "We keep a hash set."}
    world.record("decision:gone", gone)
    world.record("decision:gone-pinned", gone, pinned=True)
    world.record("decision:kept", {"reasoning": "A plain set on disk costs random reads."})
    world.record("decision:other-doc", gone, source_resource_id="knowledge:ns/other.md#x")
    world.record("decision:not-ours", gone, agent="claude")
    report = await _run()
    assert report.retired == ["decision:gone"]
    (d,) = world.deletes
    assert (d.memory_key, d.agent, d.version) == ("decision:gone", "loom-decisions", 7)
    assert report.flagged == ["decision:gone-pinned"]
    assert [u.memory_id for u in world.updates] == ["id-decision:gone-pinned-loom-decisions"]


@pytest.mark.asyncio
async def test_a_flagged_row_is_not_tagged_twice(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record(
        "decision:gone", {"reasoning": "gone"}, pinned=True, tags=["decision", "source-changed"]
    )
    report = await _run()
    assert report.flagged == [] and world.updates == []


@pytest.mark.asyncio
async def test_a_tagged_pinned_row_the_document_still_contradicts_is_not_tagged_again(
    monkeypatch, capsys
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set."},
        pinned=True,
        tags=["decision", "source-changed"],
    )
    report = await _run()
    (rec,) = report.records
    assert rec.action == "already_flagged" and report.flagged == [] and world.updates == []
    cli.print_report(report)
    assert f"already tagged source-changed {rec.memory_key}" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_dry_run_retires_nothing(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})
    report = await _run(dry_run=True)
    assert report.retired == ["decision:gone"] and world.deletes == []


# ------------------------------------------------------------------ errors and the CLI


@pytest.mark.asyncio
async def test_a_missing_document_or_one_without_chunks_writes_nothing(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [_candidate()])
    world.record("decision:gone", {"reasoning": "gone"})
    report = await _run(uri="missing.md")
    assert "no live document 'missing.md'" in report.error

    async def none(org, ns, uri):
        return []

    monkeypatch.setattr(document_reads, "document_chunks", none)
    report = await _run()
    assert "loom rechunk --org o --namespace ns --source-uri doc.md --apply" in report.error
    assert world.recorded == [] and world.deletes == [] and world.prompts == []


@pytest.mark.asyncio
async def test_an_unreadable_reply_is_reported(monkeypatch) -> None:
    from server.llm import client as llm_client

    _World(monkeypatch, [])

    class _Prose:
        async def complete(self, *a: Any, **k: Any) -> Any:
            return SimpleNamespace(text="There is one decision here.", stop_reason="")

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Prose())
    report = await _run()
    assert [d.reason for d in report.dropped] == ["the model's reply is not the JSON asked for"]


def test_the_command_is_dry_run_by_default() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli.register_decisions_cli(sub)
    args = parser.parse_args(
        ["decisions", "extract", "--org", "o", "--namespace", "ns", "--source-uri", "d"]
    )
    assert (args.subcommand, args.dry_run, args.max_per_section) == ("extract", True, 3)
    args = parser.parse_args(
        ["decisions", "extract", "--org", "o", "--namespace", "n", "--source-uri", "d", "--apply"]
    )
    assert args.dry_run is False


def test_a_cli_run_writes_as_the_decisions_command() -> None:
    from server.cli.app import _dispatch
    from server.memory.app.provenance import writer_actor

    seen: list[str] = []

    def _func(_: argparse.Namespace) -> int:
        seen.append(writer_actor())
        return 0

    ns = argparse.Namespace(cmd="decisions", subcommand="extract", func=_func)
    assert _dispatch(ns) == 0
    assert seen == ["process:cli:decisions"]


@pytest.mark.asyncio
async def test_a_chunk_with_only_an_image_is_not_sent_to_the_model(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk(uri, 1, "![diagram](img.png)", ("Spilling", "Architecture")),
        ]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "img.png" not in prompt[1] and "[section: Spilling > Final DISTINCT]" in prompt[1]
    assert report.image_only == ["Spilling > Architecture"]


# ------------------------------------------------------------------ refusals and ownership


def _refusal(code: str) -> Any:
    from fastapi import HTTPException

    return HTTPException(status_code=409, detail={"code": code, "message": "refused"})


@pytest.mark.asyncio
async def test_a_refused_record_is_reported_and_the_run_goes_on(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.record("decision:gone", {"reasoning": "gone"})
    world.refuse_record = _refusal("BLACKLISTED")
    report = await _run()
    assert [(d.section, d.reason) for d in report.dropped] == [
        (SECTION, f"{_key(_candidate())}: refused: BLACKLISTED")
    ]
    assert report.records == []
    assert report.retired == ["decision:gone"] and len(world.deletes) == 1


@pytest.mark.asyncio
async def test_the_source_changed_tag_goes_by_memory_id_and_a_refusal_is_reported(
    monkeypatch, capsys
) -> None:
    world = _World(monkeypatch, [])
    # Two identities hold the key with user and session `-`: memory.update would read that
    # as any and answer AMBIGUOUS_KEY; the tag is written by the extractor row's id.
    world.record("decision:gone", {"reasoning": "gone"}, pinned=True)
    world.add("decision:gone", agent="claude")
    await _run()
    (upd,) = world.updates
    assert upd.memory_id == "id-decision:gone-loom-decisions"
    world.refuse_update = _refusal("AMBIGUOUS_KEY")
    report = await _run()
    assert report.refused == ["decision:gone: refused: AMBIGUOUS_KEY"]
    cli.print_report(report)
    out = capsys.readouterr().out
    assert "not changed decision:gone: refused: AMBIGUOUS_KEY" in out
    assert report.flagged == [] and "tagged decision:gone" not in out


@pytest.mark.asyncio
async def test_a_refused_pinned_tag_drops_only_that_candidate(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, pinned=True)
    world.refuse_update = _refusal("AMBIGUOUS_KEY")
    report = await _run()
    assert [d.reason for d in report.dropped] == [f"{_key(c)}: refused: AMBIGUOUS_KEY"]


@pytest.mark.asyncio
async def test_retire_reads_the_extractors_own_record_not_another_writers(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    # Our row's reasoning still stands, but another identity recorded the key since: its
    # record is the active one, and its text is not in the document.
    world.add("decision:shared")
    world.record("decision:shared", {"reasoning": "Something else entirely."}, agent="claude")
    report = await _run()
    assert report.retired == [] and world.deletes == []


@pytest.mark.asyncio
async def test_a_hash_in_a_heading_or_in_a_source_uri_is_told_apart(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    assert await de.cites_document("o", "knowledge:ns/doc.md#Intro", "ns", "doc.md")
    # A heading holding `#` still cites this document while no `doc.md#<part>` exists.
    assert await de.cites_document("o", "knowledge:ns/doc.md#C# bindings", "ns", "doc.md")
    assert await de.cites_document("o", "knowledge:ns/doc.md#v2#Intro", "ns", "doc.md")
    world.docs.add("doc.md#v2")
    assert not await de.cites_document("o", "knowledge:ns/doc.md#v2#Intro", "ns", "doc.md")
    assert await de.cites_document("o", "knowledge:ns/doc.md#v2#Intro", "ns", "doc.md#v2")
    assert not await de.cites_document("o", "knowledge:ns/doc.md", "ns", "doc.md")
    assert not await de.cites_document("o", "knowledge:other/doc.md#Intro", "ns", "doc.md")


@pytest.mark.asyncio
async def test_retire_leaves_a_row_of_a_document_whose_uri_extends_this_one(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.docs.add("doc.md#v2")
    world.record(
        "decision:v2", {"reasoning": "gone"}, source_resource_id="knowledge:ns/doc.md#v2#Intro"
    )
    report = await _run()
    assert report.retired == [] and world.deletes == []


@pytest.mark.asyncio
async def test_a_hash_in_a_heading_keeps_the_row_ours_to_revise_and_retire(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    sharp = "knowledge:ns/doc.md#Spilling > C# bindings"
    world.record(
        _key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, source_resource_id=sharp
    )
    world.record("decision:gone", {"reasoning": "gone"}, source_resource_id=sharp)
    report = await _run()
    assert report.records[0].action == "revised"
    assert report.retired == ["decision:gone"]


@pytest.mark.asyncio
async def test_a_pinned_row_of_another_document_is_not_tagged(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    other = "knowledge:ns/other.md#Final DISTINCT"
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set."},
        agent="alice-agent",
        pinned=True,
        source_resource_id=other,
    )
    report = await _run()
    (rec,) = report.records
    assert rec.action == "not_ours" and rec.body["agent"] == "alice-agent"
    assert world.updates == [] and world.recorded == [] and report.flagged == []


@pytest.mark.asyncio
async def test_a_pinned_row_the_document_says_again_loses_source_changed(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c), _same_fields(c), files=[HEADER], pinned=True, tags=["decision", "source-changed"]
    )
    dry = await _run(dry_run=True)
    assert dry.records[0].note == "would clear source-changed" and world.updates == []
    report = await _run()
    assert report.records[0].action == "unchanged"
    assert report.records[0].note == "source-changed cleared"
    assert [(u.memory_id, u.tags) for u in world.updates] == [
        ("id-" + _key(c) + "-loom-decisions", ["decision"])
    ]


@pytest.mark.asyncio
async def test_a_tag_stays_on_a_run_that_does_not_emit_the_key(
    monkeypatch,
) -> None:
    # The pinned row carries source-changed, set by hand or by an earlier run on a changed
    # rejected reason; its reasoning is still in the document. This run's model leaves the
    # key out: the retire step keeps the tag.
    world = _World(monkeypatch, [])
    world.record(
        "decision:k",
        {"reasoning": "A plain set on disk costs random reads.", "outcome": "a hash set"},
        pinned=True,
        tags=["decision", "source-changed"],
    )
    report = await _run()
    assert world.updates == [] and report.flagged == [] and report.retired == []


@pytest.mark.asyncio
async def test_the_most_similar_close_hit_with_a_pin_skips_the_candidate(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.add("design/unpinned", agent="alice-agent")
    world.add("design/pinned", agent="alice-agent", pinned=True)
    world.add("design/far", agent="alice-agent", pinned=True)
    # Fused rank order is not similarity order: the first hit is unpinned, the third too far.
    world.search_hits = [
        SimpleNamespace(memory_key="design/unpinned", similarity=0.95),
        SimpleNamespace(memory_key="design/pinned", similarity=0.88),
        SimpleNamespace(memory_key="design/far", similarity=0.5),
    ]
    report = await _run()
    assert report.records[0].note == "already recorded by alice-agent under design/pinned"


@pytest.mark.asyncio
async def test_retire_reads_records_only_for_rows_of_this_document(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:here", {"reasoning": "gone"})
    world.record("decision:there", {"reasoning": "gone"}, source_resource_id="knowledge:ns/b#x")
    await _run()
    # One record read per extractor row of this document, shared by the prompt and retire.
    assert world.writer_reads == ["decision:here"]


@pytest.mark.parametrize("flag", [["--max-per-section", "0"], ["--window-chars", "100"]])
def test_the_command_refuses_a_cap_below_its_floor(flag: list[str]) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli.register_decisions_cli(sub)
    base = ["decisions", "extract", "--org", "o", "--namespace", "n", "--source-uri", "d"]
    with pytest.raises(SystemExit):
        parser.parse_args([*base, *flag])


@pytest.mark.asyncio
async def test_a_row_rewritten_before_its_delete_is_not_reported_retired(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})
    world.moved_on.add("decision:gone")
    report = await _run()
    assert report.retired == []
    assert report.races == ["decision:gone: rewritten since the run read it"]
    assert cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_a_rerun_that_words_the_scenario_anew_leaves_one_live_row(monkeypatch) -> None:
    first = _candidate()
    world = _World(monkeypatch, [first])
    await _run()
    world.record(_key(first), _same_fields(first), files=[HEADER])
    # The second run's model words the same decision differently: another key.
    second = _candidate(scenario="spilling the final DISTINCT")
    world.reply = [second]
    report = await _run()
    assert [r.action for r in report.records] == ["written"]
    assert report.replaced == [f"{_key(first)} -> {_key(second)}"]
    assert [d.memory_key for d in world.deletes] == [_key(first)]
    assert report.retired == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ban", [False, True])
async def test_a_case_only_change_under_a_reworded_scenario_leaves_one_live_row(
    monkeypatch, ban
) -> None:
    first = _ban() if ban else _candidate()
    world = _World(monkeypatch, [])
    fields = {**_same_fields(first), "reasoning": first["reasoning"].upper()}
    if ban:
        # A reason-less ban's outcome is its reasoning, upper case too.
        _chunks(monkeypatch, (_BAN, ("Spilling", "Final DISTINCT")))
        fields["outcome"] = _BAN.upper()
    # Recorded when the document wrote the sentence in upper case.
    world.record(_key(first), fields)
    second = {**first, "scenario": "spilling the final DISTINCT"}
    world.reply = [second]
    report = await _run()
    assert [r.action for r in report.records] == ["written"]
    assert report.replaced == [f"{_key(first)} -> {_key(second)}"]
    assert [d.memory_key for d in world.deletes] == [_key(first)]


@pytest.mark.asyncio
async def test_a_pinned_row_with_a_reasoning_recorded_anew_is_kept(monkeypatch) -> None:
    first = _candidate()
    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    world.record(_key(first), _same_fields(first), files=[HEADER], pinned=True)
    report = await _run()
    assert report.replaced == [] and world.deletes == []


@pytest.mark.asyncio
async def test_two_decisions_on_one_reasoning_both_stay_when_one_is_emitted(monkeypatch) -> None:
    a = _candidate()
    b = _candidate(scenario="how the merge orders its runs", outcome="merge runs by key")
    world = _World(monkeypatch, [a])
    world.record(_key(a), _same_fields(a), files=[HEADER])
    world.record(_key(b), _same_fields(b), files=[HEADER])
    report = await _run()
    assert report.records[0].action == "unchanged"
    assert report.replaced == [] and report.retired == [] and world.deletes == []


@pytest.mark.asyncio
async def test_an_unreadable_code_index_keeps_the_files_a_row_has(monkeypatch, capsys) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    await _run()
    (first,) = world.recorded
    assert first.files == [HEADER]
    world.record(_key(c), _same_fields(c), files=[HEADER])
    world.index_down = True
    report = await _run()
    (rec,) = report.records
    assert rec.action == "unchanged" and len(world.recorded) == 1
    assert rec.signals == ["ExternalDistinctTransform: camelcase, unknown (code index unavailable)"]
    # A changed decision is revised with the files the row has.
    world.reply = [{**c, "reasoning": "A plain set on disk costs random reads."}]
    report = await _run(dry_run=True)
    assert report.records[0].action == "revised" and report.records[0].body["files"] == [HEADER]


@pytest.mark.asyncio
async def test_a_scenario_with_no_key_letters_is_dropped_and_the_run_goes_on(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate(category="设计", scenario="如何溢出"), _candidate()])
    report = await _run()
    assert [d.reason for d in report.dropped] == [
        "no letters or digits a-z, 0-9 to build a key from"
    ]
    assert [r.action for r in report.records] == ["written"] and len(world.recorded) == 1


def test_a_blank_reason_drops_the_alternative_and_a_reason_is_read_stripped() -> None:
    (cand,) = (
        dx.parse_candidates(
            json.dumps(
                {
                    "decisions": [
                        _candidate(
                            alternatives=[
                                {"option": "a hash set", "reason": "   "},
                                {"option": "a plain set on disk", "reason": " costs random reads"},
                            ]
                        )
                    ]
                }
            )
        )
        or []
    )
    # The verbatim check matches whitespace aside, so stripping a span cannot make it match.
    assert cand.alternatives == [("a plain set on disk", "costs random reads")]


@pytest.mark.asyncio
async def test_a_decision_with_a_recorded_outcome_is_not_re_recorded(monkeypatch, capsys) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    # decision.outcome wrote the outcome; the code index now resolves a file the row lacks.
    world.record(
        _key(c),
        {**_same_fields(c), "realized_outcome": "no spill regressions", "success": True},
        files=[],
        tags=["decision", "decision:outcome-known", "decision:success"],
    )
    report = await _run()
    (rec,) = report.records
    assert rec.action == "outcome_known" and world.recorded == []
    cli.print_report(report)
    out = capsys.readouterr().out
    assert f"not revised {rec.memory_key}: its outcome is recorded" in out
    assert "decision.record {" in out


@pytest.mark.asyncio
async def test_a_reworded_row_with_a_recorded_outcome_is_kept(monkeypatch) -> None:
    first = _candidate()
    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    world.record(
        _key(first), {**_same_fields(first), "realized_outcome": "no regressions"}, files=[HEADER]
    )
    report = await _run()
    assert report.replaced == [] and world.deletes == []


@pytest.mark.asyncio
async def test_the_pinned_note_says_to_unpin_before_sending_the_body(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, pinned=True)
    report = await _run(dry_run=True)
    assert "unpin the row with memory.unpin, then send it" in report.records[0].note


# ------------------------------------------------------------------ model wording


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize(
    "reworded",
    [
        {"outcome": "spill sorted runs to disk"},
        {"alternatives": [{"option": "an on-disk hash set", "reason": "costs random reads"}]},
    ],
)
async def test_a_reworded_outcome_or_option_is_no_change(monkeypatch, reworded, pinned) -> None:
    # Outcome and option are the model's words: a re-run that words them anew neither
    # revises the extractor's row nor tags a pinned one.
    c = _candidate()
    world = _World(monkeypatch, [{**c, **reworded}])
    world.record(_key(c), _same_fields(c), files=[HEADER], pinned=pinned)
    report = await _run()
    assert [r.action for r in report.records] == ["unchanged"]
    assert world.recorded == [] and world.updates == []


@pytest.mark.asyncio
async def test_a_changed_rejected_reason_is_a_change(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c), {**_same_fields(c), "alternatives": "a plain set on disk — rejected: slow"}
    )
    report = await _run()
    assert report.records[0].action == "revised"


@pytest.mark.asyncio
async def test_the_prompt_lists_the_documents_decisions_to_reuse_their_wording(
    monkeypatch,
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "category": "design:spilling"}, files=[HEADER])
    world.record("decision:elsewhere", _same_fields(c), source_resource_id="knowledge:ns/b#x")
    await _run()
    system = world.prompts[0][0]
    assert "reuse its category and scenario exactly" in system
    assert (
        "- design:spilling | how the final DISTINCT spills to disk | spill into sorted runs"
        in system
    )
    assert system.count("\n- design:") == 1
    assert dx.system_prompt(3) == dx.SYSTEM.replace("{max_per_section}", "3")


@pytest.mark.asyncio
async def test_a_row_with_an_outcome_whose_reasoning_left_is_tagged_not_deleted(
    monkeypatch,
) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:done", {"reasoning": "gone", "realized_outcome": "it worked"})
    report = await _run()
    assert world.deletes == [] and report.retired == []
    assert report.flagged == ["decision:done"]
    assert [u.memory_id for u in world.updates] == ["id-decision:done-loom-decisions"]


@pytest.mark.asyncio
async def test_a_tag_write_on_a_row_deleted_since_the_read_is_reported(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, pinned=True)
    world.record("decision:gone", {"reasoning": "gone"}, pinned=True)
    world.gone = {f"id-{_key(c)}-loom-decisions", "id-decision:gone-loom-decisions"}
    report = await _run()
    assert report.records == [] and report.flagged == [] and report.dropped == []
    assert sorted(report.races) == [
        f"{_key(c)}: deleted since the run read it",
        "decision:gone: deleted since the run read it",
    ]
    assert report.refused == [] and cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_a_long_reason_cut_at_the_cap_survives_a_reworded_option(monkeypatch) -> None:
    long_reason = "costs random reads " + "and more random reads " * 14
    body = BODY + " " + long_reason
    c = _candidate(alternatives=[{"option": "a hash set", "reason": long_reason}])

    world = _World(monkeypatch, [c])
    from server.knowledge.app import document_reads

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, body, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    stored = svc.clean_alternatives([f"a hash set — rejected: {long_reason}"])
    assert len(stored[0]) == svc.ALTERNATIVE_CHARS  # cut inside the reason
    world.record(_key(c), {**_same_fields(c), "alternatives": stored[0]}, files=[HEADER])
    world.reply = [
        {**c, "alternatives": [{"option": "a hash-based set on disk", "reason": long_reason}]}
    ]
    report = await _run()
    assert report.records[0].action == "unchanged" and world.recorded == []


@pytest.mark.asyncio
async def test_alternatives_in_another_order_are_no_change(monkeypatch) -> None:
    reasons = [f"costs random reads {i}" for i in range(10)]
    alts = [{"option": f"option {i}", "reason": r} for i, r in enumerate(reasons)]
    body = BODY + " " + " ".join(reasons)
    c = _candidate(alternatives=alts)
    world = _World(monkeypatch, [{**c, "alternatives": list(reversed(alts))}])
    from server.knowledge.app import document_reads

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, body, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    world.record(_key(c), _same_fields(c), files=[HEADER])
    report = await _run()
    assert report.records[0].action == "unchanged"


@pytest.mark.asyncio
async def test_an_unpinned_row_the_document_says_again_loses_source_changed(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "realized_outcome": "no regressions"},
        files=[HEADER],
        tags=["decision", "source-changed"],
    )
    report = await _run()
    assert report.records[0].note == "source-changed cleared"
    assert [u.tags for u in world.updates] == [["decision"]]


@pytest.mark.asyncio
async def test_an_unchanged_key_another_identity_holds_replaces_nothing(monkeypatch) -> None:
    first = _candidate()
    second = _candidate(scenario="spilling the final DISTINCT")
    world = _World(monkeypatch, [second])
    world.record(_key(first), _same_fields(first), files=[HEADER])
    # The reworded key is another identity's, with the same decision: not ours to hold it.
    world.record(_key(second), _same_fields(second), agent="claude", files=[HEADER])
    report = await _run()
    assert report.records[0].action == "unchanged"
    assert report.replaced == [] and world.deletes == []


@pytest.mark.asyncio
async def test_the_restatement_search_makes_no_model_call(monkeypatch) -> None:
    from server.memory.app import search

    world = _World(monkeypatch, [_candidate()])
    seen: list[Any] = []

    async def mem_search(inp):
        seen.append(inp)
        return SimpleNamespace(results=[])

    monkeypatch.setattr(search, "handle_memory_search", mem_search)
    await _run()
    (inp,) = seen
    assert (inp.search_mode, inp.rerank, inp.hyde, inp.retrieval_budget) == (
        "semantic",
        "none",
        False,
        "fast",
    )
    assert world.recorded


def _cut(cap: int) -> str:
    return (
        f"reply cut at the token limit ({cap}); lower --window-chars or --max-per-section, or "
        "raise decision_extract in LOOM_LLM_MAX_TOKENS_OVERRIDES"
    )


def _cut_client(monkeypatch, stop: str) -> list[int]:
    """A model that answers a cut-off reply; returns the max_tokens each call asked for."""
    from server.llm import client as llm_client

    asked: list[int] = []

    class _Cut:
        async def complete(self, *a: Any, max_tokens: int, **k: Any) -> Any:
            asked.append(max_tokens)
            return SimpleNamespace(text='{"decisions": [{"category": "des', stop_reason=stop)

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Cut())
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "reason"),
    [
        ("length", _cut(4000)),
        ("max_tokens", _cut(4000)),
        (
            "content_filter",
            "the model's reply is not the JSON asked for (stop reason: content_filter)",
        ),
    ],
)
async def test_a_reply_cut_short_says_why(monkeypatch, stop, reason) -> None:
    _World(monkeypatch, [])
    asked = _cut_client(monkeypatch, stop)
    report = await _run()
    assert [d.reason for d in report.dropped] == [reason]
    assert asked == [4000]


@pytest.mark.asyncio
async def test_a_deployment_cap_reaches_the_call_and_the_cut_reply_names_it(monkeypatch) -> None:
    from server.settings import settings

    _World(monkeypatch, [])
    asked = _cut_client(monkeypatch, "length")
    monkeypatch.setattr(settings, "llm_max_tokens_overrides", '{"decision_extract": 1234}')
    report = await _run()
    assert asked == [1234]
    assert [d.reason for d in report.dropped] == [_cut(1234)]


@pytest.mark.asyncio
async def test_a_changed_decision_with_an_outcome_is_flagged(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set.", "realized_outcome": "it held"},
        files=[HEADER],
    )
    report = await _run()
    (rec,) = report.records
    assert rec.action == "flagged" and report.flagged == [rec.memory_key]
    assert world.recorded == [] and [u.tags for u in world.updates] == [
        ["decision", "source-changed"]
    ]


@pytest.mark.asyncio
async def test_a_files_only_change_on_a_decision_with_an_outcome_is_left(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "realized_outcome": "it held"}, files=[])
    report = await _run()
    assert report.records[0].action == "outcome_known" and world.updates == []


def test_an_alternative_cut_before_its_reason_matches_any_one_reason() -> None:
    from server.memory.app.decision_extract import _same_reasons

    option = "o" * (svc.ALTERNATIVE_CHARS + 20)
    (stored,) = svc.clean_alternatives([f"{option} — rejected: costs random reads"])
    assert dx.REJECTED not in stored
    assert _same_reasons([stored], [f"{option} — rejected: costs random reads"])
    assert _same_reasons([stored], ["another wording — rejected: costs random reads"])
    assert not _same_reasons([stored], [])
    other = "a hash set — rejected: slow"
    assert _same_reasons([other, stored], ["a hash set — rejected: slow", "x — rejected: y"])


def test_a_backticked_snake_case_name_is_looked_up_first() -> None:
    m = dx.sort_mentions(
        ["handle_decision_record", "max_threads"], "`handle_decision_record` and max_threads"
    )
    assert m.symbols == [("handle_decision_record", "qualified")]
    assert m.unresolved == [("max_threads", "setting")]


@pytest.mark.asyncio
async def test_a_backticked_name_the_index_lacks_is_reported_as_a_setting(monkeypatch) -> None:
    c = _candidate(mentions=["max_bytes_before_external_group_by", "handle_decision_record"])
    world = _World(monkeypatch, [c])
    from server.knowledge.app import document_reads

    body = BODY.replace(
        "max_bytes_before_external_group_by", "`max_bytes_before_external_group_by`"
    )
    body += " `handle_decision_record` writes it."

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, body, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    world.symbols["handle_decision_record"] = [
        ("server/memory/app/decision.py", "handle_decision_record")
    ]
    report = await _run(dry_run=True)
    (rec,) = report.records
    assert rec.unresolved == ["max_bytes_before_external_group_by (setting)"]
    assert "server/memory/app/decision.py" in rec.body["files"]


@pytest.mark.asyncio
async def test_eleven_alternatives_fit_decision_record_as_it_keeps_them(monkeypatch) -> None:
    reasons = [f"costs random reads {i}" for i in range(11)]
    alts = [{"option": f"option {i}", "reason": r} for i, r in enumerate(reasons)]
    # A case-only duplicate of the first, which decision.record folds away too.
    alts.append({"option": "OPTION 0", "reason": reasons[0]})
    body = BODY + " " + " ".join(reasons)
    world = _World(monkeypatch, [_candidate(alternatives=alts)])
    from server.knowledge.app import document_reads

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, body, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    assert [r.action for r in report.records] == ["written"] and report.dropped == []
    (inp,) = world.recorded
    assert len(inp.alternatives) == svc.MAX_ALTERNATIVES
    assert inp.alternatives[0] == "option 0 — rejected: costs random reads 0"


@pytest.mark.asyncio
async def test_the_extractors_rows_are_read_once_per_run(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.record("decision:here", {"reasoning": "gone"})
    await _run()
    assert world.bulk_reads == 1


@pytest.mark.asyncio
async def test_one_decision_worded_twice_across_overlapping_windows_is_written_once(
    monkeypatch,
) -> None:
    from server.knowledge.app import document_reads

    first = _candidate()
    second = _candidate(scenario="spilling the final DISTINCT")
    world = _World(monkeypatch, [])
    replies = [[first], [second]]

    class _TwoWindows:
        async def complete(self, *a: Any, **k: Any) -> Any:
            return SimpleNamespace(text=json.dumps({"decisions": replies.pop(0)}), stop_reason="")

    async def chunks(org, ns, uri):
        # Two chunks whose stride overlaps: each window holds the same text.
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Final DISTINCT")),
        ]

    from server.llm import client as llm_client

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _TwoWindows())
    report = await _run(window_chars=len(BODY) + 1)
    assert report.windows == 2
    assert [d.reason for d in report.dropped] == ["same decision as an earlier candidate"]
    assert len(world.recorded) == 1


@pytest.mark.asyncio
async def test_an_outcome_row_with_stale_fields_the_document_says_again_is_cleared(
    monkeypatch,
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "realized_outcome": "it held"},
        files=[HEADER],
        source_resource_id="knowledge:ns/doc.md#Old heading",
        tags=["decision", "source-changed"],
    )
    report = await _run()
    (rec,) = report.records
    assert rec.action == "outcome_known"
    assert rec.note == "its outcome is recorded; re-recording would lose it; source-changed cleared"
    assert world.recorded == [] and [u.tags for u in world.updates] == [["decision"]]


@pytest.mark.asyncio
async def test_an_unfit_candidate_takes_no_section_slot(monkeypatch) -> None:
    unfit = _candidate(category="design:" + "x" * 200)
    world = _World(monkeypatch, [unfit, _candidate()])
    report = await _run(max_per_section=1)
    assert [d.reason for d in report.dropped] == ["does not fit decision.record"]
    assert len(world.recorded) == 1 and world.recorded[0].category == "design:spilling"


@pytest.mark.asyncio
async def test_a_long_scenario_shown_whole_keeps_its_key_across_runs(monkeypatch) -> None:
    from server.llm import client as llm_client

    scenario = "how the final DISTINCT spills to disk " + "under memory pressure " * 9
    assert len(scenario) > 160
    c = _candidate(scenario=scenario)
    world = _World(monkeypatch, [c])
    await _run()
    (first,) = world.recorded
    world.record(_key(c), _same_fields(c), files=[HEADER])

    class _Reuser:
        """A model that copies the scenario the prompt lists."""

        async def complete(self, system: str, user: str, **k: Any) -> Any:
            listed = system.rsplit("\n- ", 1)[1].split(" | ")
            reply = {**c, "category": listed[0], "scenario": listed[1]}
            return SimpleNamespace(text=json.dumps({"decisions": [reply]}), stop_reason="")

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Reuser())
    report = await _run()
    assert report.records[0].memory_key == _key(c) == svc.decision_key(first.category, scenario)
    assert report.records[0].action == "unchanged"


def test_a_long_outcome_is_shortened_and_marked_in_the_prompt() -> None:
    prompt = dx.system_prompt(3, [("design:x", "a scenario", "o" * 300)])
    line = prompt.rsplit("\n- ", 1)[1]
    assert line.startswith("design:x | a scenario | ") and line.endswith("…")
    assert len(line.split(" | ")[2]) == 160
    assert "an outcome ending in … is shortened" in prompt


@pytest.mark.asyncio
async def test_the_prompt_lists_at_most_the_configured_known_decisions(monkeypatch) -> None:
    from server.settings import settings

    world = _World(monkeypatch, [])
    for i in range(3):
        fields = {"category": "design", "scenario": f"known {i}", "outcome": "o"}
        world.record(f"decision:known-{i}", fields)
    monkeypatch.setattr(settings, "memory_decision_extract_known_max", 2)
    await _run()
    listed = world.prompts[0][0].split("outcome as far as it is shown:\n", 1)[1]
    assert len(listed.splitlines()) == 2 and "known 2" not in listed


def test_clean_alternatives_can_keep_them_uncut() -> None:
    long = "a hash set — rejected: " + "slow " * 80
    assert len(svc.clean_alternatives([long])[0]) == svc.ALTERNATIVE_CHARS
    assert svc.clean_alternatives([long, long.upper()], cut=False) == [" ".join(long.split())]


_ATTESTED = {"verified_by": "human:alice", "verified_sha256": "sha-1", "content_sha256": "sha-1"}


@pytest.mark.asyncio
async def test_an_attested_row_the_document_contradicts_is_tagged_not_revised(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, **_ATTESTED)
    report = await _run()
    (rec,) = report.records
    assert rec.action == "flagged" and world.recorded == []
    assert "attest the new decision again with memory.verify" in rec.note
    assert [u.tags for u in world.updates] == [["decision", "source-changed"]]


@pytest.mark.asyncio
async def test_an_attested_row_whose_reasoning_left_is_tagged_not_deleted(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"}, **_ATTESTED)
    report = await _run()
    assert world.deletes == [] and report.flagged == ["decision:gone"]


@pytest.mark.asyncio
async def test_a_stale_attestation_does_not_protect_a_row(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"}, **{**_ATTESTED, "content_sha256": "sha-2"})
    report = await _run()
    assert report.retired == ["decision:gone"] and report.flagged == []


@pytest.mark.asyncio
async def test_an_attested_restatement_under_another_key_is_skipped(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    world.add("decision:attested", **_ATTESTED)
    world.search_hits = [SimpleNamespace(memory_key="decision:attested", similarity=0.9)]
    report = await _run()
    assert world.recorded == []
    assert report.records[0].note == "already recorded by loom-decisions under decision:attested"


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"quote": "We spill.", "reasoning": "We spill."}, "quote too short to rest a decision on"),
        ({"reasoning": "We spill"}, "reasoning too short to rest a decision on"),
        # 12 characters as the model wrote them, 9 as the document has them.
        ({"reasoning": "We    spill."}, "reasoning too short to rest a decision on"),
        ({"quote": "We    spill."}, "quote too short to rest a decision on"),
        (
            {"alternatives": [{"option": "a hash set", "reason": "random    read"}]},
            "the reason given for rejecting 'a hash set' is too short to rest on",
        ),
        (
            {"alternatives": [{"option": "a hash set", "reason": "random"}]},
            "the reason given for rejecting 'a hash set' is too short to rest on",
        ),
    ],
)
def test_a_span_too_short_to_rest_a_decision_on_is_dropped(over, reason) -> None:
    assert dx.verbatim_failure(_cand(**over), [BODY]) == reason


@pytest.mark.asyncio
async def test_a_tag_write_on_a_value_changed_since_the_read_is_a_race(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"}, pinned=True)
    world.value_moved = {"id-decision:gone-loom-decisions"}
    report = await _run()
    assert report.races == ["decision:gone: rewritten since the run read it"]
    assert report.refused == [] and report.flagged == [] and world.updates == []
    assert world.rows[0].tags == ["decision"] and cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_a_tag_clear_on_a_value_changed_since_the_read_is_a_race(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c), _same_fields(c), files=[HEADER], pinned=True, tags=["decision", "source-changed"]
    )
    world.value_moved = {f"id-{_key(c)}-loom-decisions"}
    report = await _run()
    (rec,) = report.records
    # Reported once, as a race; the record's note does not repeat it.
    assert rec.note == ""
    assert report.races == [f"{_key(c)}: rewritten since the run read it"]
    assert cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_a_flag_on_a_value_changed_since_the_read_is_a_race(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, pinned=True)
    world.value_moved = {f"id-{_key(c)}-loom-decisions"}
    report = await _run()
    assert report.records == [] and report.dropped == []
    assert report.races == [f"{_key(c)}: rewritten since the run read it"]
    assert cli.exit_code(report) == 0


def test_an_exact_reason_is_matched_before_a_cut_one_takes_it() -> None:
    from server.memory.app.decision_extract import _same_reasons

    exact = "a plain set — rejected: costs random reads"
    # Cut at the cap inside its reason, so its stored prefix is only "costs".
    option = "o" * (svc.ALTERNATIVE_CHARS - len(" — rejected: costs"))
    (cut,) = svc.clean_alternatives([f"{option} — rejected: costs random reads and memory"])
    assert cut.endswith("rejected: costs")
    # Reordered, the exact one first: a greedy prefix match of the cut one would take it.
    candidate = [exact, f"{option} — rejected: costs random reads and memory"]
    assert _same_reasons([cut, exact], candidate)


@pytest.mark.asyncio
async def test_each_write_leaves_the_audit_row_the_transport_would(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record("decision:gone", {"reasoning": "gone"})
    world.record("decision:flag", {"reasoning": "gone"}, pinned=True)
    from server.memory.domain.provenance import cli_actor, pipeline_actor

    with pipeline_actor(cli_actor("decisions")):
        await _run()
    events = [(e, a) for e, a, _ in world.audits]
    # decision.record and memory.delete_version declare an audit row; memory.update_by_id
    # does not, as over the transport.
    assert sorted(events) == [
        ("decision.record", "process:cli:decisions"),
        ("memory.delete_version", "process:cli:decisions"),
    ]
    record_subject = next(s for e, _, s in world.audits if e == "decision.record")
    assert record_subject["agent"] == "loom-decisions" and record_subject["category"]
    assert len(world.updates) == 1


@pytest.mark.asyncio
async def test_a_dry_run_leaves_no_audit_row(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.record("decision:gone", {"reasoning": "gone"})
    await _run(dry_run=True)
    assert world.audits == []


def test_the_audited_ops_declare_an_audit_row_with_the_input_as_subject() -> None:
    from server.memory.api.ops_crud import memory_delete_version
    from server.memory.api.ops_decision import decision_record

    for op in (decision_record, memory_delete_version):
        assert op.audit and op.audit_subject is None


def test_of_two_cut_reasons_sharing_a_prefix_the_longer_is_matched_first() -> None:
    from server.memory.app.decision_extract import _same_reasons

    def cut_at(reason_kept: str, full: str) -> tuple[str, str]:
        option = "o" * (svc.ALTERNATIVE_CHARS - len(" — rejected: " + reason_kept))
        (stored,) = svc.clean_alternatives([f"{option} — rejected: {full}"])
        assert stored.endswith(f"rejected: {reason_kept}")
        return stored, f"{option} — rejected: {full}"

    short, short_full = cut_at("costs", "costs memory")
    long, long_full = cut_at("costs random reads and", "costs random reads and more")
    # Both orders: the short prefix must not take the long one's candidate first.
    assert _same_reasons([short, long], [long_full, short_full])
    assert _same_reasons([long, short], [short_full, long_full])


def test_a_non_integer_cap_is_refused_with_a_reason(capsys) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli.register_decisions_cli(sub)
    base = ["decisions", "extract", "--org", "o", "--namespace", "n", "--source-uri", "d"]
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--max-per-section", "three"])
    assert "must be an integer, at least 1" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_backticked_name_defined_in_two_files_is_no_setting(monkeypatch) -> None:
    c = _candidate(mentions=["handle_decision_record"])
    world = _World(monkeypatch, [c])
    from server.knowledge.app import document_reads

    body = BODY + " `handle_decision_record` writes it."

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, body, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    world.symbols["handle_decision_record"] = [
        ("server/memory/app/decision.py", "handle_decision_record"),
        ("server/legacy/decision.py", "handle_decision_record"),
    ]
    report = await _run(dry_run=True)
    (rec,) = report.records
    assert rec.signals == ["handle_decision_record: qualified, no single definition"]
    assert rec.unresolved == ["handle_decision_record (several definitions)"]
    assert rec.body["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["record", "update", "delete"])
async def test_a_server_error_on_a_write_is_reported_and_the_run_goes_on(
    monkeypatch, where
) -> None:
    from server.core.errors import PreconditionFailedError

    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record("decision:gone", {"reasoning": "gone"})
    world.record("decision:flag", {"reasoning": "gone"}, pinned=True)
    busy = PreconditionFailedError("the key is busy")
    setattr(world, f"refuse_{where}", busy)
    report = await _run()
    reasons = [d.reason for d in report.dropped] + report.refused
    assert any("refused: PRECONDITION_FAILED" in r for r in reasons)
    # A busy key carries no live hash: a refusal, not a race.
    assert cli.exit_code(report) == 2 and report.races == []
    # The other writes still ran.
    if where != "record":
        assert len(world.recorded) == 1
    if where != "delete":
        assert report.retired == ["decision:gone"]
    if where != "update":
        assert report.flagged == ["decision:flag"]


@pytest.mark.asyncio
async def test_a_path_the_document_writes_out_is_a_file(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    body = "We keep the merge in src/Storages/MergeTreeData.cpp because it owns the parts."
    c = _candidate(
        quote=body,
        reasoning="We keep the merge in src/Storages/MergeTreeData.cpp because it owns the parts.",
        alternatives=[],
        mentions=[],
    )
    world = _World(monkeypatch, [c])

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, body, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    await _run()
    (inp,) = world.recorded
    assert inp.files == ["src/Storages/MergeTreeData.cpp"]


@pytest.mark.asyncio
async def test_an_unchanged_decision_is_held_by_its_stored_outcome(monkeypatch) -> None:
    # This run words the outcome anew under the old key (no change), and an older row
    # under a reworded key holds the stored outcome: it is the duplicate to replace.
    c = _candidate()
    world = _World(monkeypatch, [{**c, "outcome": "spill sorted runs to disk"}])
    world.record(_key(c), _same_fields(c), files=[HEADER])
    world.record("decision:older", _same_fields(c), files=[HEADER])
    report = await _run()
    assert report.records[0].action == "unchanged"
    assert report.replaced == [f"decision:older -> {_key(c)}"]


def test_an_error_goes_to_stderr(capsys) -> None:
    report = de.ExtractReport("missing.md", True, error="no live document")
    cli.print_report(report)
    out = capsys.readouterr()
    assert out.out == "" and out.err == "error: no live document\n"


def test_an_attestation_binding_the_value_of_a_row_with_no_hash_column_confirms_it() -> None:
    from server.core.hashing import sha256_value_json
    from server.memory.app.decision_extract import _confirmed

    value = "Decided spill."
    bound = sha256_value_json(json.dumps(value))
    row = _row("decision:k", value=value, content_sha256="", verified_by="human:alice")
    assert _confirmed(_Row(**{**vars(row), "verified_sha256": bound}))
    assert not _confirmed(_Row(**{**vars(row), "verified_sha256": "other"}))


def test_a_list_field_the_model_filled_with_something_else_reads_empty() -> None:
    reply = json.dumps({"decisions": [_candidate(mentions=3, alternatives=True)]})
    (cand,) = dx.parse_candidates(reply) or []
    assert cand.mentions == [] and cand.alternatives == []


@pytest.mark.asyncio
async def test_a_keyed_provider_without_its_key_refuses_before_any_model_call(
    monkeypatch,
) -> None:
    from server.llm import client as llm_client
    from server.settings import settings

    world = _World(monkeypatch, [_candidate()])
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(llm_client, "provider_key_missing", lambda provider: True)
    report = await _run()
    assert report.error.startswith("LOOM_LLM_PROVIDER=openai has no API key set")
    assert world.prompts == [] and world.recorded == []
    assert cli.exit_code(report) == 1


@pytest.mark.asyncio
async def test_a_provider_configured_as_fake_runs(monkeypatch) -> None:
    from server.settings import settings

    world = _World(monkeypatch, [_candidate()])
    monkeypatch.setattr(settings, "llm_provider", "fake")
    report = await _run()
    assert report.error == "" and len(world.recorded) == 1


@pytest.mark.asyncio
async def test_a_failed_model_call_loses_its_window_only(monkeypatch) -> None:
    import httpx

    from server.knowledge.app import document_reads
    from server.llm import client as llm_client

    world = _World(monkeypatch, [])
    replies: list[Any] = [httpx.ReadTimeout("slow"), [_candidate()]]

    class _Flaky:
        async def complete(self, *a: Any, **k: Any) -> Any:
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return SimpleNamespace(text=json.dumps({"decisions": reply}), stop_reason="")

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Other")),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Final DISTINCT")),
        ]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Flaky())
    report = await _run(window_chars=len(BODY) + 1)
    assert [d.reason for d in report.dropped] == ["the model call failed: ReadTimeout"]
    assert len(world.recorded) == 1


@pytest.mark.asyncio
async def test_an_inferred_fake_provider_refuses_before_any_model_call(monkeypatch) -> None:
    from server.settings import Settings

    world = _World(monkeypatch, [_candidate()])
    monkeypatch.setattr(Settings, "llm_provider_inferred", lambda self: True)
    report = await _run()
    assert report.error.startswith("no LLM provider is configured")
    assert world.prompts == [] and world.recorded == [] and cli.exit_code(report) == 1


@pytest.mark.asyncio
async def test_a_run_whose_every_model_call_fails_reads_nothing_and_retires_nothing(
    monkeypatch, capsys
) -> None:
    import httpx

    from server.llm import client as llm_client

    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})

    class _Down:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise httpx.ConnectError("down")

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Down())
    report = await _run()
    assert report.error.startswith("no window was read")
    assert world.deletes == [] and report.retired == [] and cli.exit_code(report) == 1
    cli.print_report(report)
    out = capsys.readouterr()
    assert "error: no window was read" in out.err
    assert "the model call failed: ConnectError" in out.out


@pytest.mark.asyncio
async def test_a_row_deleted_before_its_delete_is_a_race(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})
    world.deleted_before.add("decision:gone")
    report = await _run()
    assert report.races == ["decision:gone: deleted since the run read it"]
    assert report.refused == [] and cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_a_refused_record_is_listed_once_and_counted(monkeypatch, capsys) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.refuse_record = _refusal("BLACKLISTED")
    report = await _run()
    assert [(d.reason, d.refused) for d in report.dropped] == [
        (f"{_key(c)}: refused: BLACKLISTED", True)
    ]
    assert report.refused == [] and cli.exit_code(report) == 2
    cli.print_report(report)
    out = capsys.readouterr().out
    assert out.count("refused: BLACKLISTED") == 1
    assert f"dropped [{SECTION}] {_key(c)}: refused: BLACKLISTED" in out


def _fake_fallback(monkeypatch, answers: list[str]) -> None:
    """A real provider configured (inferred `openai`) whose fallback with no key answered:
    each window's reply comes from the provider named in `answers`."""
    from server.llm import client as llm_client
    from server.settings import settings

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(llm_client, "provider_key_missing", lambda provider: False)

    class _Primary:
        async def complete(self, *a: Any, **k: Any) -> Any:
            provider = answers.pop(0)
            return SimpleNamespace(
                text=json.dumps({"decisions": [_candidate()]}),
                stop_reason="",
                provider=provider,
            )

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Primary())


async def _two_windows(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Other")),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Final DISTINCT")),
        ]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)


@pytest.mark.asyncio
async def test_a_window_the_fallback_fake_answered_is_dropped(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    await _two_windows(monkeypatch)
    _fake_fallback(monkeypatch, ["fake", "openai"])
    report = await _run(window_chars=len(BODY) + 1)
    assert [d.reason for d in report.dropped] == [de._FAKE_ANSWER]
    assert len(world.recorded) == 1 and report.error == ""


@pytest.mark.asyncio
async def test_a_run_the_fallback_fake_answered_wholly_reads_nothing(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})
    await _two_windows(monkeypatch)
    _fake_fallback(monkeypatch, ["fake", "fake"])
    report = await _run(window_chars=len(BODY) + 1)
    assert report.error == (
        "no window was read: every model call failed, was answered by the fake model, came "
        "back unreadable, or answered nothing and failed when read again"
    )
    assert cli.exit_code(report) == 1
    assert world.recorded == [] and world.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        "httpx",
        "timeout",
    ],
)
async def test_an_embedding_provider_error_on_a_write_is_reported(monkeypatch, error) -> None:
    import httpx

    raised: Exception = (
        httpx.ConnectError("embedder down") if error == "httpx" else TimeoutError("slow")
    )
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record("decision:gone", {"reasoning": "gone"})
    world.refuse_record = raised
    world.refuse_delete = raised
    report = await _run()
    name = type(raised).__name__
    assert [d.reason for d in report.dropped] == [f"{_key(c)}: failed: {name}"]
    assert [d.refused for d in report.dropped] == [True]
    assert report.refused == [f"decision:gone: failed: {name}"]
    assert cli.exit_code(report) == 2


@pytest.mark.asyncio
async def test_a_dry_run_whose_restatement_search_fails_exits_2(monkeypatch) -> None:
    import httpx

    from server.memory.app import search

    c = _candidate()
    world = _World(monkeypatch, [c])
    request = httpx.Request("POST", "http://embedder.invalid/v1/embeddings")

    async def failing_search(inp):
        raise httpx.HTTPStatusError(
            "503", request=request, response=httpx.Response(503, request=request)
        )

    monkeypatch.setattr(search, "handle_memory_search", failing_search)
    report = await _run(dry_run=True)
    assert [d.reason for d in report.dropped] == [f"{_key(c)}: failed: HTTPStatusError"]
    assert report.dropped[0].refused and report.refused == []
    assert world.recorded == [] and cli.exit_code(report) == 2


@pytest.mark.asyncio
async def test_a_refused_revise_leaves_its_row_for_the_next_run(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    # The row's reasoning is gone from the document; the candidate revises it, refused.
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."})
    world.refuse_record = _refusal("BLACKLISTED")
    report = await _run()
    assert [d.reason for d in report.dropped] == [f"{_key(c)}: refused: BLACKLISTED"]
    assert report.refused == [] and world.deletes == [] and report.retired == []


@pytest.mark.asyncio
async def test_a_refused_tag_write_is_not_tried_again_by_retire(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, pinned=True)
    world.refuse_update = _refusal("AMBIGUOUS_KEY")
    report = await _run()
    assert [d.reason for d in report.dropped] == [f"{_key(c)}: refused: AMBIGUOUS_KEY"]
    assert report.refused == [] and report.flagged == []


@pytest.mark.asyncio
async def test_unreadable_extractor_rows_stop_the_run_before_any_model_call(monkeypatch) -> None:
    import httpx

    world = _World(monkeypatch, [_candidate()])

    async def down(*a: Any, **k: Any) -> Any:
        raise httpx.ConnectError("store down")

    monkeypatch.setattr(de, "list_by_namespace", down)
    report = await _run()
    assert report.error == "could not read the extractor's rows: failed: ConnectError"
    assert world.prompts == [] and world.recorded == [] and cli.exit_code(report) == 1


@pytest.mark.asyncio
async def test_a_failed_record_read_stops_the_run_before_any_model_call(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.record("decision:gone", {"reasoning": "gone"})

    async def slow(org, ns, key, ch=None):
        raise TimeoutError("slow")

    monkeypatch.setattr(de, "live_writer_record", slow)
    report = await _run()
    assert report.error == "could not read the extractor's rows: failed: TimeoutError"
    assert world.deletes == [] and world.prompts == [] and cli.exit_code(report) == 1


@pytest.mark.parametrize(
    ("dropped", "refused", "code"),
    [(True, [], 2), (False, [], 0), (False, ["decision:gone: refused: X"], 2)],
)
def test_a_refused_candidate_or_retire_write_exits_2(dropped, refused, code) -> None:
    report = de.ExtractReport("d", False)
    report.dropped = [de.Dropped("s", "refused: X" if dropped else "x", refused=dropped)]
    report.refused = refused
    assert cli.exit_code(report) == code


@pytest.mark.asyncio
@pytest.mark.parametrize(("top_k", "blocked"), [(6, True), (5, False)])
async def test_a_confirmed_restatement_below_five_closer_hits_blocks_within_top_k(
    monkeypatch, top_k, blocked
) -> None:
    """The confirmed key is the sixth closest: a top_k of 6 reaches it, 5 stops before it
    though the fetch (5 keys times a per-key cap of 3) still serves it."""
    from server.memory.app import search
    from server.settings import settings

    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    hits = []
    for i in range(5):
        world.add(f"design/near{i}", agent="alice-agent")
        hits.append(SimpleNamespace(memory_key=f"design/near{i}", similarity=0.95 - i / 100))
    world.add("design/confirmed", agent="alice-agent", pinned=True)
    hits.append(SimpleNamespace(memory_key="design/confirmed", similarity=0.86))
    asked: list[int] = []

    async def mem_search(inp):
        asked.append(inp.top_k)
        return SimpleNamespace(results=hits[: inp.top_k])

    monkeypatch.setattr(search, "handle_memory_search", mem_search)
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    monkeypatch.setattr(settings, "memory_decision_extract_restatement_top_k", top_k)
    report = await _run()
    assert asked == [per_key_fetch(top_k)] and per_key_fetch(top_k) >= len(hits)
    if blocked:
        assert world.recorded == []
        assert report.records[0].note == "already recorded by alice-agent under design/confirmed"
    else:
        assert len(world.recorded) == 1


@pytest.mark.asyncio
async def test_a_confirmed_restatement_below_keys_of_many_rows_still_blocks(monkeypatch) -> None:
    # the search serves a row per writer and session: seven closer keys of three rows each
    # fill 21 places, and the confirmed key still counts among the 20 closest keys
    from server.memory.app import search
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    hits = []
    for i in range(7):
        world.add(f"design/near{i}", agent="alice-agent")
        hits += [SimpleNamespace(memory_key=f"design/near{i}", similarity=0.95 - i / 100)] * 3
    world.add("design/confirmed", agent="alice-agent", pinned=True)
    hits.append(SimpleNamespace(memory_key="design/confirmed", similarity=0.86))

    async def mem_search(inp):
        return SimpleNamespace(results=hits[: inp.top_k])

    read: list[str] = []
    rows_of_key = de._rows_of_key

    async def counted(org, namespace, key):
        read.append(key)
        return await rows_of_key(org, namespace, key)

    monkeypatch.setattr(search, "handle_memory_search", mem_search)
    monkeypatch.setattr(de, "_rows_of_key", counted)
    report = await _run()
    assert world.recorded == []
    assert report.records[0].note == "already recorded by alice-agent under design/confirmed"
    # each key's rows are read once
    assert read == [*(f"design/near{i}" for i in range(7)), "design/confirmed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("answers", "code"), [(["fake", "openai"], 2), (["openai", "openai"], 0)])
async def test_a_partly_read_document_exits_2(monkeypatch, answers, code) -> None:
    _World(monkeypatch, [])
    await _two_windows(monkeypatch)
    _fake_fallback(monkeypatch, answers)
    report = await _run(window_chars=len(BODY) + 1)
    assert report.error == "" and cli.exit_code(report) == code


def test_every_printed_action_has_its_line() -> None:
    from typing import get_args

    assert set(get_args(de.Action)) - {"row_changed"} == set(cli._ACTIONS)


# ------------------------------------------------------------------ restated reasons, whitespace, captions


@pytest.mark.parametrize(
    ("outcome", "reasoning", "restated"),
    [
        ("spill into sorted runs", "We spill into sorted runs.", True),
        ("spill into sorted runs", "We decided on spilling into sorted runs.", True),
        ("Sorted runs for the spill", "We use sorted runs, instead.", True),
        ("store offsets", "Offsets are stored.", True),
        ("run the merge per part", "Running the merge per part.", True),
        ("process each part", "Each part processes.", True),
        ("store strings as offsets", "Offsets are stored as a string.", True),
        ("spill into sorted runs", "Because we spill into sorted runs.", True),
        ("spill into sorted runs", "Since we spill into sorted runs.", True),
        ("spill into sorted runs", "We picked sorted runs for spilling.", True),
        ("spill into sorted runs", "We opted for sorted runs.", True),
        ("spill into sorted runs", "The decision is to spill into sorted runs.", True),
        ("sorted runs for the spill", "Sorted runs were chosen for the spill.", True),
        ("spill into sorted runs", "We are choosing sorted runs for spilling.", True),
        ("spill into sorted runs", "A plain set on disk costs random reads.", False),
        ("spill into sorted runs", "Sorted runs avoid random reads on disk.", False),
        # An outcome that carries its reason leaves the reasoning nothing to add: dropped,
        # which is why the prompt asks for the outcome without the reason.
        (
            "spill into sorted runs, as a plain set on disk costs random reads",
            "A plain set on disk costs random reads.",
            True,
        ),
    ],
)
def test_a_reasoning_with_no_word_beyond_its_outcome_restates_it(
    outcome: str, reasoning: str, restated: bool
) -> None:
    assert dx.restates_outcome(_cand(outcome=outcome, reasoning=reasoning)) is restated


@pytest.mark.asyncio
async def test_a_candidate_whose_reasoning_restates_the_outcome_is_dropped(monkeypatch) -> None:
    restating = _candidate(
        reasoning="We spill into sorted runs like ORDER BY.",
        outcome="spill into sorted runs like ORDER BY",
    )
    world = _World(monkeypatch, [restating])
    report = await _run()
    assert world.recorded == [] and report.records == []
    (d,) = report.dropped
    assert (d.section, d.reason, d.refused) == (SECTION, dx.RESTATED, False)


# DOC has this sentence on one line.
_WRAPPED_REASONING = "We spill into sorted\nruns like ORDER BY."


def test_a_span_differing_only_in_whitespace_is_the_documents() -> None:
    text = "We spill into sorted\nruns like ORDER BY.  A plain set on disk costs random reads."
    cand = _cand(reasoning="A plain set on disk  costs random reads.")
    assert dx.verbatim_failure(cand, [text]) == ""
    assert dx.find_span("sorted runs like", [text]) == "sorted\nruns like"
    assert dx.find_span("sorted runslike", [text]) == ""
    stored = dx.document_spans(cand, [text])
    # Stored on one line, the document's words and case.
    assert (
        stored.quote
        == "We spill into sorted runs like ORDER BY. A plain set on disk costs random reads."
    )
    assert stored.reasoning == "A plain set on disk costs random reads."
    assert stored.alternatives == [("a plain set on disk", "costs random reads")]


@pytest.mark.asyncio
async def test_a_span_with_other_line_breaks_is_recorded_on_one_line(
    monkeypatch,
) -> None:
    from server.knowledge.app import document_reads

    wrapped = BODY.replace("A plain set on disk ", "A plain set\non disk ")
    world = _World(monkeypatch, [_candidate(reasoning="A plain set on disk costs random reads.")])

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, wrapped, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    (inp,) = world.recorded
    assert inp.reasoning == "A plain set on disk costs random reads."
    assert inp.source_resource_id == f"knowledge:ns/doc.md#{SECTION}"
    assert report.dropped == []


@pytest.mark.asyncio
async def test_a_reasoning_the_document_rewrapped_is_not_retired(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    # DOC has the sentence on one line; the stored span broke it.
    world.record("decision:wrapped", {"reasoning": "A plain set on disk\ncosts random reads."})
    report = await _run()
    assert report.retired == [] and world.deletes == [] and report.flagged == []


def test_the_prompt_asks_for_the_reason_and_skips_recaps_and_superseded_text() -> None:
    prompt = dx.system_prompt(3)
    assert "Never the decision restated in other words" in prompt
    # A ban stated without a reason may be emitted, as a constraint resting on its own sentence.
    assert "except a ban or an invariant the text states without one" in prompt
    assert "is_constraint true and its own sentence as the reasoning" in prompt
    assert "a section that sums up or recaps other sections" in prompt
    assert "former, replaced or superseded" in prompt
    # The code decides what is image-only: a caption over a diagram may state a decision.
    assert "caption" not in prompt and "an image" not in prompt


@pytest.mark.parametrize(
    ("body", "image_only"),
    [
        ("![diagram](img.png)\n\nFigure 2: the spill path, from the sort to the merge.", True),
        ("[image]\nThe write path", True),
        ("![diagram](img.png)\n\n" + BODY, False),
        # A short line with no picture is text, a decision perhaps.
        ("Spill into sorted runs: random reads cost too much.", False),
    ],
)
def test_an_image_with_only_a_caption_is_image_only(body: str, image_only: bool) -> None:
    assert dx.image_only(body) is image_only


@pytest.mark.asyncio
async def test_a_captioned_diagram_is_not_sent_and_is_reported_image_only(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    caption = "![diagram](flow.png)\n\nFigure 1: how a spilled block flows back."
    doc = DOC.replace("![diagram](img.png)\n", caption + "\n")

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc)

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk(uri, 1, caption, ("Spilling", "Architecture")),
        ]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "Figure 1" not in prompt[1]
    assert report.image_only == ["Spilling > Architecture"]


@pytest.mark.asyncio
async def test_the_fake_model_rests_each_decision_on_a_second_sentence() -> None:
    from server.llm import customer
    from server.llm.fake import FakeLLMClient

    w = Window(
        [
            CorpusChunk("d", 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk("d", 1, "One sentence only, no reason given.", ("Spilling", "Short")),
        ]
    )
    with customer.for_org("o"):
        res = await FakeLLMClient().complete(dx.system_prompt(3), dx.user_prompt(w))
    (cand,) = dx.parse_candidates(res.text) or []
    assert cand.outcome == "We spill into sorted runs like ORDER BY."
    assert cand.reasoning == "A plain set on disk costs random reads."
    assert dx.verbatim_failure(cand, [BODY]) == "" and not dx.restates_outcome(cand)


@pytest.mark.asyncio
async def test_a_long_sections_last_chunk_with_an_image_and_a_short_tail_is_sent(
    monkeypatch,
) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    tail = "![a long diagram name](figures/spill-merge-flow.png)\n\nThe merge reads each run once."
    doc = DOC.replace(BODY, f"{BODY}\n\n{tail}")

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc)

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk(uri, 1, tail, ("Spilling", "Final DISTINCT")),
        ]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "The merge reads each run once." in prompt[1]
    assert report.image_only == ["Spilling > Architecture"]


@pytest.mark.asyncio
async def test_a_rewrapped_reasoning_under_a_reworded_key_replaces_the_old_row(
    monkeypatch,
) -> None:
    first = _candidate()
    world = _World(monkeypatch, [])
    # Recorded when the document broke the line; DOC now has it on one line.
    wrapped = {**_same_fields(first), "reasoning": _WRAPPED_REASONING}
    world.record(_key(first), wrapped, files=[HEADER])
    second = _candidate(scenario="spilling the final DISTINCT")
    world.reply = [second]
    report = await _run()
    assert [r.action for r in report.records] == ["written"]
    assert report.replaced == [f"{_key(first)} -> {_key(second)}"]
    assert [d.memory_key for d in world.deletes] == [_key(first)]


@pytest.mark.asyncio
async def test_a_pinned_row_whose_document_only_rewrapped_is_not_flagged(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    stored = {**_same_fields(c), "reasoning": _WRAPPED_REASONING}
    world.record(_key(c), stored, pinned=True)
    report = await _run()
    assert [r.action for r in report.records] == ["unchanged"]
    assert world.updates == [] and world.recorded == [] and report.flagged == []


@pytest.mark.asyncio
async def test_an_unpinned_own_row_whose_reasoning_only_rewrapped_is_unchanged(
    monkeypatch,
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": _WRAPPED_REASONING}, files=[HEADER])
    report = await _run()
    assert [r.action for r in report.records] == ["unchanged"]
    assert world.recorded == [] and world.updates == [] and world.deletes == []


@pytest.mark.asyncio
async def test_a_row_with_an_outcome_whose_reasoning_only_rewrapped_is_not_tagged(
    monkeypatch,
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    fields = {**_same_fields(c), "reasoning": _WRAPPED_REASONING, "realized_outcome": "no gaps"}
    # Its files are stale, so the row is not unchanged; the decision itself is.
    world.record(_key(c), fields, files=[])
    report = await _run()
    assert [r.action for r in report.records] == ["outcome_known"]
    assert world.recorded == [] and world.updates == [] and report.flagged == []


@pytest.mark.asyncio
async def test_one_reasoning_wrapped_apart_in_two_windows_is_written_once(monkeypatch) -> None:
    from server.knowledge.app import document_reads
    from server.llm import client as llm_client

    first = _candidate()
    second = _candidate(scenario="spilling the final DISTINCT")
    world = _World(monkeypatch, [])
    replies = [[first], [second]]

    class _TwoWindows:
        async def complete(self, *a: Any, **k: Any) -> Any:
            return SimpleNamespace(text=json.dumps({"decisions": replies.pop(0)}), stop_reason="")

    async def chunks(org, ns, uri):
        wrapped = BODY.replace("sorted runs", "sorted\nruns")
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk(uri, 1, wrapped, ("Spilling", "Final DISTINCT")),
        ]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _TwoWindows())
    report = await _run(window_chars=len(BODY) + 1)
    assert report.windows == 2
    assert [d.reason for d in report.dropped] == ["same decision as an earlier candidate"]
    assert len(world.recorded) == 1


def test_a_parent_section_over_a_captioned_picture_is_image_only() -> None:
    doc = (
        "# Spilling\n\n![flow](flow.png)\n\nFigure 1: the spill path.\n\n"
        "## Final DISTINCT\n\n" + BODY + "\n\n"
        "# Merging\n\n## Runs\n\n" + BODY + "\n"
    )
    # The parent with a picture is reported; a heading alone over subsections is not.
    assert dx.image_only_sections(doc) == ["Spilling"]
    # So is a parent over a picture alone, with no words at all.
    bare = f"# Spilling\n\n![flow](flow.png)\n\n## Final DISTINCT\n\n{BODY}\n"
    assert dx.image_only_sections(bare) == ["Spilling"]


@pytest.mark.asyncio
async def test_a_parent_sections_captioned_picture_is_not_sent(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    caption = "![flow](flow.png)\n\nFigure 1: the spill path."
    doc = f"# Spilling\n\n{caption}\n\n## Final DISTINCT\n\n{BODY}\n"

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc)

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, caption, ("Spilling",)),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Final DISTINCT")),
        ]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "Figure 1" not in prompt[1] and "[section: Spilling > Final DISTINCT]" in prompt[1]
    assert report.image_only == ["Spilling"]


@pytest.mark.parametrize(
    ("body", "image"),
    [
        ("[image]", True),
        ("[Image 3]", True),
        ("[image: the spill path]", True),
        ("[image1]", True),
        ("[image_1]", True),
        ("[images]", True),
        ("[pictures]", True),
        ("[image tiles](tiles.md)", False),
        ("[image][1]", False),
        ("[image]: x.png", False),
        ("[imagine the merge]", False),
        ("[the diagram][image]", False),
        ("[image tiles]", False),
        ("[images and charts are in the appendix]", False),
    ],
)
def test_an_image_placeholder_stands_alone(body: str, image: bool) -> None:
    # A caption-length text beside it decides: with a placeholder the body is image-only.
    assert dx.image_only(f"{body}\n\nThe spill path.") is image


def test_a_preamble_or_a_headingless_document_of_a_picture_is_image_only() -> None:
    badge = "![build](badge.svg)\n\nBuild status for the spill code."
    assert dx.image_only_sections(f"{badge}\n\n# Spilling\n\n{BODY}\n") == [""]
    assert dx.image_only_sections(badge) == [""]
    assert dx.image_only_sections(f"Intro line.\n\n# Spilling\n\n{BODY}\n") == []
    assert dx.image_only_sections(BODY) == []


@pytest.mark.asyncio
async def test_a_badge_and_caption_preamble_is_not_sent_and_is_reported(
    monkeypatch, capsys
) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    badge = "![build](badge.svg)\n\nBuild status for the spill code."
    doc = f"{badge}\n\n{DOC}"

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc)

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, badge, ()),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Final DISTINCT")),
        ]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "Build status" not in prompt[1]
    assert report.image_only == ["", "Spilling > Architecture"]
    cli.print_report(report)
    assert "image-only, rationale not extracted: (untitled)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_chunks_stored_with_no_section_path_are_judged_on_their_own_text(
    monkeypatch,
) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    badge = "![build](badge.svg)\n\nBuild status for the spill code."
    doc = f"{badge}\n\n{DOC}"

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc)

    async def chunks(org, ns, uri):
        # Chunked when no heading was seen: every chunk has the preamble's empty path.
        return [CorpusChunk(uri, 0, badge, ()), CorpusChunk(uri, 1, BODY, ())]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "Build status" not in prompt[1] and BODY in prompt[1]
    assert report.image_only == ["", "Spilling > Architecture"]


_CI_URL = "https://ci.example.com/org/spill/actions/workflows/ci.yml"
_LINKED_BADGE = f"[![build](badge.svg)]({_CI_URL})"


@pytest.mark.parametrize(
    ("body", "image_only"),
    [
        ("![diagram][fig1]\n\nFigure 1: the spill path.", True),
        # A shortcut reference is not read as an image: it looks like a macro.
        ("![spill flow]\n\nFigure 1: the spill path.", False),
        ("Use vec![0; n]: a fixed array overflows the stack.", False),
        ("    let buf = vec![0u8; 4096];\n\nAllocate the buffer once per block.", False),
        # Its link target, left over, would read as more than a caption.
        (f"{_LINKED_BADGE}\n\nBuild status for the spill code.", True),
        (
            f'<a href="{_CI_URL}"><img src="badge.svg"></a>\n\nBuild status for the spill code.',
            True,
        ),
        (f"[![b][badge]]({_CI_URL})\n\nBuild status for the spill code.", True),
        ('<img src="flow.png" alt="flow">\n\nFigure 1: the spill path.', True),
        # A comparison or a generic is no tag: the decision beside the diagram is text.
        (
            "![flow](flow.png)\n\nSpill once the block count is < 10k and memory use is > 80% "
            "of the limit, so the merge stays sequential.",
            False,
        ),
        # A link is no image: a short sentence with one is text.
        ("See [the merge notes](https://example.com/notes) for why runs are sorted.", False),
    ],
)
def test_a_caption_is_counted_without_its_markup(body: str, image_only: bool) -> None:
    assert dx.image_only(body) is image_only


def test_only_tag_shaped_markup_is_removed_from_a_caption() -> None:
    text = '<p align="center">Returns Option<A>, Map<a, b> when x < 10k and y > 80%.</p>'
    assert dx.caption_text(text) == "Returns Option<A>, Map<a, b> when x < 10k and y > 80%."


@pytest.mark.asyncio
async def test_a_linked_badge_and_caption_preamble_is_not_sent(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    badge = f"{_LINKED_BADGE}\n\nBuild status for the spill code."
    doc = f"{badge}\n\n{DOC}"

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc)

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, badge, ()),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Final DISTINCT")),
        ]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "Build status" not in prompt[1]
    assert report.image_only == ["", "Spilling > Architecture"]


@pytest.mark.asyncio
async def test_a_headingless_document_of_a_captioned_picture_is_not_sent(
    monkeypatch, capsys
) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    picture = "![flow](flow.png)\n\nFigure 1: the spill path."

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=picture)

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, picture, ())]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    assert world.prompts == [] and report.windows == 0
    assert report.image_only == [""]
    cli.print_report(report)
    assert "image-only, rationale not extracted: (untitled)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_rejected_reason_the_document_rewrapped_is_no_change(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    c = _candidate()
    world = _World(monkeypatch, [c])
    # Stored on one line, as decision.record keeps an alternative.
    world.record(_key(c), _same_fields(c), files=[HEADER])

    async def chunks(org, ns, uri):
        wrapped = BODY.replace("costs random reads", "costs random\nreads")
        return [CorpusChunk(uri, 0, wrapped, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    assert [r.action for r in report.records] == ["unchanged"]
    assert world.recorded == [] and world.updates == [] and world.deletes == []


def test_a_preamble_of_linked_badges_and_a_caption_is_image_only() -> None:
    badges = " ".join(f"[![b{i}](b{i}.svg)]({_CI_URL}/{i})" for i in range(8))
    caption = "Build, lint, docs and release status for the spill."
    assert len(caption) == 51
    assert dx.image_only_sections(f"{badges}\n\n{caption}\n\n# Spilling\n\n{BODY}\n") == [""]


@pytest.mark.asyncio
async def test_a_dropped_candidate_with_a_rewrapped_quote_names_the_quotes_section(
    monkeypatch,
) -> None:
    from server.knowledge.app import document_reads

    # The quote is the second section's, re-wrapped; the reasoning is in no chunk.
    cand = _candidate(
        quote="A plain set on disk costs random reads.", reasoning="Sorted runs are faster."
    )
    world = _World(monkeypatch, [cand])

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, "We spill into sorted runs like ORDER BY.", ("Spilling", "One")),
            CorpusChunk(uri, 1, "A plain set on disk\ncosts random reads.", ("Spilling", "Two")),
        ]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    assert world.recorded == []
    # The window is read once more for the reasoning, named by the failed decision's
    # section; the model gives the same reply.
    assert [d.section for d in report.retried] == ["Spilling > Two"]
    assert [(d.section, d.reason) for d in report.dropped] == [
        ("Spilling > Two", "reasoning is not in the document text")
    ] * 2


@pytest.mark.parametrize(
    ("body", "image_only"),
    [
        ("Embed `![d](f.png)` in the page.", False),
        ("```md\n![d](f.png)\n```\n\nEmbed it so.", False),
        ("Embed it as `<img src=x>` in the page.", False),
        ("![d](f.png)\n\nFigure 1: the spill path, see `spill()`.", True),
    ],
)
def test_image_markup_inside_code_is_text(body: str, image_only: bool) -> None:
    assert dx.image_only(body) is image_only


def test_image_markup_inside_code_makes_no_section_image_only() -> None:
    fence = "```\n![]()\n```"
    doc = f"`![1]`\n\n# Spilling\n\n{fence}\n\n## Final DISTINCT\n\n{BODY}\n"
    assert dx.image_only_sections(doc) == []


@pytest.mark.asyncio
async def test_a_captioned_picture_in_a_preamble_with_text_is_sent(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    picture = "![flow](flow.png)\n\nFigure 1: the spill path."
    # The preamble as a whole is a picture, its caption and a paragraph: not image-only.
    doc = f"{picture}\n\n{BODY}\n\n{DOC}"

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc)

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, picture, ()), CorpusChunk(uri, 1, BODY, ())]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    assert dx.image_only(picture) and "" not in report.image_only
    prompt, retry = world.prompts  # an empty reply is read once more, with the same prompt
    assert retry == prompt
    assert "Figure 1: the spill path." in prompt[1] and BODY in prompt[1]


# ------------------------------------------------------------------ second reads


def _chunks(monkeypatch, *chunks: tuple[str, tuple[str, ...]]) -> None:
    from server.knowledge.app import document_reads

    async def read(org, ns, uri):
        return [CorpusChunk(uri, i, text, path) for i, (text, path) in enumerate(chunks)]

    monkeypatch.setattr(document_reads, "document_chunks", read)


@pytest.mark.asyncio
async def test_an_empty_reply_is_read_once_more_and_the_second_reply_is_used(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.replies = [[]]
    report = await _run()
    assert len(world.prompts) == 2 and world.prompts[0] == world.prompts[1]
    assert [r.action for r in report.records] == ["written"]
    assert [(d.section, d.reason) for d in report.retried] == [(SECTION, de._EMPTY_REPLY)]
    assert cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_an_empty_reply_over_a_caption_of_text_is_not_read_again(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    _chunks(monkeypatch, ("New planner pass", ("Planner",)))
    report = await _run()
    assert len(world.prompts) == 1 and report.retried == []
    assert cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_a_failed_second_read_loses_the_window(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.replies = [[], httpx.ConnectError("down")]
    report = await _run()
    assert len(world.prompts) == 2 and world.recorded == []
    assert report.error == (
        "no window was read: every model call failed, was answered by the fake model, came "
        "back unreadable, or answered nothing and failed when read again"
    )
    assert cli.exit_code(report) == 1


_STITCHED_REASONING = "A plain set on disk ORDER BY."


@pytest.mark.asyncio
async def test_a_stitched_reasoning_is_asked_for_again_as_one_span(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.replies = [[_candidate(reasoning=_STITCHED_REASONING)]]
    report = await _run()
    assert len(world.prompts) == 2
    note = world.prompts[1][1]
    assert (
        note.startswith("An earlier reply gave these decisions")
        and json.dumps(_STITCHED_REASONING) in note
    )
    assert note.endswith(world.prompts[0][1])
    assert [r.action for r in report.records] == ["written"]
    assert [d.reason for d in report.retried] == [de._STITCHED]
    assert [d.reason for d in report.dropped] == ["reasoning is not in the document text"]


@pytest.mark.asyncio
async def test_a_stitched_second_read_the_fallback_fake_answered_loses_nothing(
    monkeypatch,
) -> None:
    from server.llm import client as llm_client
    from server.settings import settings

    _World(monkeypatch, [])  # its model is replaced below
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(llm_client, "provider_key_missing", lambda provider: False)
    first = [_candidate(), _candidate(scenario="the merge", reasoning=_STITCHED_REASONING)]
    providers = ["openai", "fake"]

    class _LLM:
        async def complete(self, *a: Any, **k: Any) -> Any:
            text = json.dumps({"decisions": first})
            return SimpleNamespace(text=text, stop_reason="", provider=providers.pop(0))

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _LLM())
    report = await _run()
    assert providers == [] and [r.action for r in report.records] == ["written"]
    assert [d.refused for d in report.dropped] == [False]
    assert [r.reason for r in report.retried] == [
        de._STITCHED,
        f"the second read was lost: {de._FAKE_ANSWER}",
    ]
    assert cli.exit_code(report) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repeat",
    [
        {"scenario": "spilling the final DISTINCT"},
        # Its reasoning copied in another case, its outcome reworded: still the kept one.
        {
            "scenario": "spilling the final DISTINCT",
            "outcome": "spill to sorted runs",
            "reasoning": "we spill into sorted runs like ORDER BY.",
        },
    ],
)
async def test_a_second_read_repeating_a_kept_decision_writes_it_once(monkeypatch, repeat) -> None:
    stitched = _candidate(
        scenario="the merge", outcome="merge each run once", reasoning=_STITCHED_REASONING
    )
    repaired = {**stitched, "reasoning": "A plain set on disk costs random reads."}
    world = _World(monkeypatch, [])
    world.replies = [
        [_candidate(), stitched],
        # The kept decision again under a reworded scenario, then the repair.
        [_candidate(**repeat), repaired],
    ]
    report = await _run()
    assert [inp.scenario for inp in world.recorded] == [
        _candidate()["scenario"],
        "the merge",
    ]
    assert [d.reason for d in report.dropped] == [
        "reasoning is not in the document text",
        de._REPEATED,
    ]


_ONE = "We spill into sorted runs like ORDER BY. A plain set on disk costs random reads."
_TWO = "The merge reads each run once. One pass keeps reads sequential."
_MERGE = _candidate(
    scenario="the merge",
    outcome="merge each run once",
    quote="The merge reads each run once.",
    reasoning="One pass keeps reads sequential.",
    alternatives=[],
    mentions=[],
)
_SPILL = _candidate(
    quote="We spill into sorted runs like ORDER BY.",
    reasoning=_STITCHED_REASONING,
    alternatives=[],
    mentions=[],
)


async def _second_read(
    monkeypatch, again: list[dict[str, Any]], spill: dict[str, Any] = _SPILL
) -> tuple[Any, Any]:
    """A window of two sections: the first reply keeps the merge and joins the spill's
    reason; `again` is the second reply."""
    world = _World(monkeypatch, [])
    _chunks(monkeypatch, (_ONE, ("Spilling", "One")), (_TWO, ("Spilling", "Two")))
    world.replies = [[spill, _MERGE], again]
    return world, await _run()


@pytest.mark.asyncio
async def test_a_lost_stitched_read_is_named_by_the_failed_section(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    _chunks(monkeypatch, (_TWO, ("Spilling", "Two")), (_ONE, ("Spilling", "One")))
    world.replies = [[_MERGE, _SPILL], httpx.ConnectError("down")]
    report = await _run()
    assert [inp.scenario for inp in world.recorded] == ["the merge"]
    assert [(r.section, r.reason) for r in report.retried] == [
        ("Spilling > One", de._STITCHED),
        ("Spilling > One", "the second read was lost: the model call failed: ConnectError"),
    ]
    assert cli.exit_code(report) == 0


@pytest.mark.asyncio
async def test_a_repeat_in_a_later_section_is_reported_there(monkeypatch) -> None:
    world, report = await _second_read(monkeypatch, [{**_MERGE, "scenario": "merging runs"}])
    assert [inp.scenario for inp in world.recorded] == ["the merge"]
    assert [(d.section, d.reason) for d in report.dropped] == [
        ("Spilling > One", "reasoning is not in the document text"),
        ("Spilling > Two", de._REPEATED),
    ]


@pytest.mark.asyncio
async def test_a_repair_with_a_reworded_outcome_and_a_new_reasoning_is_recorded(
    monkeypatch,
) -> None:
    repair = {
        **_SPILL,
        "outcome": "spill to sorted runs on disk",
        "reasoning": "A plain set on disk costs random reads.",
    }
    world, report = await _second_read(monkeypatch, [repair])
    assert [inp.outcome for inp in world.recorded] == [
        "merge each run once",
        "spill to sorted runs on disk",
    ]
    assert [d.reason for d in report.dropped] == ["reasoning is not in the document text"]


@pytest.mark.asyncio
async def test_a_repair_matches_its_failed_outcome_whitespace_aside(monkeypatch) -> None:
    # The repair rests on the kept merge decision's span: only its outcome marks it a repair.
    spill = {**_SPILL, "outcome": "spill into\nsorted runs"}
    repair = {**_SPILL, "outcome": "spill into sorted runs", "reasoning": _MERGE["reasoning"]}
    world, report = await _second_read(monkeypatch, [repair], spill=spill)
    assert [inp.outcome for inp in world.recorded] == [
        "merge each run once",
        "spill into sorted runs",
    ]
    assert de._REPEATED not in [d.reason for d in report.dropped]


@pytest.mark.parametrize("lost", ["unreadable", "fake"])
@pytest.mark.asyncio
async def test_an_empty_window_whose_second_read_is_lost_is_refused(monkeypatch, lost) -> None:
    from server.llm import client as llm_client
    from server.settings import settings

    world = _World(monkeypatch, [])
    await _two_windows(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(llm_client, "provider_key_missing", lambda provider: False)
    lost_reply = ("not json", "openai") if lost == "unreadable" else ("{}", "fake")
    replies = [(json.dumps({"decisions": []}), "openai"), lost_reply]
    replies.append((json.dumps({"decisions": [_candidate()]}), "openai"))

    class _LLM:
        async def complete(self, *a: Any, **k: Any) -> Any:
            text, provider = replies.pop(0)
            return SimpleNamespace(text=text, stop_reason="", provider=provider)

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _LLM())
    report = await _run(window_chars=len(BODY) + 1)
    # The other window is still read and recorded.
    assert replies == [] and len(world.recorded) == 1
    assert [r.action for r in report.records] == ["written"]
    (d,) = report.dropped
    assert d.section == "Spilling > Other" and d.refused
    assert [r.reason for r in report.retried] == [de._EMPTY_REPLY]
    assert cli.exit_code(report) == 2


@pytest.mark.asyncio
async def test_a_stitched_rejected_reason_is_asked_for_again_and_repaired(monkeypatch) -> None:
    stitched = "a plain set on disk random reads"
    joined = [{"option": "a plain set on disk", "reason": stitched}]
    world = _World(monkeypatch, [_candidate()])
    world.replies = [[_candidate(alternatives=joined)]]
    report = await _run()
    assert len(world.prompts) == 2 and json.dumps(stitched) in world.prompts[1][1]
    (inp,) = world.recorded
    assert inp.alternatives == ["a plain set on disk — rejected: costs random reads"]
    assert [r.reason for r in report.retried] == [de._STITCHED]


@pytest.mark.asyncio
async def test_a_window_gets_at_most_one_extra_call(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    stitched = [{"option": "a plain set on disk", "reason": "costs on disk random reads"}]
    world.replies = [[], [_candidate(alternatives=stitched)]]
    report = await _run()
    assert len(world.prompts) == 2 and world.recorded == []
    assert [d.reason for d in report.retried] == [de._EMPTY_REPLY]
    assert [d.reason for d in report.dropped] == [
        "the reason given for rejecting 'a plain set on disk' is not in the document text"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "over",
    [
        {"quote": "A quote the document never wrote."},
        # A short span stays short however often the window is read.
        {"quote": "We spill", "reasoning": _STITCHED_REASONING},
        {"reasoning": "Runs win."},
    ],
)
async def test_a_short_or_unquoted_failure_is_not_asked_for_again(monkeypatch, over) -> None:
    world = _World(monkeypatch, [_candidate(**over)])
    report = await _run()
    assert len(world.prompts) == 1 and report.retried == [] and world.recorded == []


@pytest.mark.parametrize(
    "lost",
    [
        httpx.ConnectError("down"),
        # An unreadable reply: a string where the candidates go.
        "not json",
    ],
)
@pytest.mark.asyncio
async def test_a_lost_second_read_for_a_stitched_reason_keeps_the_first_reply(
    monkeypatch, lost
) -> None:
    from server.llm import client as llm_client

    _World(monkeypatch, [])  # its model is replaced below, to answer the second read
    first = [_candidate(), _candidate(scenario="the merge", reasoning=_STITCHED_REASONING)]
    calls: list[str] = []

    class _LLM:
        async def complete(self, system, user, max_tokens=0, response_format=None):
            calls.append(user)
            if len(calls) == 1:
                return SimpleNamespace(text=json.dumps({"decisions": first}), stop_reason="")
            if isinstance(lost, Exception):
                raise lost
            return SimpleNamespace(text=lost, stop_reason="")

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _LLM())
    report = await _run()
    assert len(calls) == 2
    assert [r.action for r in report.records] == ["written"]
    assert [d.refused for d in report.dropped] == [False]
    assert report.retried[0].reason == de._STITCHED
    assert report.retried[1].reason.startswith("the second read was lost: ")
    assert report.error == "" and cli.exit_code(report) == 0


# ------------------------------------------------------------------ bans with no reason

_BAN = "Client-side retries only; no server retry setting."


def _ban(**over: Any) -> dict[str, Any]:
    ban: dict[str, Any] = {
        "category": "design:retries",
        "scenario": "where requests are retried",
        "outcome": "client-side retries only, no server retry setting",
        "reasoning": _BAN,
        "quote": _BAN,
        "alternatives": [],
        "is_constraint": True,
        "mentions": [],
    }
    return _candidate(**{**ban, **over})


@pytest.mark.parametrize(
    ("reasoning", "is_constraint", "ban"),
    [
        (_BAN, True, True),
        ("Reads must never block on a merge.", True, True),
        ("At most one spill file is open per thread.", True, True),
        ("The proxy retries each request.", True, False),
        # "nothing" and "cannot" hold no word-bounded marker.
        ("The planner cannot see nothing here.", True, False),
        (_BAN, False, False),
    ],
)
def test_a_ban_is_a_constraint_whose_reasoning_says_so(reasoning, is_constraint, ban) -> None:
    cand = _cand(reasoning=reasoning, is_constraint=is_constraint)
    assert dx.states_a_ban(cand) is ban


@pytest.mark.asyncio
async def test_a_ban_stated_with_no_reason_is_kept_as_one(monkeypatch) -> None:
    from server.memory.domain.decision_statement import memory_value

    world = _World(monkeypatch, [_ban()])
    _chunks(monkeypatch, (f"{_BAN} The proxy adds jitter to each attempt.", ("Retries",)))
    report = await _run()
    assert [r.action for r in report.records] == ["written"] and report.dropped == []
    (inp,) = world.recorded
    assert inp.outcome == inp.reasoning == _BAN and de.CONSTRAINT_TAG in inp.tags
    statement = memory_value({"outcome": inp.outcome, "reasoning": inp.reasoning})
    assert statement == f"Decided {_BAN}"


@pytest.mark.asyncio
async def test_a_ban_whose_outcome_is_shorter_than_its_sentence_is_kept_with_no_reason(
    monkeypatch,
) -> None:
    # The outcome lacks "client-side retries", so the reasoning does not restate it; the
    # reasoning is the whole quote, so the text gives no reason.
    world = _World(monkeypatch, [_ban(outcome="no server retry setting")])
    _chunks(monkeypatch, (f"{_BAN} The proxy adds jitter to each attempt.", ("Retries",)))
    report = await _run()
    assert report.dropped == []
    (inp,) = world.recorded
    assert inp.outcome == inp.reasoning == _BAN


@pytest.mark.asyncio
async def test_a_ban_copied_in_another_case_is_still_its_whole_quote(monkeypatch) -> None:
    world = _World(monkeypatch, [_ban(outcome="no server retry setting", reasoning=_BAN.lower())])
    _chunks(monkeypatch, (f"{_BAN} The proxy adds jitter to each attempt.", ("Retries",)))
    await _run()
    (inp,) = world.recorded
    assert inp.outcome == inp.reasoning == _BAN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "over",
    [
        # The reasoning is the whole quote.
        {"outcome": "no server retry setting"},
        # The reasoning is a fragment of the ban: the quote is stored instead, judged on
        # the model's spans, so the label inside the document's quote changes nothing.
        {"reasoning": "no server retry setting."},
    ],
)
async def test_a_reasonless_ban_split_by_a_label_is_stored_without_it(monkeypatch, over) -> None:
    text = "Client-side retries only;\nProxy\nno server retry setting."
    world = _World(monkeypatch, [_ban(**over)])
    _chunks(monkeypatch, (text, ("Retries",)))
    await _run()
    (inp,) = world.recorded
    assert inp.outcome == inp.reasoning == _BAN


@pytest.mark.asyncio
async def test_a_ban_too_long_for_an_outcome_is_judged_as_any_candidate(monkeypatch) -> None:
    long_ban = "Every client must retry " + "with its own jitter and backoff, " * 32 + "only once."
    assert len(long_ban) > dx.OUTCOME_MAX_CHARS
    world = _World(
        monkeypatch, [_ban(outcome="retry with jitter", reasoning=long_ban, quote=long_ban)]
    )
    _chunks(monkeypatch, (long_ban, ("Retries",)))
    report = await _run()
    # Not a no-reason ban: it does not restate the short outcome, so it is recorded as is.
    (inp,) = world.recorded
    assert inp.outcome == "retry with jitter" and report.dropped == []


def test_the_outcome_limit_is_decision_records() -> None:
    from server.memory.api.models_decision import DecisionRecordInput

    (limit,) = [
        m.max_length
        for m in DecisionRecordInput.model_fields["outcome"].metadata
        if hasattr(m, "max_length")
    ]
    assert limit == dx.OUTCOME_MAX_CHARS


@pytest.mark.asyncio
async def test_a_ban_fragment_as_reasoning_gives_way_to_the_ban_quote(monkeypatch) -> None:
    world = _World(monkeypatch, [_ban(reasoning="no server retry setting")])
    _chunks(monkeypatch, (f"{_BAN} The proxy adds jitter to each attempt.", ("Retries",)))
    await _run()
    (inp,) = world.recorded
    assert inp.outcome == inp.reasoning == _BAN


def test_the_report_prints_each_second_read(capsys) -> None:
    report = de.ExtractReport("doc.md", dry_run=True)
    report.retried = [de.Retried(SECTION, de._EMPTY_REPLY), de.Retried("", de._STITCHED)]
    cli.print_report(report)
    out = capsys.readouterr().out
    assert f"- read again [{SECTION}] {de._EMPTY_REPLY}\n" in out
    assert f"- read again [(untitled)] {de._STITCHED}\n" in out


@pytest.mark.parametrize("emitted", [True, False])
@pytest.mark.asyncio
async def test_a_reasonless_ban_reads_unchanged_on_a_rerun(monkeypatch, emitted) -> None:
    ban = _ban()
    world = _World(monkeypatch, [ban] if emitted else [])
    _chunks(monkeypatch, (f"{_BAN} The proxy adds jitter to each attempt.", ("Retries",)))
    # What the first run recorded: the ban sentence as both outcome and reasoning.
    fields = {**_same_fields(ban), "outcome": _BAN}
    world.record(
        _key(ban),
        fields,
        tags=["decision", de.CONSTRAINT_TAG],
        source_resource_id="knowledge:ns/doc.md#Retries",
    )
    report = await _run()
    assert world.recorded == [] and world.deletes == [] and world.updates == []
    assert [r.action for r in report.records] == (["unchanged"] if emitted else [])
    assert report.retired == [] and report.flagged == [] and report.replaced == []


@pytest.mark.parametrize(
    "over",
    [
        {"is_constraint": False},
        # The quote holds the ban word, the stored reasoning does not.
        {"reasoning": "Client-side retries"},
    ],
)
@pytest.mark.asyncio
async def test_a_restatement_that_is_no_ban_is_still_dropped(monkeypatch, over) -> None:
    world = _World(monkeypatch, [_ban(**over)])
    _chunks(monkeypatch, (f"{_BAN} Client-side retries.", ("Retries",)))
    report = await _run()
    assert world.recorded == []
    assert [d.reason for d in report.dropped] == [dx.RESTATED]


# ------------------------------------------------------------------ recaps


@pytest.mark.parametrize(
    ("section", "recap"),
    [
        ("Design > Recap", True),
        ("Background > Final DISTINCT", True),
        ("Search > Improving recall in hybrid search", False),
        ("SUMMARY", True),
        ("Spilling > Final DISTINCT", False),
        ("Recalling spilled blocks", False),
        ("Current design", False),
    ],
)
def test_a_recap_heading_at_any_level_marks_the_section(section, recap) -> None:
    assert dx.in_recap(section) is recap


@pytest.mark.asyncio
async def test_a_recap_quote_is_dropped_and_its_text_still_gives_a_reason(monkeypatch) -> None:
    recap = "We spill into sorted runs like ORDER BY."
    reason = "A plain set on disk costs random reads."
    world = _World(
        monkeypatch,
        [
            _candidate(quote=recap, alternatives=[], reasoning=reason),
            _candidate(
                scenario="the merge",
                quote="One merge pass reads each run.",
                reasoning=reason,
                alternatives=[],
            ),
        ],
    )
    _chunks(
        monkeypatch,
        (f"{recap} {reason}", ("Spilling", "Recap")),
        ("One merge pass reads each run.", ("Spilling", "Merge")),
    )
    report = await _run()
    assert [d.reason for d in report.dropped] == [dx.RECAP]
    (inp,) = world.recorded
    assert inp.reasoning == reason and inp.source_resource_id.endswith("Spilling > Merge")
    assert "[section: Spilling > Recap]" in world.prompts[0][1]


@pytest.mark.asyncio
async def test_a_recap_candidate_with_a_missing_reasoning_is_not_read_again(monkeypatch) -> None:
    recap = "We spill into sorted runs like ORDER BY."
    world = _World(monkeypatch, [_candidate(quote=recap, reasoning=_STITCHED_REASONING)])
    _chunks(monkeypatch, (f"{recap} A plain set on disk costs random reads.", ("Recap",)))
    report = await _run()
    assert len(world.prompts) == 1 and report.retried == []
    assert [d.reason for d in report.dropped] == ["reasoning is not in the document text"]


LONG = "A plain set on disk costs random reads, and sorted runs merge in one pass. " * 2
DECK = (
    "# Deck\n\n## Intro\n\n" + LONG + "\n\n"
    "## Background\n\nWhere we started\n\n"
    "## Old spill\n\n" + LONG + "\n\n"
    "## Old merge\n\n![merge](m.png)\nThe merge\n\n" + LONG + "\n\n"
    "## Design\n\n"
    "## New spill\n\n" + LONG + "\n"
)


def test_a_recap_divider_covers_the_sections_up_to_the_next_short_one() -> None:
    assert dx.recap_span_sections(DECK) == {"Deck > Old spill", "Deck > Old merge"}


@pytest.mark.parametrize(
    "content",
    [
        # A recap-named section with a body of its own is a recap by its own heading.
        "# Deck\n\n## Summary\n\n" + LONG + "\n\n## Spill\n\n" + LONG + "\n",
        # A summary line states the decisions the sections after it give.
        "# Design\n\n## Summary\n\nShard by tenant to bound joins.\n\n## Sharding\n\n"
        + LONG
        + "\n\n## Replication\n\n"
        + LONG
        + "\n",
        # A pointer elsewhere is no divider either.
        "# Design\n\n## Background\n\nSee [the RFC](rfc.md)\n\n## Sharding\n\n" + LONG + "\n",
        # A heading alone over its subsections: they carry its name in their breadcrumbs.
        "# Background\n\n## History\n\n" + LONG + "\n\n# Spill\n\n" + LONG + "\n",
    ],
)
def test_a_recap_section_with_a_sentence_a_link_or_subsections_opens_no_span(content) -> None:
    assert dx.recap_span_sections(content) == set()


def test_a_section_titled_as_one_in_a_span_but_outside_it_is_not_covered() -> None:
    content = DECK + "\n## Old spill\n\n" + LONG + "\n"
    assert dx.recap_span_sections(content) == {"Deck > Old merge"}


def test_a_recap_span_ends_at_a_heading_above_the_dividers_level() -> None:
    content = "# A\n\n## Recap\n\n## R\n\n" + LONG + "\n\n# B\n\n" + LONG + "\n"
    assert dx.recap_span_sections(content) == {"A > R"}


@pytest.mark.asyncio
async def test_a_quote_inside_a_recap_divider_span_is_dropped(monkeypatch) -> None:
    old = "A plain set on disk costs random reads, and sorted runs merge in one pass."
    world = _World(
        monkeypatch,
        [
            _candidate(quote=old, reasoning=old, outcome="sorted runs", alternatives=[]),
            _candidate(
                scenario="the new spill",
                quote=old,
                reasoning=old,
                outcome="sorted runs",
                alternatives=[],
            ),
        ],
    )
    world.content = DECK
    # The same text in a covered section and in one after the span.
    _chunks(monkeypatch, (LONG, ("Deck", "Old spill")))
    report = await _run()
    assert world.recorded == [] and [d.reason for d in report.dropped] == [dx.RECAP] * 2
    world.prompts.clear()
    _chunks(monkeypatch, (LONG, ("Deck", "New spill")))
    report = await _run()
    assert len(world.recorded) == 1 and report.dropped[0].reason != dx.RECAP


# ------------------------------------------------------------------ inverted alternatives


@pytest.mark.parametrize(
    ("quote", "option", "reason", "inverted"),
    [
        (
            "We shard by time range; sharding by tenant is deferred to v2.",
            "defer sharding by tenant",
            "x" * 12,
            True,
        ),
        (
            "Sharding by tenant is deferred to v2. One cluster serves each tenant today.",
            "Defer sharding by tenant",
            "One cluster serves each tenant today.",
            True,
        ),
        (
            "The largest job runs longest, so v1 cannot evict it. Eviction picks the oldest.",
            "evict the largest job",
            "The largest job runs longest",
            False,
        ),
        # The sentence giving the reason is the rejection itself.
        (BODY, "a plain set on disk", "costs random reads", False),
        ("Sharding by tenant is faster than a shared table.", "shard by tenant", "x" * 12, False),
        ("Only filler words here.", "the", "x" * 12, False),
        # A choice named against what it beat still rejects it.
        ("We chose sorted runs over a plain set on disk.", "a plain set on disk", "x" * 12, False),
        ("Sorted runs replace the hash set.", "the hash set", "x" * 12, False),
        ("We benchmarked sorted runs against a hash set.", "a hash set", "x" * 12, False),
        ("We prefer sorted runs to a plain set on disk.", "a plain set on disk", "x" * 12, False),
        ("Sorted runs are favored; a hash set reads at random.", "a hash set", "x" * 12, False),
        ("The design favours sorted runs to a hash set.", "a hash set", "x" * 12, False),
        ("Sorted runs outperform a hash set on disk.", "a hash set", "x" * 12, False),
        ("Sorted runs beat a hash set on disk.", "a hash set", "x" * 12, False),
        # "vs." ends no sentence.
        ("Sorted runs vs. a hash set on disk.", "a hash set", "x" * 12, False),
        ("The design rejects a hash set on disk.", "a hash set", "x" * 12, False),
        ("The merge avoids a hash set on disk.", "a hash set", "x" * 12, False),
        ("Sorted runs, compared with a hash set, read in order.", "a hash set", "x" * 12, False),
        # A hard-wrapped sentence keeps its negation on the next line.
        (
            "Sorting the runs in memory is\nnot possible: the runs exceed RAM.\n"
            "Spilled runs merge from disk.",
            "sort the runs in memory",
            "Spilled runs merge from disk.",
            False,
        ),
        # The reason found in another case is still the sentence that rejects it.
        (
            "We shard by time. Sharding by tenant splits every join.",
            "shard by tenant",
            "sharding by tenant splits every join.",
            False,
        ),
        # A list item is a sentence of its own.
        (
            "Plans:\n- sort the runs in memory\n- never spill twice",
            "sort the runs in memory",
            "never spill twice",
            True,
        ),
    ],
)
def test_an_option_the_quote_states_as_chosen_is_inverted(quote, option, reason, inverted) -> None:
    cand = _cand(quote=quote, alternatives=[{"option": option, "reason": reason}])
    assert dx.inverted_options(cand) == ([option] if inverted else [])


@pytest.mark.parametrize(
    ("layout", "reason"),
    [
        (
            "We looked at a plain set on disk. Each lookup costs a random read.",
            "Each lookup costs a random read.",
        ),
        (
            "Options:\n- a plain set on disk\n- each lookup costs a random read",
            "each lookup costs a random read",
        ),
        (
            "We benchmarked sorted runs against a plain set on disk. "
            "Each lookup costs a random read.",
            "Each lookup costs a random read.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_option_named_outside_the_quote_stays_rejected(
    monkeypatch, layout, reason
) -> None:
    quote = "We spill into sorted runs like ORDER BY."
    alternatives = [{"option": "a plain set on disk", "reason": reason}]
    cand = _candidate(quote=quote, reasoning=reason, alternatives=alternatives, mentions=[])
    world = _World(monkeypatch, [cand])
    _chunks(monkeypatch, (f"{quote}\n\n{layout}", ("Spilling",)))
    report = await _run()
    (inp,) = world.recorded
    assert inp.alternatives == [f"a plain set on disk — rejected: {reason}"]
    assert report.dropped == []


@pytest.mark.asyncio
async def test_an_inverted_alternative_is_left_out_and_the_decision_kept(monkeypatch) -> None:
    text = (
        "We shard by time range; sharding by tenant is deferred to v2. "
        "A time range keeps each merge local to one shard."
    )
    cand = _candidate(
        scenario="how tables are sharded",
        outcome="shard by time range",
        reasoning="A time range keeps each merge local to one shard.",
        quote="We shard by time range; sharding by tenant is deferred to v2.",
        alternatives=[
            {"option": "Defer sharding by tenant", "reason": "A time range keeps each merge local"}
        ],
        mentions=[],
    )
    world = _World(monkeypatch, [cand])
    _chunks(monkeypatch, (text, ("Sharding",)))
    report = await _run()
    (inp,) = world.recorded
    assert inp.alternatives == []
    (d,) = report.dropped
    assert d.reason == (
        "rejected option 'Defer sharding by tenant' left out: the quote states it as part of "
        "the choice"
    )
    assert cli.exit_code(report) == 0
