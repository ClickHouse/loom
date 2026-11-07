"""`loom decisions extract`: grounding, the support check, mention resolution and the
reconcile and retire branches, over a fake model and stubbed planes."""

from __future__ import annotations

import argparse
import json
import sys
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
        "evidence": ["We spill into sorted runs like ORDER BY."],
        "reasons": [
            {
                "text": "a run merges like ORDER BY",
                "evidence": ["We spill into sorted runs like ORDER BY."],
            }
        ],
        "rejected": [
            {
                "option": "a plain set on disk",
                "reason": "costs random reads",
                "evidence": ["A plain set on disk costs random reads."],
            }
        ],
        "is_constraint": False,
        "stage": "decided",
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


def _flat(text: str) -> str:
    """`text` as the fake support check compares it: whitespace and case aside."""
    return " ".join(text.split()).casefold()


# The world of the running test: a model a test swaps in answers its reads, the world its
# support checks (`_Routed`).
_WORLD: Any = None


class _Routed:
    """A test's own model for the reads, the world's fake for the support checks."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def complete(self, system: str, user: str, **k: Any) -> Any:
        if system.startswith(dx.SUPPORT_SYSTEM):
            world = _WORLD
            world.support_prompts.append(user)
            reply = json.dumps(world.verdicts(user))
            return SimpleNamespace(text=reply, stop_reason="", provider="openai")
        return await self.inner.complete(system, user, **k)


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
        self.search_queries: list[str] = []
        # Replies served before `reply`, one per call: a candidate list, or an error raised.
        self.replies: list[Any] = []
        # The live document's text, which the fake support check reads.
        self.content = DOC
        self.title = ""
        # Support-check behaviour: claim ids it does not support, stored keys it finds no
        # longer stated (on every check), stored key -> candidate index that restates it.
        self.unsupported: set[str] = set()
        self.gone_keys: set[str] = set()
        self.same_as: dict[str, int] = {}
        # Raw support replies served before the default verdicts, one per check.
        self.support_replies: list[Any] = []
        self.support_prompts: list[str] = []
        # The candidates of the last read, which the fake support check judges against.
        self.read_cands: list[dict[str, Any]] = []
        self._patch(monkeypatch)
        # Set for this test only: monkeypatch puts the module back after it.
        monkeypatch.setattr(sys.modules[__name__], "_WORLD", self)
        from server.settings import Settings, settings

        # LOOM_LLM_PROVIDER=fake set on purpose: the run does not refuse the fake model.
        monkeypatch.setattr(settings, "llm_provider", "fake")
        monkeypatch.setattr(Settings, "llm_provider_inferred", lambda self: False)

    def verdicts(self, user: str) -> dict[str, Any]:
        """The support check's reply. Every claim is supported unless named in
        `unsupported`. A stored decision is still stated unless in `gone_keys`, as a judge
        would read the document: its reasoning is in the live text and no candidate under
        its key gives other reasons or rejected options; `same_as` names, unless set, the
        candidate whose outcome and reasons are the stored row's under another key."""
        import re

        claims = json.loads(user.split("\n\n[candidates]\n\n", 1)[1].split("\n\n[stored")[0])
        outcomes = {
            int(c["id"].split(".")[0]): c["claim"].removeprefix("The document says: ")
            for c in claims
            if c["id"].endswith(".o")
        }
        verdict_claims = [
            {"id": c["id"], "why": "", "supported": c["id"] not in self.unsupported} for c in claims
        ]
        stored = []
        for key in re.findall(r'\{"id": "([^"]+)", "statement"', user):
            still, heir = self._judge(key, outcomes)
            stored.append({"id": key, "why": "", "still_stated": still, "same_as": heir})
        return {"claims": verdict_claims, "stored": stored}

    def _judge(self, key: str, outcomes: dict[int, str]) -> tuple[bool, int | None]:
        if key in self.same_as:
            return key not in self.gone_keys, self.same_as[key]
        fields = self.records[key][1] if key in self.records else {}
        reasoning = str(fields.get("reasoning") or "")
        restated = any(
            _same_fields(c)["reasoning"].casefold() == _flat(reasoning) for c in self.read_cands
        )
        said = (
            reasoning == fields.get("outcome")
            or restated
            or bool(dx.find_span(reasoning, [self.content]))
        )
        mine = [c for c in self.read_cands if _key(c) == key]
        if mine and _same_fields(mine[0])["reasoning"].casefold() != _flat(reasoning):
            said = False
        stored_alts = str(fields.get("alternatives", "")).casefold()
        if mine and _same_fields(mine[0])["alternatives"].casefold() != stored_alts:
            said = False
        heir = None
        for i, outcome in outcomes.items():
            same = [c for c in self.read_cands if c["outcome"] == outcome and _key(c) != key]
            if same and (
                same[0]["outcome"].casefold() == str(fields.get("outcome") or "").casefold()
                and _same_fields(same[0])["reasoning"].casefold() == reasoning.casefold()
            ):
                heir = i
        return said and key not in self.gone_keys, heir

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
                if system.startswith(dx.SUPPORT_SYSTEM):
                    world.support_prompts.append(user)
                    if world.support_replies:
                        reply = world.support_replies.pop(0)
                        if isinstance(reply, Exception):
                            raise reply
                        return SimpleNamespace(text=reply, stop_reason="")
                    return SimpleNamespace(text=json.dumps(world.verdicts(user)), stop_reason="")
                world.prompts.append((system, user))
                reply = world.replies.pop(0) if world.replies else world.reply
                if isinstance(reply, Exception):
                    raise reply
                world.read_cands = [c for c in reply if isinstance(c, dict)]
                return SimpleNamespace(text=json.dumps({"decisions": reply}), stop_reason="")

        async def knowledge_get(inp):
            return SimpleNamespace(
                found=inp.source_uri in world.docs, content=world.content, title=world.title
            )

        async def chunks(org, ns, uri):
            return [CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT"))]

        async def writer(org, ns, key, ch=None):
            world.writer_reads.append(key)
            here = any(r.memory_key == key and r.namespace == ns for r in world.rows)
            return world.records[key][0] if key in world.records and here else None

        def same(ident, r):
            return all(
                getattr(ident, a) in ("*", getattr(r, a))
                for a in ("agent", "user_id", "session_id")
            )

        async def get_by_key(ident, key, *, scope, fresh=False, strong=False):
            # As the store: a `*` agent drops the identity filter and the newest row wins.
            rows = [r for r in world.rows if r.namespace == ident.namespace]
            if ident.agent == "*":
                hits = [r for r in rows if r.memory_key == key]
            else:
                hits = [r for r in rows if r.memory_key == key and same(ident, r)]
            return hits[-1] if hits else None

        async def many(ident, keys, *, scope, strong=False):
            world.bulk_reads += 1
            return [
                r
                for r in world.rows
                if r.memory_key in keys and same(ident, r) and r.namespace == ident.namespace
            ]

        async def list_rows(ident, *, scope, memory_type, order_by="", limit=200, offset=0, **kw):
            rows = [r for r in world.rows if same(ident, r) and r.namespace == ident.namespace]
            if kw.get("pinned_only"):
                rows = [r for r in rows if r.pinned]
            if kw.get("key_prefix"):
                rows = [r for r in rows if r.memory_key.startswith(kw["key_prefix"])]
            return rows[offset : offset + limit]

        async def mem_search(inp):
            world.search_queries.append(inp.query)
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

        from server.code.app.index import repo_registry

        async def no_refresh(**kw):
            return []

        mp.setattr(repo_registry, "refresh_db_repos", no_refresh)
        mp.setattr(repo_registry, "registry_read", lambda: True)
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
    reasons = "; ".join(r["text"].rstrip(" .;") for r in c["reasons"])
    return {
        "category": c["category"],
        "scenario": c["scenario"],
        "outcome": c["outcome"],
        "reasoning": reasons or c["outcome"],
        "alternatives": "\n".join(
            f"{a['option']} — rejected: {a['reason']}" if a["reason"] else a["option"]
            for a in c["rejected"]
        ),
    }


# ------------------------------------------------------------------ grounding


def _cand(**over: Any) -> dx.Candidate:
    (cand,) = dx.parse_candidates(json.dumps({"decisions": [_candidate(**over)]})) or []
    return cand


def test_a_candidate_whose_quotes_are_the_documents_keeps_every_claim() -> None:
    grounded, notes = dx.ground(_cand(), [BODY])
    assert notes == [] and grounded is not None
    assert grounded.evidence == ["We spill into sorted runs like ORDER BY."]
    assert [r.text for r in grounded.reasons] == ["a run merges like ORDER BY"]
    assert [a.option for a in grounded.alternatives] == ["a plain set on disk"]


def test_a_claim_without_a_found_quote_is_left_out_and_an_ungrounded_outcome_drops() -> None:
    cand = _cand(
        reasons=[{"text": "paraphrased", "evidence": ["words the document never wrote down"]}],
        rejected=[{"option": "x", "reason": "", "evidence": ["short"]}],
    )
    grounded, notes = dx.ground(cand, [BODY])
    assert grounded is not None and grounded.reasons == [] and grounded.alternatives == []
    assert notes == [
        "reason 'paraphrased' left out: none of its quotes is in the document text",
        "rejected option 'x' left out: none of its quotes is in the document text",
    ]
    ungrounded = _cand(evidence=["not in the deck at all"])
    assert dx.ground(ungrounded, [BODY]) == (None, [dx.UNGROUNDED])


def test_a_quote_is_stored_as_the_documents_own_words() -> None:
    cand = _cand(evidence=["we SPILL into sorted  runs like order by."])
    grounded, _notes = dx.ground(cand, [BODY])
    assert grounded is not None
    assert grounded.evidence == ["We spill into sorted runs like ORDER BY."]


def test_a_leading_verb_is_left_out_of_the_outcome() -> None:
    assert _cand(outcome="Decided to spill into sorted runs").outcome == "spill into sorted runs"
    assert _cand(outcome="Proposed: spill").outcome == "spill"


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
    assert "[section: Deck > Two]" in dx.user_prompt(w, "")


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


@pytest.mark.parametrize(
    ("path", "governed"),
    [
        ("frontend/src/views/Recent.mentor.test.tsx", False),
        ("tests/unit/test_decision_ops.py", False),
        ("src/Interpreters/tests/gtest_hash_join.cpp", False),
        ("internal/operator/reconcile_test.go", False),
        ("docs/reference/configuration.md", False),
        ("frontend/src/__tests__/api.ts", False),
        ("frontend/src/__mocks__/api.ts", False),
        ("frontend/src/graph.spec.ts", False),
        ("src/join/tests.rs", False),
        ("frontend/e2e/fixtures.ts", False),
        ("src/Common/hash_unittest.cc", False),
        ("internal/mocks/reconciler.go", False),
        ("src/test/java/x/FooTest.java", False),
        ("lib/src/testFixtures/java/Fake.java", False),
        ("app/src/integrationTest/java/X.java", False),
        ("cypress/support/commands.ts", False),
        ("shop/orders/tests.py", False),
        ("conftest.py", False),
        ("internal/controller/mock_reconciler.go", False),
        ("internal/controller/reconciler_mock.go", False),
        ("internal/testdata/cluster.go", False),
        # A spec directory is code: a target-spec module, a protocol spec.
        ("compiler/rustc_target/src/spec/mod.rs", True),
        ("specs/CommitProtocol.tla", True),
        ("docs/guide.markdown", False),
        (".cursor/rules/style.mdc", False),
        # Code whose module name only mentions tests, and a CMake file the index reads as code.
        ("CMakeLists.txt", True),
        ("server/code/domain/index/test_gate.py", True),
        ("src/main/java/x/LoadTest.java", True),
        # An RSpec file is a test; a `*_test` module reads as one too.
        ("spec/models/user_spec.rb", False),
        ("src/Common/self_test.c", False),
        ("src/Interpreters/HashJoin/HashJoin.h", True),
    ],
)
def test_a_decision_governs_code_not_a_test_or_a_markdown_page(path: str, governed: bool) -> None:
    assert dx.governs(path) is governed


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
    assert "At most 3 items whose evidence starts in one section" in world.prompts[0][0]


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing_and_prints_the_bodies(monkeypatch, capsys) -> None:
    world = _World(monkeypatch, [_candidate(), _candidate(evidence=["not in the deck at all"])])
    report = await _run(dry_run=True)
    assert world.recorded == [] and world.updates == [] and world.deletes == []
    cli.print_report(report)
    out = capsys.readouterr().out
    assert "(dry run)" in out and f"would record {_key(_candidate())}" in out
    assert '"agent": "loom-decisions"' in out and '"source_class": "derived"' in out
    assert "unresolved: #4821 (pr)" in out
    assert f"dropped [{SECTION}] {dx.UNGROUNDED}" in out
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
async def test_a_pinned_row_whose_document_changed_only_in_case_is_left_alone(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    # Recorded when the document wrote the reasoning and the reason in upper case.
    fields = {
        **_same_fields(c),
        "reasoning": _same_fields(c)["reasoning"].upper(),
        "alternatives": "a plain set on disk — rejected: COSTS RANDOM READS",
    }
    world.record(_key(c), fields, pinned=True)
    report = await _run()
    assert report.records[0].action == "unchanged"
    assert report.flagged == [] and world.recorded == [] and world.updates == []


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
    # Pinned under the extractor's identity, as memory.pin on an extracted row does.
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set."},
        pinned=True,
        char_limit=900,
    )
    report = await _run()
    assert world.recorded == []
    (upd,) = world.updates
    assert upd.tags == ["decision", "source-changed"] and upd.value is None
    assert upd.memory_id == "id-" + _key(c) + "-" + de.EXTRACTOR_AGENT
    assert upd.char_limit == 900  # the row's own cap, passed back
    (rec,) = report.records
    assert rec.action == "flagged" and rec.body["agent"] == de.EXTRACTOR_AGENT
    cli.print_report(report)
    assert f'"agent": "{de.EXTRACTOR_AGENT}"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_another_writers_pinned_row_is_neither_tagged_nor_revised(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set."},
        agent="alice-agent",
        user_id="alice",
        pinned=True,
    )
    report = await _run()
    # The support check reads only the extractor's rows: another writer's is not judged.
    assert world.recorded == [] and world.updates == []
    assert [r.action for r in report.records] == ["not_ours"]


@pytest.mark.asyncio
async def test_another_writers_unpinned_record_is_not_revised(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, agent="claude")
    report = await _run()
    assert world.recorded == [] and world.updates == []
    assert report.records[0].action == "not_ours"


@pytest.mark.asyncio
async def test_a_frozen_restatement_under_another_key_is_skipped(monkeypatch) -> None:
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
async def test_a_frozen_decision_in_another_category_is_a_restatement(monkeypatch) -> None:
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


def test_a_longer_span_costs_linear_time_against_one_text() -> None:
    # Twice the span's words against one text: a matcher that read a gap more than one way
    # would take exponential time in the words on the final mismatch (about 200 times
    # here); linear time is about twice. Each the best of five runs.
    text = "a\nX\n" * 30

    def best(words: int) -> float:
        span = " ".join(["a"] * words + ["b"])
        runs = []
        for _ in range(5):
            started = time.perf_counter()
            assert dx.find_span(span, [text]) == ""
            runs.append(time.perf_counter() - started)
        return min(runs)

    assert best(20) < 20 * best(10)


@pytest.mark.parametrize(
    ("text", "span"),
    [
        # Each line a word of the span or a label: the final word never matches.
        ("a\nX\n" * 100_000, " ".join(["a"] * 10 + ["b"])),
        # Every match reads across a `not` line and is rejected, one start at a time.
        ("a\nnot\n" * 100_000, "a a a"),
    ],
    ids=["labels", "rejected"],
)
def test_lines_that_all_look_like_labels_cost_linear_time(text, span) -> None:
    # 200,000 lines against a tenth of them pins linear cost in the text's length: a
    # matcher that rescanned the text per start would take quadratic time. Linear time is
    # ten times the small input's, each the best of three runs. The atomic groups are
    # pinned by the span-length test above.
    def best(t: str) -> float:
        runs = []
        for _ in range(3):
            started = time.perf_counter()
            assert dx.find_span(span, [t]) == ""
            runs.append(time.perf_counter() - started)
        return min(runs)

    # Quadratic time would be about 100 times; the margin is for a loaded machine.
    assert best(text) < 60 * best(text[: len(text) // 10])


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
        "Not\n",
        "Don't\n",
        "Never twice\n",
        # One word list each: a ban word, then a contrast word.
        "Only\n",
        "Always\n",
        "Instead\n",
        "Rather\n",
        # Hedges in neither list.
        "Rarely\n",
        "Unless\n",
        "Costs.)\n",
        "**Done.**\n",
        # A script without case has no title-like line.
        "代理\n",
        "- Shard A\n",
        "+ Shard A\n",
        "* Shard A\n",
        "• Shard A\n",
        "▪Shard A\n",
        "***\n",
        "1. first\n",
        "1) first\n",
        "3)\n",
        "`Proxy`\n",
        "~Proxy\n",
        "| A |\n",
        "## Heading\n",
        "> Quoted\n",
        ": Definition\n",
        "= Total\n",
        "===\n",
        "___\n",
        "---\n",
        "-\n",
        "--\n",
        "<Proxy>\n",
        "Shards differ.\n",
        "Why shard?\n",
        "One copy;\n",
        "Shards differ。\n",
    ],
)
def test_a_line_that_is_structure_a_sentence_end_or_a_negation_is_no_label(label) -> None:
    assert dx.find_span(REPLICA_SPAN, [SPLIT.format(labels=label)]) == ""


@pytest.mark.parametrize(
    ("span", "text"),
    [
        # Narrow-wrapped prose: the lower-case line continues the sentence.
        (
            "We keep the index because it serves reads.",
            "We keep the\nold\nindex because it serves reads.",
        ),
        (REPLICA_SPAN, SPLIT.format(labels="shard a\n")),
    ],
)
def test_a_line_starting_lower_case_is_no_label(span, text) -> None:
    assert dx.find_span(span, [text]) == ""


@pytest.mark.parametrize("end", [".", ".)", '."', ".*"])
def test_no_label_is_read_after_a_sentence_end(end) -> None:
    # A plain heading between two paragraphs is no label inside a sentence, whatever
    # closes the sentence after its mark.
    text = f"We cache plans per tenant{end}\n\nCosts\n\nEach plan expires after an hour."
    span = f"We cache plans per tenant{end} Each plan expires after an hour."
    assert dx.find_span(span, [text]) == ""


@pytest.mark.parametrize(
    "label",
    [
        "2.5 GB\n",
        "1.2.3\n",
        "3 replicas\n",
        "+3 nodes\n",
        "-5 °C\n",
        "**Proxy**\n",
        "*Proxy*\n",
        "_Proxy_\n",
    ],
)
def test_a_number_or_emphasis_that_is_no_list_marker_is_a_label(label) -> None:
    text = SPLIT.format(labels=label)
    assert dx.find_span(REPLICA_SPAN, [text]) == text


def test_a_rejected_label_reading_of_the_last_gap_blocks_its_partial_end() -> None:
    # The documented limit: "plan" ending inside "plans" is tried after the label reading
    # across "plans" and "not", which is rejected.
    assert dx.find_span("cache plan", ["cache\nplans\nnot\nplan"]) == ""


def test_a_line_of_a_script_without_case_is_not_read_across() -> None:
    text = "分片按租户\n代理\n划分以保持本地。"
    assert dx.find_span("分片按租户 划分以保持本地。", [text]) == ""
    assert dx.find_span("分片按租户 代理 划分以保持本地。", [text]) == text


@pytest.mark.parametrize("word", ["Not", "Rarely"])
def test_a_negation_or_hedge_line_is_not_skipped(word) -> None:
    assert dx.find_span("Retries are safe", [f"Retries are\n{word}\nsafe"]) == ""


def test_a_wrapped_paragraph_end_is_not_read_across() -> None:
    text = "We cache plans per tenant because\nPlanning costs.\n\nEach plan expires after an hour."
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
async def test_a_quote_across_labels_in_another_case_grounds_as_the_documents(monkeypatch) -> None:
    text = SPLIT.format(labels="Shard A\nShard B\n")
    reason = {"text": "a read stays in its region", "evidence": [REPLICA_SPAN.lower()]}
    world = _World(
        monkeypatch,
        [
            _candidate(
                evidence=[REPLICA_SPAN.lower()],
                reasons=[reason],
                outcome="one replica per region",
                rejected=[],
            )
        ],
    )
    _chunks(monkeypatch, (text, ("Spilling", "Final DISTINCT")))
    world.content = f"# Spilling\n\n## Final DISTINCT\n\n{text}\n"
    await _run()
    (inp,) = world.recorded
    assert inp.reasoning == "a read stays in its region"
    # The support check reads the quotes as the document has them, labels left out.
    assert REPLICA_SPAN in world.support_prompts[0]


# ------------------------------------------------------------------ retire


@pytest.mark.asyncio
async def test_rows_found_gone_are_retired_or_flagged(monkeypatch) -> None:
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

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Prose()))
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
    # The pinned row carries source-changed, set by hand or by an earlier run; the support
    # check finds it stated. This run's model leaves the key out: the retire step keeps the
    # tag.
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
    # The second run's model words the same decision differently: another key. The text
    # still states the stored row, so it stands and the reworded one is not written.
    second = _candidate(scenario="spilling the final DISTINCT")
    world.reply = [second]
    report = await _run()
    assert report.records == [] and world.deletes == [] and len(world.recorded) == 1
    assert f"restates {_key(first)}, not found gone" in [d.reason for d in report.dropped]
    # Once the text no longer states the stored row, its restatement replaces it.
    world.gone_keys = {_key(first)}
    world.same_as = {_key(first): 0}
    report = await _run()
    assert [r.action for r in report.records] == ["written"]
    assert report.replaced == [f"{_key(first)} -> {_key(second)}"]
    assert [d.memory_key for d in world.deletes] == [_key(first)]
    assert report.retired == []


@pytest.mark.asyncio
async def test_a_pinned_row_with_a_reasoning_recorded_anew_is_kept(monkeypatch) -> None:
    first = _candidate()
    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    world.record(_key(first), _same_fields(first), files=[HEADER], pinned=True)
    report = await _run()
    assert report.replaced == [] and world.deletes == []


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
    changed = {
        "text": "reads stay sequential",
        "evidence": ["A plain set on disk costs random reads."],
    }
    world.reply = [{**c, "reasons": [changed]}]
    report = await _run(dry_run=True)
    assert report.records[0].action == "revised" and report.records[0].body["files"] == [HEADER]


@pytest.mark.asyncio
async def test_a_scenario_with_no_key_letters_is_dropped_and_the_run_goes_on(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate(category="设计", scenario="?! …"), _candidate()])
    report = await _run()
    assert [d.reason for d in report.dropped] == ["no letters or digits to build a key from"]
    assert [r.action for r in report.records] == ["written"] and len(world.recorded) == 1


def test_a_blank_reason_drops_the_alternative_and_a_reason_is_read_stripped() -> None:
    (cand,) = (
        dx.parse_candidates(
            json.dumps(
                {
                    "decisions": [
                        _candidate(
                            rejected=[
                                {"option": "a hash set", "reason": "   ", "evidence": ["q"]},
                                {"option": "a plain set on disk", "reason": " costs random reads"},
                                {"option": " on disk ", "reason": " slow ", "evidence": [" q "]},
                            ]
                        )
                    ]
                }
            )
        )
        or []
    )
    # A blank reason is no reason; an option without quotes is left out.
    assert cand.alternatives == [
        dx.Rejected("a hash set", "", ("q",)),
        dx.Rejected("on disk", "slow", ("q",)),
    ]
    assert cand.rejected() == ["a hash set", "on disk — rejected: slow"]


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
    assert "- design:spilling | how the final DISTINCT spills to disk\n" in system + "\n"
    # The old outcome is never shown: the text as it is now gives it.
    assert "spill into sorted runs" not in system
    assert system.count("\n- design:") == 1
    assert dx.system_prompt(3) == dx.SYSTEM.replace("{max_per_section}", "3")


@pytest.mark.asyncio
async def test_a_row_with_an_outcome_found_gone_is_tagged_not_deleted(
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
    # The reworded key is another identity's, with the same decision: the extractor's own
    # row, still stated, stands, and the restatement is not written under the other key.
    world.record(_key(second), _same_fields(second), agent="claude", files=[HEADER])
    report = await _run()
    assert report.records == [] and world.recorded == []
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
        f"reply cut at the token limit ({cap}); lower --window-chars "
        "(LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS) or --max-per-section, or "
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

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Cut()))
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "reason"),
    [
        ("length", _cut(12000)),
        ("max_tokens", _cut(12000)),
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
    assert asked == [12000]


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
    alts = [{"option": f"option {i}", "reason": r, "evidence": [r]} for i, r in enumerate(reasons)]
    # A case-only duplicate of the first, which decision.record folds away too.
    alts.append({"option": "OPTION 0", "reason": reasons[0], "evidence": [reasons[0]]})
    body = BODY + " " + " ".join(reasons)
    world = _World(monkeypatch, [_candidate(rejected=alts)])
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
@pytest.mark.parametrize("outcome", ["spill into sorted runs", "Spill into sorted runs"])
async def test_one_decision_worded_twice_across_overlapping_windows_is_written_once(
    monkeypatch, outcome
) -> None:
    from server.knowledge.app import document_reads

    first = _candidate()
    # The second window's outcome may differ in case only: still one decision.
    second = _candidate(scenario="spilling the final DISTINCT", outcome=outcome)
    world = _World(monkeypatch, [])
    replies = [[first], [second]]

    class _TwoWindows:
        async def complete(self, *a: Any, **k: Any) -> Any:  # one reply per window
            return SimpleNamespace(text=json.dumps({"decisions": replies.pop(0)}), stop_reason="")

    async def chunks(org, ns, uri):
        # Two chunks whose stride overlaps: each window holds the same text.
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Final DISTINCT")),
        ]

    from server.llm import client as llm_client

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_TwoWindows()))
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

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Reuser()))
    report = await _run()
    assert report.records[0].memory_key == _key(c) == svc.decision_key(first.category, scenario)
    assert report.records[0].action == "unchanged"


@pytest.mark.asyncio
async def test_the_prompt_lists_at_most_the_configured_known_decisions(monkeypatch) -> None:
    from server.settings import settings

    world = _World(monkeypatch, [])
    for i in range(3):
        fields = {"category": "design", "scenario": f"known {i}", "outcome": "o"}
        world.record(f"decision:known-{i}", fields)
    monkeypatch.setattr(settings, "memory_decision_extract_known_max", 2)
    await _run()
    listed = world.prompts[0][0].split("as the text now gives them:\n", 1)[1]
    assert len(listed.splitlines()) == 2 and "known 2" not in listed


def test_clean_alternatives_can_keep_them_uncut() -> None:
    long = "a hash set — rejected: " + "slow " * 80
    assert svc.is_cut(svc.clean_alternatives([long])[0])
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
async def test_an_attested_row_found_gone_is_tagged_not_deleted(monkeypatch) -> None:
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
    # decision.record and memory.delete_version declare an audit row. The update handler
    # is stubbed here; its own row is tested in test_memory_update_by_key.py.
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
        evidence=[body],
        reasons=[{"text": "it owns the parts", "evidence": [body]}],
        rejected=[],
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
async def test_a_still_stated_duplicate_is_never_deleted(monkeypatch) -> None:
    # This run words the outcome anew under the old key (no change), and an older row
    # under a reworded key holds the stored outcome: it is the duplicate to replace.
    c = _candidate()
    world = _World(monkeypatch, [{**c, "outcome": "spill sorted runs to disk"}])
    world.record(_key(c), _same_fields(c), files=[HEADER])
    world.record("decision:older", _same_fields(c), files=[HEADER])
    # The support check reads the older row as the same decision as this run's candidate,
    # and still stated: a duplicate is never deleted on one verdict, so both stay.
    world.same_as = {"decision:older": 0}
    report = await _run()
    assert report.records[0].action == "unchanged"
    assert report.replaced == [] and world.deletes == []


def test_an_error_goes_to_stderr(capsys) -> None:
    report = de.ExtractReport("missing.md", True, error="no live document")
    cli.print_report(report)
    out = capsys.readouterr()
    assert out.out == "" and out.err == "error: no live document\n"


def test_an_attestation_binding_the_value_of_a_row_with_no_hash_column_freezes_it() -> None:
    from server.core.hashing import sha256_value_json
    from server.memory.app.decision_extract import _frozen

    value = "Decided spill."
    bound = sha256_value_json(json.dumps(value))
    row = _row("decision:k", value=value, content_sha256="", verified_by="human:alice")
    assert _frozen(_Row(**{**vars(row), "verified_sha256": bound}))
    assert not _frozen(_Row(**{**vars(row), "verified_sha256": "other"}))


def test_a_list_field_the_model_filled_with_something_else_reads_empty() -> None:
    reply = json.dumps(
        {"decisions": [_candidate(mentions=3, rejected=True, reasons={"text": "x"})]}
    )
    (cand,) = dx.parse_candidates(reply) or []
    assert cand.mentions == [] and cand.alternatives == [] and cand.reasons == []
    bad = _candidate(rejected=[{"option": "x", "reason": "y", "evidence": "a quote"}])
    (cand,) = dx.parse_candidates(json.dumps({"decisions": [bad]})) or []
    assert cand.alternatives == []


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
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Flaky()))
    report = await _run(window_chars=len(BODY) + 1)
    assert [d.reason for d in report.dropped] == ["the model call failed: ReadTimeout"]
    assert len(world.recorded) == 1


@pytest.mark.asyncio
async def test_a_held_model_call_stops_the_run_before_any_write(monkeypatch) -> None:
    from server.core.errors import ModelCallRefused
    from server.knowledge.app import document_reads
    from server.llm import client as llm_client

    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})
    calls: list[int] = []

    class _Held:
        async def complete(self, *a: Any, **k: Any) -> Any:
            calls.append(1)
            if len(calls) > 1:
                raise ModelCallRefused("over budget", hold_s=60.0)
            return SimpleNamespace(text=json.dumps({"decisions": [_candidate()]}), stop_reason="")

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT")),
            CorpusChunk(uri, 1, BODY, ("Spilling", "Other")),
            CorpusChunk(uri, 2, BODY, ("Spilling", "Third")),
        ]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Held()))
    report = await _run(window_chars=len(BODY) + 1)
    # The first window's decision is not written and nothing is retired; no third call.
    assert isinstance(report.held, ModelCallRefused) and len(calls) == 2
    assert report.error == "model calls for o are held (MODEL_CALL_REFUSED); try again later"
    assert world.recorded == [] and world.deletes == [] and world.audits == []
    assert cli.exit_code(report) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["disabled", "refused"])
async def test_a_hold_on_a_write_s_embedding_stops_the_run_as_a_hold(monkeypatch, hold) -> None:
    from server.core.errors import ModelCallRefused
    from server.llm.customer import ModelCallsDisabled
    from server.memory.app import search

    world = _World(monkeypatch, [_candidate()])
    world.record("decision:gone", {"reasoning": "gone"})

    async def held_search(inp):
        # The restatement search embeds the candidate: the embedding call is held.
        raise ModelCallsDisabled("off") if hold == "disabled" else ModelCallRefused("budget")

    monkeypatch.setattr(search, "handle_memory_search", held_search)
    report = await _run()
    # A hold, not a refused write: nothing is counted refused and nothing is retired.
    assert report.held is not None and report.error.startswith("model calls for o are held")
    assert report.dropped == [] and report.refused == []
    assert world.recorded == [] and world.deletes == [] and report.retired == []
    assert cli.exit_code(report) == 1


@pytest.mark.asyncio
async def test_a_read_deadline_that_passes_while_reading_writes_nothing(monkeypatch) -> None:
    import asyncio
    import time

    from server.knowledge.app import document_reads
    from server.llm import client as llm_client

    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})

    class _Slow:
        async def complete(self, *a: Any, **k: Any) -> Any:
            await asyncio.sleep(5)

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, BODY, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Slow()))
    started = time.monotonic()
    report = await _run(read_deadline=time.monotonic() + 0.05)
    assert time.monotonic() - started < 2
    assert report.out_of_time and report.error.startswith("the run's time to read")
    # Neither a write nor its audit row, and nothing retired.
    assert world.recorded == [] and world.audits == [] and world.deletes == []
    assert report.dropped == []


def _two(monkeypatch) -> _World:
    other = _candidate(scenario="how the merge reads its runs", outcome="merge runs by key")
    world = _World(monkeypatch, [_candidate(), other])
    world.record("decision:gone", {"reasoning": "gone"})
    return world


@pytest.mark.asyncio
async def test_a_write_permission_lost_mid_run_stops_after_the_current_record(
    monkeypatch,
) -> None:
    world = _two(monkeypatch)
    answers = [True, False]
    report = await _run(may_write=lambda: answers.pop(0) if answers else False)
    # The first record landed with its audit row; the second was not started.
    assert len(world.recorded) == 1
    assert [e for e, _, _ in world.audits] == ["decision.record"]
    assert report.write_stopped and report.error.startswith("stopped before every write")
    assert world.deletes == [] and report.retired == [] and cli.exit_code(report) == 1


@pytest.mark.asyncio
async def test_a_hold_on_a_later_write_keeps_what_was_written_and_retires_nothing(
    monkeypatch,
) -> None:
    from server.llm.customer import ModelCallsDisabled
    from server.memory.app import decision

    world = _two(monkeypatch)
    record = decision.handle_decision_record

    async def held_second(inp):
        if world.recorded:
            raise ModelCallsDisabled("off")
        await record(inp)

    monkeypatch.setattr(decision, "handle_decision_record", held_second)
    report = await _run()
    assert len(world.recorded) == 1
    assert [e for e, _, _ in world.audits] == ["decision.record"]
    assert report.held is not None and world.deletes == [] and report.retired == []


@pytest.mark.asyncio
async def test_a_cancelled_run_finishes_its_current_write_and_shutdown_drains_it(
    monkeypatch,
) -> None:
    import asyncio

    from server.core.background import drain_task_sets
    from server.memory.app import decision

    world = _World(monkeypatch, [_candidate()])
    record = decision.handle_decision_record
    started = asyncio.Event()

    async def slow(inp):
        started.set()
        await asyncio.sleep(0.05)
        await record(inp)

    monkeypatch.setattr(decision, "handle_decision_record", slow)
    run = asyncio.create_task(_run())
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert await drain_task_sets(timeout_s=2.0)
    # The record and its audit row both landed: no row without its audit.
    assert len(world.recorded) == 1
    assert [e for e, _, _ in world.audits] == ["decision.record"]


@pytest.mark.asyncio
async def test_a_run_s_writes_are_its_own_and_finish_writes_waits_for_those_only(
    monkeypatch,
) -> None:
    import asyncio

    from server.core.background import TaskSet
    from server.memory.app import decision

    world = _World(monkeypatch, [_candidate()])
    mine, other = TaskSet(), TaskSet()
    release = asyncio.Event()
    other.spawn(release.wait(), name="another-run")  # another run's unit, still running
    record = decision.handle_decision_record
    started = asyncio.Event()

    async def slow(inp):
        started.set()
        await asyncio.sleep(0.05)
        await record(inp)

    monkeypatch.setattr(decision, "handle_decision_record", slow)
    run = asyncio.create_task(_run(writes=mine))
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    # The cancelled run's unit is held in its own set, still running.
    assert [t for t in mine if not t.done()] and world.recorded == []
    await asyncio.wait_for(de.finish_writes(mine), timeout=1.0)
    assert len(world.recorded) == 1 and [e for e, _, _ in world.audits] == ["decision.record"]
    # The other run's unit did not hold the wait, and is still pending.
    assert not all(t.done() for t in other)
    release.set()
    await de.finish_writes(other)


@pytest.mark.asyncio
async def test_a_blacklisted_candidate_is_a_permanent_refusal(monkeypatch) -> None:
    from fastapi import HTTPException

    world = _World(monkeypatch, [_candidate()])
    # As the blacklist guard refuses a write that matches a pattern.
    world.refuse_record = HTTPException(422, detail={"code": "BLACKLISTED"})
    report = await _run()
    [dropped] = report.dropped
    assert dropped.refused and dropped.permanent
    assert dropped.reason.endswith("refused: BLACKLISTED")


@pytest.mark.asyncio
async def test_the_sync_worker_s_writes_are_audited_as_the_worker(monkeypatch) -> None:
    from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
    from server.memory.domain.provenance import pipeline_actor

    world = _World(monkeypatch, [_candidate()])
    with pipeline_actor(SYNC_WORKER_ACTOR):
        await _run()
    assert [(e, a) for e, a, _ in world.audits] == [("decision.record", SYNC_WORKER_ACTOR)]


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

    from server.llm import client as llm_client

    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone"})

    class _Down:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise httpx.ConnectError("down")

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Down()))
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

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Routed(_Primary()))


def test_a_document_that_fits_the_window_is_read_whole_across_its_sections() -> None:
    # 2,000 characters before the second heading: past a third of a 3,000-character window,
    # where windows() alone would close the window at the section change.
    chunks = [
        CorpusChunk("u", 0, "a" * 2000, ("Problem",)),
        CorpusChunk("u", 1, "b" * 900, ("Decision",)),
    ]
    (whole,) = de._windows(chunks, set(), 3000)
    assert whole.chunks == chunks
    # Past the window it is read in windows of whole sections.
    assert [w.chunks for w in de._windows(chunks, set(), 2500)] == [chunks[:1], chunks[1:]]


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
async def test_a_frozen_restatement_below_five_closer_hits_blocks_within_top_k(
    monkeypatch, top_k, blocked
) -> None:
    """The frozen key is the sixth closest: a top_k of 6 reaches it, 5 stops before it
    though the fetch (5 keys times a per-key cap of 3) still serves it."""
    from server.memory.app import search
    from server.settings import settings

    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    hits = []
    for i in range(5):
        world.add(f"design/near{i}", agent="alice-agent")
        hits.append(SimpleNamespace(memory_key=f"design/near{i}", similarity=0.95 - i / 100))
    world.add("design/frozen", agent="alice-agent", pinned=True)
    hits.append(SimpleNamespace(memory_key="design/frozen", similarity=0.86))
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
        assert report.records[0].note == "already recorded by alice-agent under design/frozen"
    else:
        assert len(world.recorded) == 1


@pytest.mark.asyncio
async def test_a_frozen_restatement_below_keys_of_many_rows_still_blocks(monkeypatch) -> None:
    # the search serves a row per writer and session: seven closer keys of three rows each
    # fill 21 places, and the frozen key still counts among the 20 closest keys
    from server.memory.app import search
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    world = _World(monkeypatch, [_candidate(scenario="spilling the final DISTINCT")])
    hits = []
    for i in range(7):
        world.add(f"design/near{i}", agent="alice-agent")
        hits += [SimpleNamespace(memory_key=f"design/near{i}", similarity=0.95 - i / 100)] * 3
    world.add("design/frozen", agent="alice-agent", pinned=True)
    hits.append(SimpleNamespace(memory_key="design/frozen", similarity=0.86))

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
    assert report.records[0].note == "already recorded by alice-agent under design/frozen"
    # each key's rows are read once
    assert read == [*(f"design/near{i}" for i in range(7)), "design/frozen"]


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


def test_a_quote_differing_only_in_whitespace_is_the_documents() -> None:
    text = "We spill into sorted\nruns like ORDER BY.  A plain set on disk costs random reads."
    assert dx.find_span("sorted runs like", [text]) == "sorted\nruns like"
    assert dx.find_span("sorted runslike", [text]) == ""
    grounded, notes = dx.ground(_cand(), [text])
    # Stored on one line, the document's words and case.
    assert grounded is not None and notes == []
    assert grounded.evidence == ["We spill into sorted runs like ORDER BY."]
    assert grounded.alternatives[0].evidence == ("A plain set on disk costs random reads.",)


@pytest.mark.asyncio
async def test_a_span_with_other_line_breaks_is_recorded_on_one_line(
    monkeypatch,
) -> None:
    from server.knowledge.app import document_reads

    wrapped = BODY.replace("A plain set on disk ", "A plain set\non disk ")
    world = _World(monkeypatch, [_candidate()])

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, wrapped, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    (inp,) = world.recorded
    assert inp.alternatives == ["a plain set on disk — rejected: costs random reads"]
    assert inp.source_resource_id == f"knowledge:ns/doc.md#{SECTION}"
    assert report.dropped == []
    # The support check reads the quote on one line, as the document's words.
    assert "A plain set on disk costs random reads." in world.support_prompts[0]


def test_the_prompt_asks_for_cited_claims_and_skips_recaps_and_superseded_text() -> None:
    prompt = dx.system_prompt(3)
    # Each claim cites quotes; a decision the document gives no reason for has none.
    assert "Every reason and every rejected option has its own evidence" in prompt
    assert "[] when the document gives none: never invent one" in prompt
    assert "A measurement, a summary or recap" in prompt
    assert "former, replaced or superseded" in prompt
    # The support check judges what a claim says, never the stage.
    assert "only proposes it is not checked here" in dx.support_system(False)
    assert "still_stated" in dx.support_system(True) and "still_stated" not in dx.SUPPORT_SYSTEM
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
        return SimpleNamespace(found=True, content=doc, title="")

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
        res = await FakeLLMClient().complete(dx.system_prompt(3), dx.user_prompt(w, ""))
    (cand,) = dx.parse_candidates(res.text) or []
    assert cand.outcome == "We spill into sorted runs like ORDER BY."
    assert cand.reasoning == "A plain set on disk costs random reads"
    grounded, notes = dx.ground(cand, [BODY])
    assert grounded is not None and notes == []
    with customer.for_org("o"):
        res = await FakeLLMClient().complete(
            dx.support_system(True), dx.support_prompt("x", [cand], [("decision:k", "Decided x.")])
        )
    verdicts = dx.parse_support(res.text, {"decision:k"})
    assert verdicts is not None and verdicts.supported == {"0.o", "0.r0"}
    assert verdicts.stored == {"decision:k": (True, None)}


@pytest.mark.asyncio
async def test_a_long_sections_last_chunk_with_an_image_and_a_short_tail_is_sent(
    monkeypatch,
) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    tail = "![a long diagram name](figures/spill-merge-flow.png)\n\nThe merge reads each run once."
    doc = DOC.replace(BODY, f"{BODY}\n\n{tail}")

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc, title="")

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
        return SimpleNamespace(found=True, content=doc, title="")

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
        return SimpleNamespace(found=True, content=doc, title="")

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
        return SimpleNamespace(found=True, content=doc, title="")

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


@pytest.mark.parametrize(
    ("text", "images"),
    [
        ("See ![x](a.png) and ![y](b.png).", ["![x](a.png)", "![y](b.png)"]),
        ('![t](f.png "a title")', ['![t](f.png "a title")']),
        ("![a\nwrapped alt](f.png)", ["![a\nwrapped alt](f.png)"]),
        ("![diagram][fig1]", ["![diagram][fig1]"]),
        ("[![build](badge.svg)](https://ci.example.com)", ["![build](badge.svg)"]),
        ('<img\n  src="flow.png"\n  alt="flow"\n/>', ['<img\n  src="flow.png"\n  alt="flow"\n/>']),
        ("<IMG SRC=c.png>", ["<IMG SRC=c.png>"]),
        ("[image: the spill path] [Image 3]", ["[image: the spill path]", "[Image 3]"]),
        # A target ends at its first `)`, whatever `(` it holds.
        ("![x](diagrams/flow(v2.png)", ["![x](diagrams/flow(v2.png)"]),
        (r"![x](a\(b.png)", [r"![x](a\(b.png)"]),
        ('![x](f.png "see (a")', ['![x](f.png "see (a")']),
        ("![x](a(b.png) and 1) done", ["![x](a(b.png)"]),
        ("![x](it's.png)", ["![x](it's.png)"]),
        # A line end after `(`, before the title and before `)`; a quoted title spans more.
        ("![x](\nf.png)", ["![x](\nf.png)"]),
        ('![x](f.png\n  "title")', ['![x](f.png\n  "title")']),
        ('![x](\nf.png\n"t")', ['![x](\nf.png\n"t")']),
        ('![x](f.png\n"t"\n)', ['![x](f.png\n"t"\n)']),
        ('![x](f.png "a\nb")', ['![x](f.png "a\nb")']),
        ("![x](f.png 'a\nb')", ["![x](f.png 'a\nb')"]),
        ('![x](\nf.png\n"t"\n)', ['![x](\nf.png\n"t"\n)']),
        # Four line ends outside a title are more than a target holds.
        ("![x](a\nb\nc\nd\ne) done", []),
        # A target never runs into the next one.
        ("![x](a](b)", []),
        # A placeholder or a tag stops at the next opener; the image is the later span.
        ("[image: a [b] c]", []),
        ('<img alt="a<b">', []),
        ("![a [<img ](x) >", ["<img ](x) >"]),
        # Unclosed markup is no image.
        ("![open ![x](a.png) <img ](b", ["![x](a.png)"]),
    ],
)
def test_image_markup_matches(text: str, images: list[str]) -> None:
    assert [m.group(0) for m in dx._IMAGE_MARKUP.finditer(text)] == images


def test_link_targets_are_removed_from_a_caption() -> None:
    text = "[notes](https://example.com/notes) and [ref][1], ](dangling"
    assert dx.caption_text(text) == "[notes] and [ref], ](dangling"
    assert dx.caption_text("![x](a](b) done") == "![x](a] done"


@pytest.mark.parametrize(
    "body",
    [
        "<img " * 30_000,
        "![" * 30_000,
        "![x](a" * 30_000,
        "[image:" * 6_000,
        # Words and an image, so the caption is read and its link targets removed.
        "![x](y)" + "a](" * 40_000,
    ],
    ids=["img-tag", "image-alt", "image-target", "placeholder", "link-target"],
)
def test_unclosed_image_markup_is_read_in_linear_time(body: str) -> None:
    # A scan from every unclosed opener to the end of the text costs seconds per document.
    start = time.process_time()
    dx.image_only_sections(body)
    assert time.process_time() - start < 0.5


@pytest.mark.asyncio
async def test_a_linked_badge_and_caption_preamble_is_not_sent(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    badge = f"{_LINKED_BADGE}\n\nBuild status for the spill code."
    doc = f"{badge}\n\n{DOC}"

    async def knowledge_get(inp):
        return SimpleNamespace(found=True, content=doc, title="")

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
        return SimpleNamespace(found=True, content=picture, title="")

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, picture, ())]

    monkeypatch.setattr(document_reads, "handle_knowledge_get", knowledge_get)
    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    assert world.prompts == [] and report.windows == 0
    assert report.image_only == [""]
    cli.print_report(report)
    assert "image-only, rationale not extracted: (untitled)" in capsys.readouterr().out


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

    # The quote is the second section's, re-wrapped; the outcome's quote is in no chunk.
    cand = _candidate(evidence=["Sorted runs are faster than any set."])
    cand["rejected"][0]["evidence"] = ["A plain set on disk costs random reads."]
    world = _World(monkeypatch, [cand])

    async def chunks(org, ns, uri):
        return [
            CorpusChunk(uri, 0, "We spill into sorted runs like ORDER BY.", ("Spilling", "One")),
            CorpusChunk(uri, 1, "A plain set on disk\ncosts random reads.", ("Spilling", "Two")),
        ]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    report = await _run()
    assert world.recorded == [] and report.retried == []
    # Named by the section the candidate's first quote is in, none of the outcome's found.
    assert [(d.section, d.reason) for d in report.dropped] == [("Spilling > One", dx.UNGROUNDED)]


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
        return SimpleNamespace(found=True, content=doc, title="")

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
            _candidate(
                evidence=[recap], rejected=[], reasons=[{"text": "r", "evidence": [reason]}]
            ),
            _candidate(
                scenario="the merge",
                outcome="one merge pass",
                evidence=["One merge pass reads each run."],
                reasons=[{"text": "random reads cost", "evidence": [reason]}],
                rejected=[],
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
    # A reason may rest on a recap's text; only an outcome there is no decision.
    assert inp.reasoning == "random reads cost"
    assert inp.source_resource_id.endswith("Spilling > Merge")
    assert "[section: Spilling > Recap]" in world.prompts[0][1]


# ------------------------------------------------------------------ linked to a repository

TRUNK = "code-widgets"
REPO_TAG = "repo:0123456789"
SOURCE_CHANGED = "source-changed"


def _link_repo(
    monkeypatch: pytest.MonkeyPatch,
    world: _World,
    *,
    refused: frozenset[tuple[str, str]] = frozenset(),
    code_namespaces: frozenset[str] = frozenset(),
) -> dict[str, list[str]]:
    """A repository acme/widgets with trunk TRUNK, the caller refused the `refused`
    (namespace, role) pairs; returns the namespaces each read went to."""
    from server.code.app import repo_scope
    from server.code.app import symbol as code_symbol
    from server.code.domain.index import repos

    seen: dict[str, list[str]] = {"symbol": [], "writer": [], "list": []}

    def trunk(org, repo):
        # acme/bare: a repository with no remote to tag rows with
        known = {
            "acme/widgets": (TRUNK, REPO_TAG),
            "acme/bare": (TRUNK, ""),
            "acme/gadgets": ("code-gadgets", "repo:9999999999"),
        }
        return known.get(repo, ("", ""))

    async def may(org, ns, role, *, hides_answer=True):
        return (ns, role.name.lower()) not in refused

    symbol = code_symbol.handle_code_symbol

    async def code_symbol_handler(inp):
        seen["symbol"].append(inp.namespace)
        return await symbol(inp)

    writer = de.live_writer_record

    async def writer_in(org, ns, key, ch=None):
        seen["writer"].append(ns)
        return await writer(org, ns, key)

    listing = de.list_by_namespace

    async def list_in(ident, **kw):
        seen["list"].append(ident.namespace)
        return await listing(ident, **kw)

    monkeypatch.setattr(repo_scope, "repo_trunk", trunk)
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: ns in code_namespaces or None)
    monkeypatch.setattr(de, "row_in_scope", may)
    monkeypatch.setattr(code_symbol, "handle_code_symbol", code_symbol_handler)
    monkeypatch.setattr(de, "live_writer_record", writer_in)
    monkeypatch.setattr(de, "list_by_namespace", list_in)
    return seen


@pytest.mark.asyncio
async def test_without_a_repo_the_documents_namespace_resolves_names_and_holds_rows(
    monkeypatch,
) -> None:
    world = _World(monkeypatch, [_candidate()])
    seen = _link_repo(monkeypatch, world)
    await _run()
    (inp,) = world.recorded
    assert inp.namespace == "ns" and REPO_TAG not in inp.tags
    assert seen == {"symbol": ["ns"], "writer": ["ns"], "list": ["ns"]}


@pytest.mark.asyncio
async def test_into_source_resolves_in_the_trunk_and_tags_the_row_beside_the_document(
    monkeypatch,
) -> None:
    world = _World(monkeypatch, [_candidate()])
    seen = _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets")  # the document's namespace is no code namespace
    assert report.error == ""
    (inp,) = world.recorded
    assert inp.namespace == "ns"
    assert inp.tags == ["source-kind:design_doc", REPO_TAG]
    assert inp.files == [HEADER]
    assert inp.source_resource_id == f"knowledge:ns/doc.md#{SECTION}"
    # The trunk is listed too, for this document's rows a run may have written there.
    assert seen == {"symbol": [TRUNK], "writer": ["ns"], "list": ["ns", TRUNK]}


@pytest.mark.asyncio
async def test_into_repo_writes_to_the_trunk_citing_the_document_in_its_home(
    monkeypatch,
) -> None:
    world = _World(monkeypatch, [_candidate()])
    seen = _link_repo(monkeypatch, world)
    await _run(repo="acme/widgets", into="repo")
    (inp,) = world.recorded
    assert inp.namespace == TRUNK and inp.tags == ["source-kind:design_doc"]
    assert inp.source_resource_id == f"knowledge:ns/doc.md#{SECTION}"
    assert seen == {"symbol": [TRUNK], "writer": [TRUNK], "list": [TRUNK, "ns"]}


@pytest.mark.asyncio
async def test_a_document_in_a_code_namespace_defaults_into_the_repo(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    _link_repo(monkeypatch, world, code_namespaces=frozenset({"ns"}))
    await _run(repo="acme/widgets")
    (inp,) = world.recorded
    assert inp.namespace == TRUNK


@pytest.mark.asyncio
async def test_into_repo_reconciles_against_the_trunks_own_row(monkeypatch) -> None:
    """The trunk's row for this document, unchanged, is left as it is: the reconcile read
    the record and the row where the run writes."""
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), namespace=TRUNK, files=[HEADER], tags=["decision"])
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets", into="repo")
    assert world.recorded == []
    assert [r.action for r in report.records] == ["unchanged"]


@pytest.mark.asyncio
async def test_a_row_extracted_before_the_repo_tag_is_revised_to_carry_it(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER], tags=["decision"])
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets", into="source")
    assert [r.action for r in report.records] == ["revised"]
    assert REPO_TAG in world.recorded[0].tags


@pytest.mark.parametrize(
    ("kw", "refused", "error"),
    [
        # The same answer as for a repository that is not configured.
        (
            {"repo": "acme/widgets"},
            {(TRUNK, "viewer")},
            "no repository 'acme/widgets' you may read is configured in o",
        ),
        (
            {"repo": "acme/widgets", "into": "repo"},
            {(TRUNK, "writer")},
            f"not allowed to write o/{TRUNK}",
        ),
        ({"repo": "acme/widgets"}, {("ns", "writer")}, "not allowed to write o/ns"),
        ({"repo": "acme/widgets", "into": "repo"}, {("ns", "viewer")}, "not allowed to read o/ns"),
        (
            {"repo": "acme/other"},
            set(),
            "no repository 'acme/other' you may read is configured in o",
        ),
        ({"into": "source"}, set(), "--into needs --repo"),
        ({}, {("ns", "viewer")}, "not allowed to read o/ns"),
        ({"into": "elsewhere"}, set(), "--into is repo or source, not 'elsewhere'"),
        (
            {"repo": "acme/bare"},
            set(),
            "repository 'acme/bare' has no remote to tag its rows with; use --into repo",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_run_the_caller_may_not_make_refuses_before_any_model_call(
    monkeypatch, kw: dict[str, Any], refused: set[tuple[str, str]], error: str
) -> None:
    world = _World(monkeypatch, [_candidate()])
    _link_repo(monkeypatch, world, refused=frozenset(refused))
    report = await _run(**kw)
    assert report.error == error
    assert world.prompts == [] and world.recorded == []
    assert cli.exit_code(report) == 1


@pytest.mark.parametrize(
    ("kw", "broken", "error"),
    [
        ({"repo": "acme/widgets"}, (TRUNK, "viewer"), "no repository 'acme/widgets' you may read"),
        ({"repo": "acme/widgets"}, ("ns", "writer"), "not allowed to write o/ns"),
        ({"repo": "acme/widgets", "into": "repo"}, ("ns", "viewer"), "not allowed to read o/ns"),
    ],
    ids=["trunk", "where the rows go", "document's namespace"],
)
@pytest.mark.asyncio
async def test_an_access_check_that_fails_refuses(
    monkeypatch, kw: dict[str, Any], broken: tuple[str, str], error: str
) -> None:
    world = _World(monkeypatch, [_candidate()])
    _link_repo(monkeypatch, world)

    async def flaky(org, ns, role, *, hides_answer=True):
        if (ns, role.name.lower()) == broken:
            raise RuntimeError("grants unreadable")
        return True

    monkeypatch.setattr(de, "row_in_scope", flaky)
    report = await _run(**kw)
    assert report.error.startswith(error) and world.recorded == [] and world.prompts == []


@pytest.mark.asyncio
async def test_repo_trunk_matches_owner_name_or_a_remote_url(monkeypatch) -> None:
    from server.code.app import repo_scope
    from server.code.app.index import repo_registry
    from server.code.domain.index import repos
    from server.code.domain.index.remote import repo_tag
    from tests.unit.code_fixtures import repo_config

    trunk = repo_config(
        namespace="code-widgets",
        repo="Acme/Widgets",
        git_remote="https://github.com/acme/widgets.git",
        git_branch="main",
    )
    # Listed first and with no remote of its own: the trunk's entry names it.
    branch = repo_config(
        namespace="code-widgets-26-1",
        repo="Acme/Widgets",
        git_branch="release-26.1",
    )

    async def no_refresh(**kw):
        return []

    monkeypatch.setattr(repo_registry, "refresh_db_repos", no_refresh)
    monkeypatch.setattr(repos, "configured_repos", lambda: [branch, trunk])
    monkeypatch.setattr(repo_scope, "configured_repos", lambda: [branch, trunk])
    want = ("code-widgets", repo_tag("https://github.com/acme/widgets.git"))
    assert repo_scope.repo_trunk("o", "acme/widgets") == want
    assert repo_scope.repo_trunk("o", "git@github.com:Acme/Widgets.git") == want
    assert repo_scope.repo_trunk("other-org", "acme/widgets") == ("", "")
    assert repo_scope.repo_trunk("o", "widgets") == ("", "")
    assert repo_scope.repo_names("o") == {want[1]: ("acme/widgets", "code-widgets")}


@pytest.mark.parametrize(
    ("fields", "action"),
    [({}, "unchanged"), ({"realized_outcome": "shipped"}, "outcome_known")],
    ids=["pinned", "outcome recorded"],
)
@pytest.mark.asyncio
async def test_a_row_kept_as_it_is_gains_the_repo_tag_by_a_tag_write(
    monkeypatch, capsys, fields: dict[str, Any], action: str
) -> None:
    """A pinned row, or one whose outcome is recorded, is not recorded again: the
    repository's tag reaches it by a tags-only write, so the pre-edit block serves it."""
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        _same_fields(c) | fields,
        files=[HEADER],
        tags=["decision", SOURCE_CHANGED],
        pinned=not fields,
    )
    _link_repo(monkeypatch, world)
    dry = await _run(dry_run=True, repo="acme/widgets")
    cli.print_report(dry)
    assert f"would tag {REPO_TAG}" in capsys.readouterr().out and world.updates == []
    report = await _run(repo="acme/widgets")
    assert world.recorded == []
    (upd,) = world.updates
    assert sorted(upd.tags) == sorted(["decision", REPO_TAG])
    (rec,) = report.records
    assert rec.action == action
    assert rec.note.endswith(f"source-changed cleared; tagged {REPO_TAG}")


@pytest.mark.asyncio
async def test_a_contradicted_pinned_row_is_flagged_and_tagged_in_one_write(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        _same_fields(c) | {"reasoning": "We spill like ORDER BY does."},
        files=[HEADER],
        pinned=True,
    )
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets")
    (upd,) = world.updates
    assert set(upd.tags) == {"decision", REPO_TAG, SOURCE_CHANGED}
    assert [r.action for r in report.records] == ["flagged"]


@pytest.mark.parametrize(
    ("kw", "home", "error"),
    [
        (
            {"repo": "acme/widgets", "into": "repo"},
            "ns",
            "this document's decisions are in o/ns; run as the run that wrote "
            "them (with no --repo, or its --repo and --into), or delete them there first",
        ),
        (
            {"repo": "acme/widgets", "into": "source"},
            TRUNK,
            f"this document's decisions are in o/{TRUNK}; run as the run that wrote "
            "them (with no --repo, or its --repo and --into), or delete them there first",
        ),
    ],
    ids=["to the trunk", "to the source"],
)
@pytest.mark.asyncio
async def test_a_run_that_would_leave_the_documents_rows_behind_refuses(
    monkeypatch, kw: dict[str, Any], home: str, error: str
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), namespace=home, files=[HEADER])
    _link_repo(monkeypatch, world)
    report = await _run(**kw)
    assert report.error == error
    assert world.prompts == [] and world.recorded == [] and world.deletes == []


@pytest.mark.asyncio
async def test_a_run_without_repo_refuses_over_rows_linked_to_a_repository(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER], tags=["decision", REPO_TAG])
    _link_repo(monkeypatch, world)
    from server.code.app import repo_scope

    monkeypatch.setattr(repo_scope, "repo_names", lambda org: {REPO_TAG: ("acme/widgets", TRUNK)})
    report = await _run()
    # The repository by its name, which --repo takes.
    assert report.error.startswith(
        "this document's decisions are linked to a repository (acme/widgets)"
    )
    assert world.prompts == [] and world.recorded == []


@pytest.mark.asyncio
async def test_a_row_the_model_did_not_emit_again_gains_the_repo_tag(monkeypatch, capsys) -> None:
    """The support check finds it stated, so the retire step keeps it, linked."""
    world = _World(monkeypatch, [])
    world.record(
        "decision:kept",
        {"reasoning": "A plain set on disk costs random reads.", "outcome": "no plain set"},
        files=[HEADER],
    )
    _link_repo(monkeypatch, world)
    dry = await _run(dry_run=True, repo="acme/widgets")
    assert dry.linked == ["decision:kept"] and world.updates == []
    cli.print_report(dry)
    assert "- would tag decision:kept with the repository" in capsys.readouterr().out
    report = await _run(repo="acme/widgets")
    (upd,) = world.updates
    assert upd.tags == ["decision", REPO_TAG]
    assert report.linked == ["decision:kept"] and world.deletes == []


@pytest.mark.asyncio
async def test_a_kept_row_found_gone_is_flagged_and_tagged(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:gone", {"reasoning": "gone from the deck"}, pinned=True)
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets")
    (upd,) = world.updates
    assert set(upd.tags) == {"decision", SOURCE_CHANGED, REPO_TAG}
    assert report.flagged == ["decision:gone"] and report.linked == ["decision:gone"]


@pytest.mark.asyncio
async def test_a_flagged_and_tagged_row_says_so(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        _same_fields(c) | {"reasoning": "We spill like ORDER BY does."},
        files=[HEADER],
        pinned=True,
    )
    _link_repo(monkeypatch, world)
    (rec,) = (await _run(repo="acme/widgets")).records
    assert rec.action == "flagged" and rec.note.endswith(f"tagged {REPO_TAG}")


@pytest.mark.parametrize(
    ("refused", "error"),
    [
        (
            frozenset(),
            f"this document's decisions are in o/{TRUNK}; run as the run that wrote "
            "them (with no --repo, or its --repo and --into), or delete them there first",
        ),
        # Whether rows are there is not told to a caller who cannot read the trunk.
        (
            frozenset({(TRUNK, "viewer")}),
            "this document's namespace belongs to a repository you may not read",
        ),
    ],
    ids=["rows in the trunk", "trunk unreadable"],
)
@pytest.mark.asyncio
async def test_a_run_without_repo_in_a_branch_namespace_checks_its_trunk(
    monkeypatch, refused: frozenset[tuple[str, str]], error: str
) -> None:
    from server.code.app import repo_scope

    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), namespace=TRUNK, files=[HEADER])
    _link_repo(monkeypatch, world, refused=refused)
    # The document is in a release branch's code namespace whose trunk is TRUNK.
    monkeypatch.setattr(repo_scope, "trunk_namespace_of", lambda org, ns: TRUNK)
    report = await _run()
    assert report.error == error
    assert world.prompts == [] and world.recorded == []


@pytest.mark.asyncio
async def test_a_run_with_another_repo_refuses_over_rows_linked_to_the_first(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER], tags=["decision", REPO_TAG])
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/gadgets", into="source")
    # The tag names no configured repository here, so the run prints the tag itself.
    assert report.error == (
        f"this document's decisions are linked to a repository ({REPO_TAG}); run with its "
        "--repo and --into source, or delete them first"
    )
    assert world.prompts == [] and world.recorded == []


@pytest.mark.asyncio
async def test_a_dry_run_needs_only_read_where_the_rows_go(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    _link_repo(monkeypatch, world, refused=frozenset({(TRUNK, "writer")}))
    dry = await _run(dry_run=True, repo="acme/widgets", into="repo")
    assert dry.error == "" and [r.action for r in dry.records] == ["written"]
    applied = await _run(repo="acme/widgets", into="repo")
    assert applied.error == f"not allowed to write o/{TRUNK}" and world.recorded == []


@pytest.mark.parametrize("repo", ["acme/widgets", "acme/bare"], ids=["tagged repo", "no remote"])
@pytest.mark.asyncio
async def test_rows_in_the_repositorys_own_namespace_carry_no_tag(monkeypatch, repo: str) -> None:
    """A document in the trunk: --into source writes to the trunk, served there untagged,
    whether or not the repository has a remote to tag rows with."""
    world = _World(monkeypatch, [_candidate()])
    _link_repo(monkeypatch, world, code_namespaces=frozenset({TRUNK}))
    await de.extract_document("o", TRUNK, "doc.md", dry_run=False, repo=repo, into="source")
    (inp,) = world.recorded
    assert inp.namespace == TRUNK and not [t for t in inp.tags if t.startswith("repo:")]


@pytest.mark.asyncio
async def test_another_writers_unfrozen_row_is_not_tagged(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), agent="claude-code", files=[HEADER])
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets")
    assert world.updates == [] and world.recorded == []
    assert [r.action for r in report.records] == ["not_ours"]


@pytest.mark.asyncio
async def test_a_flagged_row_found_gone_gains_the_repo_tag(monkeypatch, capsys) -> None:
    world = _World(monkeypatch, [])
    world.record(
        "decision:gone",
        {"reasoning": "gone from the deck"},
        pinned=True,
        tags=["decision", SOURCE_CHANGED],
    )
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets")
    (upd,) = world.updates
    assert set(upd.tags) == {"decision", SOURCE_CHANGED, REPO_TAG}
    assert report.linked == ["decision:gone"] and report.flagged == []
    cli.print_report(report)
    assert "- tagged decision:gone with the repository\n" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_run_without_repo_reads_the_registry_for_a_branchs_trunk(monkeypatch) -> None:
    """A repository added from the dashboard is known only once the snapshot is read: the
    run reads it before looking up the trunk of the document's branch namespace."""
    from server.code.app.index import repo_registry
    from server.code.domain.index.repos import set_registry_repos
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    entries = [
        repo_config(
            namespace=ns,
            repo="acme/widgets",
            git_remote="https://github.com/acme/widgets.git",
            git_branch=branch,
            source="registry",
        )
        for ns, branch in ((TRUNK, "main"), ("ns", "release-26.1"))
    ]

    async def refresh(**kw):
        set_registry_repos(entries)
        return entries

    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), namespace=TRUNK, files=[HEADER])
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repo_registry, "refresh_db_repos", refresh)
    set_registry_repos([])
    try:
        report = await _run()
    finally:
        set_registry_repos([])
    assert report.error == (
        f"this document's decisions are in o/{TRUNK}; run as the run that wrote "
        "them (with no --repo, or its --repo and --into), or delete them there first"
    )
    assert world.prompts == []


@pytest.mark.asyncio
async def test_a_run_refuses_when_the_dashboards_repositories_could_not_be_read(
    monkeypatch,
) -> None:
    from server.code.app.index import repo_registry

    world = _World(monkeypatch, [_candidate()])
    monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
    report = await _run()
    assert report.error == "could not read the repositories added from the dashboard; try again"
    assert world.prompts == []


@pytest.mark.asyncio
async def test_a_linked_repository_the_caller_may_not_read_stays_a_tag(monkeypatch) -> None:
    from server.code.app import repo_scope

    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER], tags=["decision", REPO_TAG])
    _link_repo(monkeypatch, world, refused=frozenset({(TRUNK, "viewer")}))
    monkeypatch.setattr(repo_scope, "repo_names", lambda org: {REPO_TAG: ("acme/widgets", TRUNK)})
    report = await _run()
    assert report.error.startswith(
        f"this document's decisions are linked to a repository ({REPO_TAG})"
    )


@pytest.mark.asyncio
async def test_another_writers_flagged_row_is_left_as_it_is(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        _same_fields(c),
        agent="claude-code",
        files=[HEADER],
        tags=["decision", SOURCE_CHANGED],
    )
    _link_repo(monkeypatch, world)
    report = await _run(repo="acme/widgets")
    # The support check reads only the extractor's own rows: another writer's is not judged.
    assert world.updates == [] and [r.action for r in report.records] == ["not_ours"]


@pytest.mark.asyncio
async def test_a_run_naming_another_repo_checks_the_branchs_own_trunk(monkeypatch) -> None:
    """A document in a branch namespace whose rows went to that repository's trunk: a run
    naming another repository sees them there."""
    from server.code.app import repo_scope

    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), namespace=TRUNK, files=[HEADER])
    _link_repo(monkeypatch, world, code_namespaces=frozenset({"ns"}))
    monkeypatch.setattr(repo_scope, "trunk_namespace_of", lambda org, ns: TRUNK)
    report = await _run(repo="acme/gadgets")
    assert report.error == (
        f"this document's decisions are in o/{TRUNK}; run as the run that wrote "
        "them (with no --repo, or its --repo and --into), or delete them there first"
    )
    assert world.prompts == [] and world.recorded == []


@pytest.mark.asyncio
async def test_into_source_refuses_a_document_in_another_repositorys_namespace(
    monkeypatch,
) -> None:
    from server.code.app import repo_scope

    world = _World(monkeypatch, [_candidate()])
    _link_repo(monkeypatch, world, code_namespaces=frozenset({"ns"}))
    # "ns" is a code namespace of another repository, whose trunk is code-gadgets.
    monkeypatch.setattr(repo_scope, "trunk_namespace_of", lambda org, ns: "code-gadgets")
    report = await _run(repo="acme/widgets", into="source")
    assert report.error == (
        "the document is in another repository's code namespace; use --into repo"
    )
    assert world.prompts == []
    # Into the named repository's trunk, it runs.
    assert (await _run(repo="acme/widgets", into="repo")).error == ""


@pytest.mark.asyncio
async def test_a_caller_who_cannot_read_the_documents_namespace_learns_nothing_of_it(
    monkeypatch,
) -> None:
    """Refused before anything the namespace is (a code namespace, another repository's)
    shapes the answer."""
    from server.code.app import repo_scope

    world = _World(monkeypatch, [_candidate()])
    _link_repo(
        monkeypatch, world, refused=frozenset({("ns", "viewer")}), code_namespaces=frozenset({"ns"})
    )
    monkeypatch.setattr(repo_scope, "trunk_namespace_of", lambda org, ns: "code-gadgets")
    for into in (None, "source", "repo"):
        report = await _run(repo="acme/widgets", into=into)
        assert report.error == "not allowed to read o/ns"


@pytest.mark.asyncio
async def test_into_source_on_a_branch_of_the_same_repository_stays_tagged_there(
    monkeypatch,
) -> None:
    """Through the real registry: a document in a release branch's namespace of the named
    repository keeps its rows there, tagged, with its names resolved in the trunk."""
    from server.code.app import symbol as code_symbol
    from server.code.domain.index.remote import repo_tag
    from server.code.domain.index.repos import set_registry_repos
    from server.settings import settings
    from tests.unit.code_fixtures import repo_config

    remote = "https://github.com/acme/widgets.git"
    entries = [
        repo_config(
            namespace=ns, repo="acme/widgets", git_remote=remote, git_branch=b, source="registry"
        )
        for ns, b in ((TRUNK, "main"), ("ns", "release-26.1"))
    ]
    world = _World(monkeypatch, [_candidate()])
    looked_up: list[str] = []
    symbol = code_symbol.handle_code_symbol

    async def code_symbol_handler(inp):
        looked_up.append(inp.namespace)
        return await symbol(inp)

    monkeypatch.setattr(code_symbol, "handle_code_symbol", code_symbol_handler)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    set_registry_repos(entries)
    try:
        report = await _run(repo="acme/widgets", into="source")
    finally:
        set_registry_repos([])
    assert report.error == ""
    (inp,) = world.recorded
    assert inp.namespace == "ns" and repo_tag(remote) in inp.tags
    assert looked_up == [TRUNK]


@pytest.mark.asyncio
async def test_a_name_only_a_test_or_a_page_defines_links_nothing(monkeypatch) -> None:
    c = _candidate(
        mentions=[
            "memory_ttl",
            "ExternalDistinctTransform",
            "FakeSpiller",
            "fake_store",
            "page_and_test",
            "OnePageName",
        ]
    )
    world = _World(monkeypatch, [c])
    from server.knowledge.app import document_reads

    body = BODY + " `memory_ttl` caps it. FakeSpiller, `fake_store` and `page_and_test` stand in."
    body += " OnePageName is a heading."

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, body, ("Spilling", "Final DISTINCT"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    # Two pages, no code: no file to pick, still a setting.
    world.symbols["memory_ttl"] = [
        ("docs/reference/configuration.md", "memory_ttl"),
        ("docs/features/memory.md", "memory_ttl"),
    ]
    world.symbols["FakeSpiller"] = [("tests/unit/fakes.py", "FakeSpiller")]
    world.symbols["fake_store"] = [("tests/unit/fakes.py", "fake_store")]
    # One page, no code: no file.
    world.symbols["OnePageName"] = [("docs/reference/configuration.md", "OnePageName")]
    # A page and a test (a fixture named after the setting): the page makes it a setting.
    world.symbols["page_and_test"] = [
        ("docs/features/memory.md", "page_and_test"),
        ("tests/unit/fakes.py", "page_and_test"),
    ]
    # A name in a test and in one code file stays ambiguous: dropping the test makes no link.
    world.symbols["ExternalDistinctTransform"] = [
        ("tests/unit/fake_transform.py", "ExternalDistinctTransform"),
        (HEADER, "DB::ExternalDistinctTransform"),
    ]
    report = await _run(dry_run=True)
    (rec,) = report.records
    assert sorted(rec.signals) == [
        "ExternalDistinctTransform: camelcase, no single definition",
        "FakeSpiller: camelcase, only a test or a page defines it",
        "OnePageName: camelcase, only a test or a page defines it",
        "fake_store: qualified, only a test or a page defines it",
        "memory_ttl: qualified, only a test or a page defines it; reported as a setting",
        "page_and_test: qualified, only a test or a page defines it; reported as a setting",
    ]
    # A test's snake_case name is a helper, not a setting; the sorted list is the whole list.
    assert sorted(rec.unresolved) == [
        "ExternalDistinctTransform (several definitions)",
        "FakeSpiller (only tests or pages)",
        "OnePageName (only tests or pages)",
        "memory_ttl (setting)",
        "page_and_test (setting)",
    ]
    assert rec.body["files"] == []


@pytest.mark.asyncio
async def test_a_test_class_beside_a_code_constructor_links_nothing(monkeypatch) -> None:
    c = _candidate(mentions=["ExternalDistinctTransform"])
    world = _World(monkeypatch, [c])
    # The class only in a test, its constructor in code: the class-first pick would link the test.
    world.symbols["ExternalDistinctTransform"] = [
        ("tests/fake_transform.h", "DB::ExternalDistinctTransform"),
        (
            "src/Processors/ExternalDistinctTransform.cpp",
            "DB::ExternalDistinctTransform::ExternalDistinctTransform",
        ),
    ]
    report = await _run(dry_run=True)
    (rec,) = report.records
    assert rec.body["files"] == []
    assert "ExternalDistinctTransform (several definitions)" in rec.unresolved


@pytest.mark.parametrize(
    ("stage", "read"),
    [
        ("decided", "decided"),
        ("Decided", "decided"),
        ("proposed", "proposed"),
        ("planned", "proposed"),
        (None, "proposed"),
    ],
)
def test_only_a_plain_decided_reads_as_decided(stage: Any, read: str) -> None:
    c = _candidate(stage=stage)
    if stage is None:
        del c["stage"]
    (cand,) = dx.parse_candidates(json.dumps({"decisions": [c]})) or []
    assert cand.stage == read


def test_the_window_opens_with_the_documents_title() -> None:
    w = Window([CorpusChunk("d", 0, "We use a bucket per spill.", ("Plan",))])
    assert dx.user_prompt(w, "RFC:  spill\nthe join").startswith("[document: RFC: spill the join]")
    assert dx.user_prompt(w, "").startswith("[section: Plan]")
    assert dx.user_prompt(w, "t" * 500).startswith(f"[document: {'t' * dx.TITLE_CHARS}]\n")


@pytest.mark.parametrize(
    ("read_as", "kept", "tags"),
    [("decided", "proposed", ["decision:proposed"]), ("proposed", "decided", [])],
)
@pytest.mark.asyncio
async def test_the_same_words_keep_the_rows_stage(
    monkeypatch, read_as: str, kept: str, tags: list[str]
) -> None:
    # The stage is the model's reading: a stage taken from one run would drift on re-runs.
    c = _candidate(stage=read_as)
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER], tags=["decision", *tags])
    report = await _run()
    (rec,) = report.records
    assert rec.action == "unchanged" and world.recorded == [] and rec.body["stage"] == kept
    assert rec.note == f"the document reads as {read_as}; the row keeps its stage {kept}"


@pytest.mark.asyncio
async def test_every_read_of_a_window_names_the_document(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.title = "RFC: final DISTINCT"
    await _run()
    assert world.prompts and all(
        "[document: RFC: final DISTINCT]" in user for _, user in world.prompts
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize(("stage", "tags"), [("decided", ["decision:proposed"]), ("proposed", [])])
async def test_the_stage_of_a_decision_with_a_recorded_outcome_is_not_compared(
    monkeypatch, stage: str, tags: list[str], pinned: bool
) -> None:
    # Acted on: decided to every reader, whatever its tag or the document's wording says.
    c = _candidate(stage=stage)
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "realized_outcome": "built", "success": True},
        files=[HEADER],
        tags=["decision", *tags, "decision:outcome-known"],
        pinned=pinned,
    )
    report = await _run()
    (rec,) = report.records
    assert rec.action == "unchanged" and rec.note == ""
    assert rec.body["stage"] == "decided"  # acted on
    assert world.recorded == [] and world.updates == []


@pytest.mark.asyncio
async def test_a_files_change_keeps_a_proposed_rows_stage_against_a_decided_reading(
    monkeypatch,
) -> None:
    from server.memory.domain.decision_schema import PROPOSED_TAG

    c = _candidate(stage="decided")
    world = _World(monkeypatch, [c])
    # The row lacks the file this run resolves: revised, but the stage stays proposed.
    world.record(_key(c), _same_fields(c), files=[], tags=["decision", PROPOSED_TAG])
    report = await _run()
    (rec,) = report.records
    assert rec.action == "revised" and world.recorded[-1].stage == "proposed"
    assert rec.body["stage"] == "proposed" and rec.body["files"] == [HEADER]
    assert rec.note == "the document reads as decided; the row keeps its stage proposed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_as", "kept", "tags"),
    [("decided", "proposed", ["decision:proposed"]), ("proposed", "decided", [])],
)
async def test_a_frozen_row_keeps_its_stage_and_is_not_flagged(
    monkeypatch, read_as: str, kept: str, tags: list[str]
) -> None:
    c = _candidate(stage=read_as)
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER], tags=["decision", *tags], pinned=True)
    report = await _run()
    (rec,) = report.records
    assert rec.action == "unchanged" and report.flagged == []
    assert world.recorded == [] and world.updates == []
    assert rec.note == f"the document reads as {read_as}; the row keeps its stage {kept}"


@pytest.mark.parametrize(
    ("read_as", "kept", "tags"),
    [("decided", "proposed", ["decision:proposed"]), ("proposed", "decided", [])],
)
@pytest.mark.asyncio
async def test_another_writers_row_keeps_its_stage(
    monkeypatch, read_as: str, kept: str, tags: list[str]
) -> None:
    c = _candidate(stage=read_as)
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER], tags=["decision", *tags], agent="claude")
    report = await _run()
    assert report.records[0].action == "not_ours"
    assert world.recorded == [] and world.updates == []
    assert report.records[0].body["stage"] == read_as


@pytest.mark.asyncio
async def test_a_changed_text_is_recorded_at_the_stage_it_reads_as(monkeypatch) -> None:
    from server.memory.domain.decision_schema import PROPOSED_TAG

    c = _candidate(stage="decided")
    world = _World(monkeypatch, [c])
    old = {**_same_fields(c), "reasoning": "We spill like ORDER BY."}
    world.record(_key(c), old, files=[HEADER], tags=["decision", PROPOSED_TAG])
    report = await _run()
    (rec,) = report.records
    assert rec.action == "revised" and world.recorded[-1].stage == "decided" and rec.note == ""


@pytest.mark.asyncio
async def test_a_writer_record_whose_row_is_deleted_between_the_reads_is_a_race(
    monkeypatch,
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    # The record's writer has no row (deleted between the reads); another identity's row
    # holds the key.
    writer = WriterRecord(de.EXTRACTOR_AGENT, "-", "-", json.dumps(_same_fields(c)), None)  # type: ignore[arg-type]
    world.records[_key(c)] = (writer, _same_fields(c))
    world.add(_key(c), agent="claude")
    report = await _run()
    assert report.records == [] and world.recorded == [] and world.deletes == []
    assert report.races == [f"{_key(c)}: deleted since the run read it"]


@pytest.mark.asyncio
async def test_a_proposed_candidate_looks_for_a_restatement_worded_as_proposed(monkeypatch) -> None:
    world = _World(monkeypatch, [_candidate(stage="proposed")])
    await _run(dry_run=True)
    assert (
        world.search_queries
        and " Proposed spill into sorted runs because" in world.search_queries[0]
    )


# ------------------------------------------------------------------ support check


@pytest.mark.asyncio
async def test_the_support_check_drops_an_unsupported_outcome_and_unsupported_claims(
    monkeypatch,
) -> None:
    keep = _candidate()
    drop = _candidate(scenario="another decision", outcome="keep a hash set")
    world = _World(monkeypatch, [keep, drop])
    world.unsupported = {"0.r0", "1.o"}
    report = await _run()
    (inp,) = world.recorded
    # The kept decision lost its unsupported reason: it is recorded with none.
    assert inp.outcome == "spill into sorted runs" and inp.reasoning == inp.outcome
    assert inp.alternatives == ["a plain set on disk — rejected: costs random reads"]
    assert [d.reason for d in report.dropped] == [
        "reason 'a run merges like ORDER BY' left out: the support check found it not stated",
        dx.UNSUPPORTED,
    ]
    claims = world.support_prompts[0].split("[candidates]", 1)[1]
    assert (
        '"id": "1.o"' in claims
        and '"quotes": ["We spill into sorted runs like ORDER BY."]' in claims
    )


@pytest.mark.asyncio
async def test_a_lost_support_check_keeps_nothing_of_the_window_and_retires_nothing(
    monkeypatch,
) -> None:
    world = _World(monkeypatch, [_candidate()])
    world.record("decision:gone", {"reasoning": "gone from the deck"})
    world.support_replies = [httpx.ConnectError("down"), "not json"]
    report = await _run()
    assert world.recorded == [] and world.deletes == [] and report.retired == []
    reasons = [d.reason for d in report.dropped if d.refused]
    assert reasons == ["the support check failed: ConnectError"]
    assert cli.exit_code(report) == 2
    report = await _run()
    assert world.recorded == [] and world.deletes == []
    assert [d.reason for d in report.dropped if d.refused] == [
        "support check: the model's reply is not the JSON asked for"
    ]


@pytest.mark.asyncio
async def test_a_row_is_retired_only_when_a_second_check_finds_it_gone_too(monkeypatch) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:old", {"reasoning": "We spill into sorted runs like ORDER BY."})
    gone = json.dumps(
        {"claims": [], "stored": [{"id": "decision:old", "still_stated": False, "same_as": None}]}
    )
    still = gone.replace("false", "true")
    # The first check reads it gone, the second still stated: it stays.
    world.support_replies = [gone, still]
    report = await _run()
    assert report.retired == [] and world.deletes == []
    assert len(world.support_prompts) == 2 and "[stored decisions]" in world.support_prompts[1]
    # Gone on both checks: retired.
    world.support_replies = [gone, gone]
    report = await _run()
    assert report.retired == ["decision:old"]


def test_a_support_reply_is_read_by_id_and_a_missing_verdict_is_no() -> None:
    asked = {f"decision:{c}" for c in "abcdefg"}
    assert dx.parse_support("no json", asked) is None and dx.parse_support("[]", asked) is None
    reply = {
        "claims": [{"id": "0.o", "supported": True}, {"id": "0.r0", "supported": "yes"}],
        "stored": [
            {"id": "decision:a", "still_stated": True, "same_as": 0},
            {"id": "decision:b", "still_stated": False, "same_as": "2"},
            # Anything but true or false is no verdict, never a "gone".
            {"id": "decision:c", "still_stated": 1},
            {"id": "decision:d", "still_stated": "false"},
            {"id": "decision:e"},
            # A digit int() cannot read names no restatement.
            {"id": "decision:f", "still_stated": True, "same_as": "\u00b2"},
            {"id": "decision:g", "still_stated": True, "same_as": "9" * 4400},
            "junk",
        ],
    }
    v = dx.parse_support(json.dumps(reply), asked)
    # An id the check was not asked about is ignored.
    only_a = dx.parse_support(json.dumps(reply), {"decision:a"})
    assert only_a is not None and only_a.stored == {"decision:a": (True, 0)}
    assert v is not None and v.supported == {"0.o"}
    assert v.stored == {
        "decision:a": (True, 0),
        "decision:b": (False, 2),
        "decision:f": (True, None),
        "decision:g": (True, None),
    }
    cand, notes = dx.supported(1, _cand(), v)
    assert cand is None and notes == [dx.UNSUPPORTED]


@pytest.mark.asyncio
async def test_a_second_check_that_finds_a_row_stated_keeps_its_restatement_unwritten(
    monkeypatch,
) -> None:
    first = _candidate()
    second = _candidate(scenario="spilling the final DISTINCT")
    world = _World(monkeypatch, [second])
    world.record(_key(first), _same_fields(first), files=[HEADER])
    gone = json.dumps(
        {
            "claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")],
            "stored": [{"id": _key(first), "still_stated": False, "same_as": 0}],
        }
    )
    still = json.dumps(
        {"claims": [], "stored": [{"id": _key(first), "still_stated": True, "same_as": None}]}
    )
    world.support_replies = [gone, still]
    report = await _run()
    # The first check named the candidate as its restatement; the second kept the row:
    # one live row, the stored one.
    assert world.recorded == [] and world.deletes == [] and report.retired == []
    assert f"restates {_key(first)}, not found gone" in [d.reason for d in report.dropped]


@pytest.mark.asyncio
@pytest.mark.parametrize("again", [False, True])
async def test_a_restatement_under_a_gone_or_unjudged_rows_key_does_not_rewrite_it(
    monkeypatch, again: bool
) -> None:
    stated = _candidate()
    gone = _candidate(scenario="spilling the final DISTINCT", outcome="keep it in memory")
    cand = _candidate(scenario=gone["scenario"])
    world = _World(monkeypatch, [cand])
    world.record(_key(stated), _same_fields(stated), files=[HEADER])
    world.record(_key(gone), _same_fields(gone), files=[HEADER])
    first = {
        "claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")],
        "stored": [
            {"id": _key(stated), "still_stated": True, "same_as": 0},
            {"id": _key(gone), "still_stated": False, "same_as": None},
        ],
    }
    second = {"claims": [], "stored": [{"id": _key(gone), "still_stated": again}]}
    world.support_replies = [json.dumps(first), json.dumps(second)]
    report = await _run()
    # The candidate says what the stated row says: the row under its key is not
    # rewritten as a second copy of the stated one. Found gone twice, it is retired; a
    # split between the checks is no verdict, and it keeps its stored words.
    assert world.recorded == [] and world.updates == []
    assert f"restates {_key(stated)}, not found gone" in [d.reason for d in report.dropped]
    retired = [] if again else [_key(gone)]
    assert report.retired == retired and len(world.deletes) == len(retired)


@pytest.mark.asyncio
async def test_an_unjudged_rows_candidate_restating_a_gone_row_replaces_it(monkeypatch) -> None:
    gone = _candidate()
    unjudged = _candidate(scenario="spilling the final DISTINCT", outcome="keep it in memory")
    cand = _candidate(scenario=unjudged["scenario"])
    world = _World(monkeypatch, [cand])
    world.record(_key(gone), _same_fields(gone), files=[HEADER])
    world.record(_key(unjudged), _same_fields(unjudged), files=[HEADER])
    # The check leaves the row under the candidate's key out (no verdict) and finds the
    # one the candidate restates gone, twice.
    first = {
        "claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")],
        "stored": [{"id": _key(gone), "still_stated": False, "same_as": 0}],
    }
    second = {"claims": [], "stored": [{"id": _key(gone), "still_stated": False}]}
    world.support_replies = [json.dumps(first), json.dumps(second)]
    report = await _run()
    (rec,) = world.recorded
    assert rec.scenario == unjudged["scenario"] and rec.outcome == cand["outcome"]
    assert rec.outcome != unjudged["outcome"]
    assert report.replaced == [f"{_key(gone)} -> {_key(unjudged)}"]
    assert len(world.deletes) == 1
    assert not any(d.reason.startswith("restates") for d in report.dropped)


@pytest.mark.asyncio
async def test_a_still_stated_row_keeps_its_words_when_a_revision_adds_a_reason(
    monkeypatch,
) -> None:
    c = _candidate()
    extra = {"text": "the runs fit the merge buffer", "evidence": c["evidence"]}
    revised = _candidate(reasons=[*c["reasons"], extra])
    world = _World(monkeypatch, [revised])
    world.record(_key(c), _same_fields(c), files=[HEADER])
    world.support_replies = [
        json.dumps(
            {
                "claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.r1", "0.x0")],
                "stored": [{"id": _key(c), "still_stated": True, "same_as": 0}],
            }
        )
    ]
    report = await _run()
    # The documented limit: the stored row still holds, so it keeps its one reason.
    assert world.recorded == [] and world.updates == [] and world.deletes == []
    assert report.retired == [] and report.flagged == []


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [{"pinned": True}, {"realized_outcome": "built"}])
async def test_a_frozen_or_acted_on_row_the_check_left_out_is_left_as_it_is(
    monkeypatch, frozen
) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    fields = {**_same_fields(c), "reasoning": "We keep a hash set."}
    row = {"pinned": True} if "pinned" in frozen else {}
    world.record(
        _key(c),
        {**fields, **{k: v for k, v in frozen.items() if k != "pinned"}},
        files=[HEADER],
        **row,
    )
    # The reply judges the claims but leaves the stored row out: no verdict.
    world.support_replies = [
        json.dumps({"claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")]})
    ]
    report = await _run()
    (rec,) = report.records
    assert rec.action == "unchanged" and world.updates == [] and world.recorded == []
    assert report.flagged == [] and world.deletes == []
    assert rec.note == "the support check gave no verdict on the row; left as it is"


@pytest.mark.asyncio
async def test_a_cut_support_reply_names_its_own_cap(monkeypatch) -> None:
    from server.settings.budgets import llm_max_tokens

    world = _World(monkeypatch, [_candidate()])

    class _CutCheck:
        async def complete(self, system: str, user: str, **k: Any) -> Any:
            if system.startswith(dx.SUPPORT_SYSTEM):
                return SimpleNamespace(text='{"claims": [', stop_reason="length")
            return SimpleNamespace(text=json.dumps({"decisions": world.reply}), stop_reason="")

    from server.llm import client as llm_client

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _CutCheck())
    report = await _run()
    cap = llm_max_tokens("decision_support")
    assert [d.reason for d in report.dropped if d.refused] == [
        f"support check: reply cut at the token limit ({cap}); lower --window-chars "
        "(LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS) or --max-per-section, or raise "
        "decision_support in LOOM_LLM_MAX_TOKENS_OVERRIDES"
    ]
    assert world.recorded == []


@pytest.mark.asyncio
async def test_the_read_and_the_support_check_run_under_their_own_stages(monkeypatch) -> None:
    from server.llm import client as llm_client
    from server.observability import genai

    _World(monkeypatch, [_candidate()])
    inner = llm_client.get_llm_client()
    stages: list[tuple[bool, str]] = []

    class _Staged:
        async def complete(self, system: str, user: str, **k: Any) -> Any:
            stages.append((system.startswith(dx.SUPPORT_SYSTEM), genai.current_stage()))
            return await inner.complete(system, user, **k)

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Staged())
    await _run()
    assert stages == [(False, "decision_extract"), (True, "decision_support")]


def test_only_the_statement_verb_is_left_out_of_an_outcome() -> None:
    assert _cand(outcome="Proposed decisions are hidden").outcome == "Proposed decisions are hidden"
    assert _cand(outcome="Decided spill").outcome == "Decided spill"
    assert _cand(outcome="decided to spill").outcome == "spill"


def _stored(key: str, still: bool | None) -> str:
    """A support reply with no claims and one verdict on `key` (None: the key left out)."""
    stored = [] if still is None else [{"id": key, "still_stated": still, "same_as": None}]
    return json.dumps({"claims": [], "stored": stored})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replies", "retired"),
    [
        # One window states it: stated, no second check.
        ([True, False], False),
        # One window leaves it out: not judged.
        ([None, False], False),
        # Both say gone; the second check leaves it out in one window: not judged.
        ([False, False, False, None], False),
        # Both say gone, twice: no longer stated.
        ([False, False, False, False], True),
    ],
)
async def test_a_row_is_gone_only_when_every_window_says_so_twice(
    monkeypatch, replies: list[bool | None], retired: bool
) -> None:
    world = _World(monkeypatch, [])
    world.record("decision:old", {"reasoning": "gone from the deck"})
    _chunks(
        monkeypatch,
        ("We spill into sorted runs like ORDER BY.", ("Spilling", "One")),
        ("A plain set on disk costs random reads.", ("Spilling", "Two")),
    )
    world.support_replies = [_stored("decision:old", v) for v in replies]
    report = await _run(window_chars=60)
    assert report.windows == 2
    assert len(world.support_prompts) == len(replies)
    assert report.retired == (["decision:old"] if retired else [])
    assert world.updates == [] and report.flagged == []


@pytest.mark.asyncio
async def test_a_lost_second_check_never_clears_source_changed(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set."},
        pinned=True,
        tags=["decision", SOURCE_CHANGED],
    )
    claims = [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")]
    gone = json.dumps(
        {"claims": claims, "stored": [{"id": _key(c), "still_stated": False, "same_as": None}]}
    )
    world.support_replies = [gone, httpx.ConnectError("down")]
    report = await _run()
    # Gone once, then not judged: the pinned row keeps its tag and is not re-flagged.
    assert world.updates == [] and world.recorded == []
    (rec,) = report.records
    assert rec.action == "unchanged"
    assert rec.note == "the support check gave no verdict on the row; left as it is"


@pytest.mark.asyncio
async def test_a_document_with_no_window_read_retires_nothing(monkeypatch) -> None:
    from server.knowledge.app import document_reads

    world = _World(monkeypatch, [])
    world.record("decision:old", {"reasoning": "gone from the deck"})
    world.record("decision:pinned", {"reasoning": "gone too"}, pinned=True)

    async def chunks(org, ns, uri):
        return [CorpusChunk(uri, 0, "![diagram](flow.png)\n\nFigure 1.", ("Spilling", "Flow"))]

    monkeypatch.setattr(document_reads, "document_chunks", chunks)
    world.content = "# Spilling\n\n## Flow\n\n![diagram](flow.png)\n\nFigure 1.\n"
    report = await _run()
    assert report.windows == 0 and world.prompts == [] and world.support_prompts == []
    assert report.retired == [] and report.flagged == [] and world.deletes == world.updates == []


@pytest.mark.asyncio
async def test_a_row_past_the_known_cap_is_never_judged_retired_or_flagged(monkeypatch) -> None:
    from server.settings import settings

    world = _World(monkeypatch, [])
    world.record("decision:a", {"reasoning": "gone from the deck"})
    world.record("decision:b", {"reasoning": "gone too"}, pinned=True)
    monkeypatch.setattr(settings, "memory_decision_extract_known_max", 0)
    report = await _run()
    assert world.support_prompts == []
    assert report.retired == [] and report.flagged == [] and world.deletes == world.updates == []


@pytest.mark.asyncio
async def test_an_unfrozen_row_with_no_verdict_is_revised_only_for_other_words(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(_key(c), _same_fields(c), files=[HEADER])
    no_verdict = json.dumps(
        {"claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")]}
    )
    world.support_replies = [no_verdict]
    report = await _run()
    assert [r.action for r in report.records] == ["unchanged"] and world.recorded == []
    # Other words under the key: recorded as the text now reads.
    world.record(_key(c), {**_same_fields(c), "reasoning": "We keep a hash set."}, files=[HEADER])
    world.support_replies = [no_verdict]
    report = await _run()
    assert [r.action for r in report.records] == ["revised"] and len(world.recorded) == 1


def test_the_check_reads_a_stored_decision_without_its_verb_or_realized_outcome() -> None:
    record = {
        "scenario": "how the final DISTINCT spills",
        "outcome": "spill into sorted runs",
        "reasoning": "a run merges like ORDER BY",
        "alternatives": "a plain set on disk — rejected: costs random reads",
        "realized_outcome": "built and measured",
    }
    assert dx.stored_statement(record) == (
        "how the final DISTINCT spills. spill into sorted runs because a run merges like "
        "ORDER BY. Rejected: a plain set on disk — costs random reads"
    )
    assert "Decided" not in dx.stored_statement(record)
    assert dx.stored_statement({**record, "reasoning": record["outcome"]}).count("because") == 0


@pytest.mark.asyncio
async def test_a_wrapped_quote_names_its_own_section_not_the_windows_first(monkeypatch) -> None:
    summary = "In short, we spill into sorted runs like ORDER BY."
    wrapped = "One merge pass reads\neach run once, and a plain set on disk costs random reads."
    c = _candidate(
        outcome="one merge pass",
        evidence=["One merge pass reads each run once"],
        reasons=[
            {"text": "random reads cost", "evidence": ["a plain set on disk costs random reads"]}
        ],
        rejected=[],
    )
    recap = _candidate(
        scenario="the recap", outcome="spill", evidence=["In short, we spill into"], rejected=[]
    )
    world = _World(monkeypatch, [c, recap])
    _chunks(monkeypatch, (summary, ("Spilling", "Summary")), (wrapped, ("Spilling", "Merge")))
    report = await _run()
    (inp,) = world.recorded
    assert inp.source_resource_id.endswith("#Spilling > Merge")
    assert [(d.section, d.reason) for d in report.dropped] == [("Spilling > Summary", dx.RECAP)]


@pytest.mark.asyncio
async def test_a_restatement_naming_a_dropped_duplicate_defers_through_the_kept_one(
    monkeypatch,
) -> None:
    first = _candidate()
    kept = _candidate(scenario="spilling the final DISTINCT")
    # The same outcome under a third key: dropped as the same decision as `kept`.
    twin = _candidate(scenario="the final DISTINCT spills")
    world = _World(monkeypatch, [kept, twin])
    world.record(_key(first), _same_fields(first), files=[HEADER])
    world.same_as = {_key(first): 1}  # the check names the dropped twin
    report = await _run()
    # The stored row stands, and the kept candidate is not written beside it.
    assert world.recorded == [] and world.deletes == []
    reasons = [d.reason for d in report.dropped]
    assert "same decision as an earlier candidate" in reasons
    assert f"restates {_key(first)}, not found gone" in reasons


@pytest.mark.asyncio
async def test_a_pinned_row_with_no_verdict_the_read_emits_gains_the_repo_tag(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c), _same_fields(c), files=[HEADER], pinned=True, tags=["decision", SOURCE_CHANGED]
    )
    _link_repo(monkeypatch, world)
    world.support_replies = [
        json.dumps({"claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")]})
    ]
    report = await _run(repo="acme/widgets")
    (upd,) = world.updates
    # Linked to the repository, its source-changed tag kept: no verdict clears nothing.
    assert set(upd.tags) == {"decision", SOURCE_CHANGED, REPO_TAG}
    assert report.records[0].action == "unchanged" and world.recorded == []


@pytest.mark.asyncio
async def test_an_unjudged_row_with_the_same_words_and_new_row_fields_keeps_its_words(
    monkeypatch,
) -> None:
    from server.memory.domain.decision_schema import PROPOSED_TAG

    c = _candidate(stage="decided", outcome="SPILL into sorted runs")
    world = _World(monkeypatch, [c])
    # Stored as proposed, in other case, before its file resolved.
    world.record(
        _key(c),
        {**_same_fields(c), "outcome": "spill into sorted runs"},
        files=[],
        tags=["decision", PROPOSED_TAG],
    )
    world.support_replies = [
        json.dumps({"claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")]})
    ]
    report = await _run()
    (rec,) = report.records
    assert rec.action == "revised"
    (inp,) = world.recorded
    # Recorded again for its files only: its stage and words are the stored ones.
    assert inp.files == [HEADER] and inp.stage == "proposed"
    assert inp.outcome == "spill into sorted runs"


def test_a_long_rejected_option_cut_as_stored_is_the_same_words() -> None:
    long = "x" * 400
    c = _cand(rejected=[{"option": "a plain set on disk", "reason": long, "evidence": ["q"]}])
    inp = de._record_input(
        "o",
        SimpleNamespace(row_tag="", rows_namespace="ns", doc_namespace="ns"),
        "doc.md",
        c,
        SECTION,
        [],
    )
    record = {
        "category": inp.category,
        "scenario": inp.scenario,
        "outcome": inp.outcome,
        "reasoning": inp.reasoning,
        "alternatives": "\n".join(svc.clean_alternatives(inp.alternatives, cut=True)),
    }
    assert de._same_words(record, inp)


@pytest.mark.asyncio
async def test_a_window_without_a_rows_text_names_no_heir_for_it(monkeypatch) -> None:
    first = _candidate()
    other = _candidate(
        scenario="the merge",
        outcome="one merge pass",
        evidence=["A plain set on disk costs random reads."],
        rejected=[],
    )
    world = _World(monkeypatch, [other])
    world.record(_key(first), _same_fields(first), files=[HEADER])
    _chunks(
        monkeypatch,
        ("We spill into sorted runs like ORDER BY.", ("Spilling", "One")),
        ("A plain set on disk costs random reads.", ("Spilling", "Two")),
    )
    claims = [{"id": i, "supported": True} for i in ("0.o", "0.r0")]
    states = {"id": _key(first), "still_stated": True, "same_as": None}
    names = {"id": _key(first), "still_stated": False, "same_as": 0}
    world.support_replies = [
        json.dumps({"claims": [], "stored": [states]}),
        json.dumps({"claims": claims, "stored": [names]}),
    ]
    world.replies = [[], [other]]
    report = await _run(window_chars=60)
    assert report.windows == 2
    # Window two, which lacks the stored row's text, named its own decision as the row's
    # restatement; window one states the row: the decision of window two is written.
    assert [i.outcome for i in world.recorded] == ["one merge pass"]


@pytest.mark.asyncio
async def test_a_restatement_of_a_row_one_window_left_out_is_not_written(monkeypatch) -> None:
    first = _candidate()
    other = _candidate(
        scenario="spilling the final DISTINCT",
        evidence=["A plain set on disk costs random reads."],
        reasons=[],
        rejected=[],
    )
    world = _World(monkeypatch, [other])
    world.record(_key(first), _same_fields(first), files=[HEADER])
    _chunks(
        monkeypatch,
        ("We spill into sorted runs like ORDER BY.", ("Spilling", "One")),
        ("A plain set on disk costs random reads.", ("Spilling", "Two")),
    )
    names = {"id": _key(first), "still_stated": False, "same_as": 0}
    world.support_replies = [
        json.dumps({"claims": [], "stored": []}),
        json.dumps({"claims": [{"id": "0.o", "supported": True}], "stored": [names]}),
    ]
    world.replies = [[], [other]]
    report = await _run(window_chars=60)
    # Window one left the row out, window two found it gone: no verdict, so the stored row
    # stands and the restatement window two named is not written beside it.
    assert world.recorded == [] and world.deletes == [] and report.retired == []
    assert f"restates {_key(first)}, not found gone" in [d.reason for d in report.dropped]


@pytest.mark.asyncio
async def test_two_checks_that_split_give_no_verdict_and_keep_a_flag(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c), _same_fields(c), files=[HEADER], pinned=True, tags=["decision", SOURCE_CHANGED]
    )
    claims = [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")]
    gone = {"id": _key(c), "still_stated": False, "same_as": None}
    world.support_replies = [
        json.dumps({"claims": claims, "stored": [gone]}),
        json.dumps({"claims": [], "stored": [{**gone, "still_stated": True}]}),
    ]
    report = await _run()
    # Gone, then stated: no verdict. The pinned row keeps its flag and is not re-flagged.
    assert world.updates == [] and world.recorded == []
    assert report.records[0].note == "the support check gave no verdict on the row; left as it is"


@pytest.mark.asyncio
async def test_an_unjudged_revise_keeps_source_changed(monkeypatch) -> None:
    c = _candidate()
    world = _World(monkeypatch, [c])
    world.record(
        _key(c),
        {**_same_fields(c), "reasoning": "We keep a hash set."},
        files=[HEADER],
        tags=["decision", SOURCE_CHANGED],
    )
    world.support_replies = [
        json.dumps({"claims": [{"id": i, "supported": True} for i in ("0.o", "0.r0", "0.x0")]})
    ]
    report = await _run()
    assert [r.action for r in report.records] == ["revised"]
    (inp,) = world.recorded
    assert SOURCE_CHANGED in inp.tags
