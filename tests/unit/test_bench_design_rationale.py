"""The design-rationale bench without a server or a provider: the task-set loader, the
detectors and citation parser, the scoring and statistics, the spend cap, and the
synthetic example set end to end over a fake Loom and a fake model."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from datetime import date
from pathlib import Path

import httpx
import pytest

import bench.design_rationale.__main__ as cli
from bench.design_rationale import detectors, llm, prompts, run, score
from bench.design_rationale.__main__ import main
from bench.design_rationale.llm import (
    CHARS_PER_TOKEN_WORST,
    CapReached,
    Ledger,
    Model,
    ProviderComplete,
    Reply,
    cost_usd,
)
from bench.design_rationale.loom import EXTRACT_TIMEOUT_S, PRE_EDIT_BUDGET, Loom
from bench.design_rationale.taskset import Document, TaskSetError, load, parse_key, parse_sample
from server.knowledge.app.writes import _chunk_text
from server.memory.app.decision_extract import _windows
from server.memory.app.for_file import render_block
from server.memory.domain import decision_extract as dx
from server.memory.domain.corpus_synthesis import CorpusChunk
from server.settings import settings
from server.settings.budgets import llm_max_tokens

EXAMPLE = Path(__file__).resolve().parents[2] / "bench" / "design_rationale" / "example"


# ---------------------------------------------------------------- task set


def test_the_example_set_loads_with_its_revision_and_kinds():
    ts = load(EXAMPLE)
    assert {k: len(v) for k, v in ts.tasks.items()} == {"A": 3, "B": 3, "C": 2, "D": 1}
    assert ts.documents["upload-retries-v2"].revision_of == "upload-retries"
    # The key written again after the revision: two writes, the later one current.
    writes = [r for r in ts.gold_writes if r.memory_key == "design/upload/retried-statuses"]
    assert [r.after_revision for r in writes] == [False, True]
    assert ts.gold["design/upload/retried-statuses"].after_revision
    assert ts.task_paths(ts.tasks["B"][0]) == ["src/cache/segmented_lru.py"]


def _copy_example(tmp_path: Path) -> Path:
    root = tmp_path / "set"
    (root / "docs").mkdir(parents=True)
    for p in EXAMPLE.rglob("*"):
        if p.is_file():
            (root / p.relative_to(EXAMPLE)).write_text(p.read_text())
    return root


def test_a_task_naming_an_unknown_document_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    tasks["A"][0]["document"] = "nope"
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="unknown document"):
        load(root)


def test_a_supersession_task_without_stale_claims_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    del tasks["C"][0]["stale_claims"]
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="stale_claims"):
        load(root)


def test_a_gold_pointer_is_read_back_to_its_document_and_section(tmp_path):
    root = _copy_example(tmp_path)
    rows = json.loads((root / "gold_rows.json").read_text())
    rows[0].pop("document")
    rows[0].pop("section")
    rows[0]["source_resource_id"] = (
        "knowledge:other-ns/https://docs.example.com/thumbnail-cache#A > B"
    )
    (root / "gold_rows.json").write_text(json.dumps(rows))
    row = load(root).gold[rows[0]["memory_key"]]
    assert (row.document, row.section) == ("thumbnail-cache", "A > B")


# ---------------------------------------------------------------- detectors

DIFF = """## Plan
x
## Diff
```diff
--- a/src/cache/config.py
+++ b/src/cache/config.py
-thumb_cache_bytes = 512 * MiB
+thumb_cache_bytes = 1024 * MiB
--- a/src/cache/segmented_lru.py
+++ b/src/cache/segmented_lru.py
-        self.probation = Segment()
+        self.items = OrderedDict()
```
"""


def test_the_detectors_read_the_diff_in_the_answer():
    files = detectors.diff_files(detectors.extract_diff(DIFF))
    assert sorted(files) == ["src/cache/config.py", "src/cache/segmented_lru.py"]
    removed = {"type": "diff_removed_regex", "path_glob": "src/cache/*", "pattern": "probation"}
    added = {"type": "diff_added_regex", "path_glob": "src/upload/*", "pattern": "sleep"}
    touched = {"type": "file_touched", "paths": ["src/cache/config.py"]}
    # A changed default, named by its declaration line.
    default = {
        "type": "diff_added_regex",
        "path_glob": "src/cache/config.py",
        "pattern": r"thumb_cache_bytes\s*=(?!\s*512\b)",
    }
    assert detectors.run_detector(default, files) is True
    kept = detectors.diff_files(
        "--- a/src/cache/config.py\n+++ b/src/cache/config.py\n"
        "+thumb_cache_bytes = 512 * MiB  # unchanged, reworded comment\n"
    )
    assert detectors.run_detector(default, kept) is False
    assert detectors.run_detector(removed, files) is True
    assert detectors.run_detector(added, files) is False
    assert detectors.run_detector(touched, files) is True
    assert detectors.run_detector({"type": "judge", "rubric": "r"}, files) is None


def test_citations_of_each_kind_are_found_once():
    uri = "https://docs.example.com/upload-retries"
    text = (
        f"- because jitter [design/upload/backoff-full-jitter] "
        f"(knowledge:dr-kb/{uri}#Upload retry policy > Backoff)\n"
        f"- again [design/upload/backoff-full-jitter] and [{uri}] and [decision:0a1b]\n"
        f"- from [{uri}#Upload retry policy > Backoff]"
    )
    cites = detectors.find_citations(text, [uri])
    assert [(c["kind"], c["cite"]) for c in cites] == [
        ("knowledge", f"knowledge:dr-kb/{uri}#Upload retry policy > Backoff"),
        ("document", uri),
        ("document", f"{uri}#Upload retry policy > Backoff"),
        ("memory", "design/upload/backoff-full-jitter"),
        ("memory", "decision:0a1b"),
    ]
    assert detectors.parse_knowledge(cites[0]["cite"], [uri]) == (
        "dr-kb",
        uri,
        "Upload retry policy > Backoff",
    )
    assert detectors.parse_knowledge("knowledge:dr-kb/https://docs.example.com/up…", [uri]) is None


def test_a_section_is_found_by_the_breadcrumb_the_chunker_writes():
    # The chunker cleans a heading: a code span loses its backticks, a Setext heading
    # counts. The citation names the cleaned path.
    content = (
        "# Doc\n\n## Why `max_rows` is 512\n\nBecause rows.\n\n"
        "Setext head\n-----------\n\nUnder it.\n"
    )
    assert detectors.section_text(content, "Doc > Why max_rows is 512").startswith(
        "Why max_rows is 512\n\nBecause rows."
    )
    assert detectors.section_text(content, "Doc > Setext head").startswith(
        "Setext head\n\nUnder it."
    )


def test_a_section_is_found_by_its_last_breadcrumb():
    content = "# T\n\n## Backoff\n\nbody"
    assert detectors.section_text(content, "T > Backoff") == "Backoff\n\nbody"
    assert detectors.section_text(content, "T > Missing") is None
    # A heading is a heading line: the words in a sentence do not match.
    assert detectors.section_text("# T\n\nWe tune Backoff here.\n", "T > Backoff") is None
    # Two sections of one title: the cited segments pick one, a bare title neither.
    two = "# T\n\n## Option A\n\n### Cons\n\nA cons.\n\n## Option B\n\n### Cons\n\nB cons.\n"
    assert detectors.section_text(two, "Option B > Cons") == "Cons\n\nB cons.\n"
    assert detectors.section_text(two, "T > Option A > Cons").startswith("Cons\n\nA cons.")
    assert detectors.section_text(two, "Cons") is None


def test_a_citation_cut_short_or_with_backticks_still_finds_its_section():
    uri = "https://docs.example.com/d"
    content = "# Doc\n\n## Why `max_rows` is 512\n\nRows.\n\n## Retries (v2)\n\nRetried.\n"
    for text, want in (
        # The citation pattern stops at a backtick and at a closing parenthesis.
        (f"see knowledge:kb/{uri}#Doc > Why `max_rows` is 512.", "Why max_rows is 512"),
        (f"(knowledge:kb/{uri}#Doc > Retries (v2))", "Retries (v2)"),
        (f"[{uri}#Doc > Why `max_rows` is 512]", "Why max_rows is 512"),
    ):
        cite = detectors.find_citations(text, [uri])[0]
        if cite["kind"] == "knowledge":
            section = detectors.parse_knowledge(cite["cite"], [uri])[2]
        else:
            section = cite["cite"].partition("#")[2]
        assert detectors.section_text(content, section).startswith(want), text


# ---------------------------------------------------------------- scoring


def test_a_why_answer_scores_precision_recall_and_cited_claims():
    task = {"gold_arguments": ["g0", "g1", "g2"]}
    entry = {
        "citations": [
            {"cite": "design/a", "resolves": True},
            {"cite": "design/b", "resolves": False},
        ]
    }
    verdict = {
        "claims": [
            {"verdict": "correct", "gold": [0], "cites": ["design/a"], "cites_support": [True]},
            {"verdict": "unsupported", "cites": ["design/b"], "cites_support": [True]},
        ],
        "gold_covered": [0, 7],
        "potentially_misleading": False,
    }
    m = score.pass_metrics("A", task, entry, verdict)
    assert m["precision"] == 0.5 and m["recall"] == pytest.approx(1 / 3)
    # An unresolved citation never counts, whatever the judge said of it.
    assert m["citations"] == 2 and m["citations_good"] == 1 and m["claims_cited"] == 1
    # Each claim is scored on its own citations: a shared key that supports one claim
    # does not make another claim it does not support count as cited.
    verdict["claims"] = [
        {"verdict": "correct", "gold": [0], "cites": ["design/a"], "cites_support": [True]},
        {"verdict": "unsupported", "cites": ["design/a"], "cites_support": [False]},
    ]
    m = score.pass_metrics("A", task, entry, verdict)
    assert m["citations_good"] == 1 and m["claims_cited"] == 1


def test_a_feature_answer_scores_against_the_gold_facts():
    task = {"gold_facts": ["f0", "f1", "f2", "f3"]}
    entry = {"citations": [{"cite": "design/a", "resolves": True}]}
    verdict = {
        "claims": [
            {"verdict": "correct", "gold": [1], "cites": ["design/a"], "cites_support": [True]},
            {"verdict": "contradicts", "cites": [], "cites_support": []},
        ],
        "gold_covered": [1, 3],
        "potentially_misleading": True,
    }
    m = score.pass_metrics("D", task, entry, verdict)
    assert m["precision"] == 0.5 and m["recall"] == 0.5
    assert m["misleading"] == 1.0 and m["claims_cited"] == 1
    assert score.readable("D", task, verdict)
    assert not score.readable("D", task, {"claims": []})


def test_a_change_task_fails_on_a_detector_even_when_the_judge_passes_it():
    entry = {"citations": [], "detectors": [{"type": "x", "fired": True}]}
    m = score.pass_metrics("B", {}, entry, {"forbidden_proposed": [], "constraints_violated": []})
    assert m["avoids"] == 0.0 and m["satisfies"] == 0.0
    clean = {"citations": [], "detectors": [{"type": "judge", "fired": None}]}
    m = score.pass_metrics("B", {}, clean, {"forbidden_proposed": [], "pushed_back": True})
    assert m["avoids"] == 1.0 and m["pushed_back"] == 1.0


def test_supersession_and_abstention_tasks_pass_on_their_own_terms():
    entry = {"citations": []}
    sup = {"subtype": "supersession"}
    ab = {"subtype": "abstention"}
    assert score.pass_metrics("C", sup, entry, {"stale_served": True})["passed"] == 0.0
    assert score.pass_metrics("C", sup, entry, {"stale_served": False})["passed"] == 1.0
    assert score.pass_metrics("C", ab, entry, {"abstained": True})["passed"] == 1.0
    assert (
        score.pass_metrics("C", ab, entry, {"abstained": True, "invented_rationale": True})[
            "passed"
        ]
        == 0.0
    )


def test_kappa_and_planning_mde():
    assert score.cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert score.cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0
    # At the assumed planning spread, 15 paired tasks detect about 25 points.
    assert round(score.planning_mde(15), 2) == 0.25


def test_a_citation_counts_only_when_the_judge_names_it_exactly():
    entry = {"citations": [{"cite": "design/a/b", "resolves": True}]}
    near = {
        "cites": ["design/a"],
        "cites_support": [True],
        "forbidden_proposed": [],
        "constraints_violated": [],
    }
    assert score.pass_metrics("B", {}, entry, near)["citations_good"] == 0
    exact = {**near, "cites": ["[design/a/b]"]}
    assert score.pass_metrics("B", {}, entry, exact)["citations_good"] == 1


def test_the_no_claim_share_counts_answers_whose_passes_made_no_claim():
    ts = load(EXAMPLE)
    tid = ts.tasks["A"][0]["id"]

    def entry(claims):
        verdict = {
            "claims": claims,
            "gold_covered": [],
            "potentially_misleading": False,
            "abstained": True,
        }
        return {"citations": [], "detectors": [], "passes": [{"verdict": verdict}]}

    # An abstaining answer can still make a graded claim: only the claimless one counts.
    claim = {"verdict": "correct", "cites": [], "cites_support": []}
    judged = {f"{tid}/arm0/s1": entry([]), f"{tid}/arm0/s2": entry([claim])}
    assert score.answers_without_claims(ts, judged) == {"A": {0: 0.5}}
    # An unreadable pass is not a pass with no claim.
    unreadable = {"citations": [], "detectors": [], "passes": [{"verdict": {"claims": False}}]}
    judged[f"{tid}/arm0/s3"] = {
        **unreadable,
        "passes": [*unreadable["passes"], *entry([claim])["passes"]],
    }
    assert score.answers_without_claims(ts, judged) == {"A": {0: round(1 / 3, 4)}}
    # An answer whose only pass is unreadable is not scored at all.
    only = {**entry([])["passes"][0]["verdict"], "claims": False}
    judged[f"{tid}/arm0/s4"] = {"citations": [], "detectors": [], "passes": [{"verdict": only}]}
    assert score.answers_without_claims(ts, judged) == {"A": {0: round(1 / 3, 4)}}
    # A feature answer is counted for its own kind.
    did = ts.tasks["D"][0]["id"]
    judged[f"{did}/arm0/s1"] = entry([])
    assert score.answers_without_claims(ts, judged)["D"] == {0: 1.0}


def test_a_verdict_without_its_keys_is_unreadable_not_a_pass():
    ts = load(EXAMPLE)
    judged = {
        "B1/arm0/s1": {
            "citations": [],
            "detectors": [],
            "passes": [{"model": "m", "order": 0, "repeat": 0, "verdict": {"why": "x"}}],
        }
    }
    assert score.answer_metrics("B", ts.tasks["B"][0], judged["B1/arm0/s1"]) == {}
    assert score.unreadable(ts, judged)[0] == {"passes": 1, "unreadable": 1, "answers_unscored": 1}


def test_a_verdict_with_a_bool_where_a_list_goes_is_unreadable():
    ts = load(EXAMPLE)
    task = ts.tasks["B"][0]
    good = {"forbidden_proposed": [], "constraints_violated": [], "rubric_fail": False}
    assert score.readable("B", task, {**good, "cites": [], "cites_support": []})
    assert not score.readable("B", task, {**good, "cites": ["x"], "cites_support": True})
    a = {"claims": [{"cites": "x"}], "gold_covered": [], "potentially_misleading": False}
    assert not score.readable("A", ts.tasks["A"][0], a)
    assert not score.readable("A", ts.tasks["A"][0], {**a, "claims": "none"})
    assert not score.readable("A", ts.tasks["A"][0], {**a, "claims": [], "gold_covered": True})
    assert not score.readable("A", ts.tasks["A"][0], {**a, "claims": [], "gold_covered": 0})
    assert not score.readable("A", ts.tasks["A"][0], {**a, "claims": False})
    assert not score.readable("A", ts.tasks["A"][0], {**a, "claims": ["x"]})
    assert not score.readable("A", ts.tasks["A"][0], {**a, "claims": 0})
    assert not score.readable("B", task, {**good, "cites": False})
    assert not score.readable("B", task, {**good, "cites_support": 0})
    assert score.readable("B", task, {**good, "cites": None})


def test_one_document_gives_no_mde():
    ts = load(EXAMPLE)
    by_arm = {
        arm: {"A1": {"kind": "A", "precision": p}, "A2": {"kind": "A", "precision": p}}
        for arm, p in ((3, 1.0), (0, 0.5))
    }
    rows = [c for c in score.compare(ts, by_arm, True) if c["metric"] == "precision"]
    assert rows[0]["clusters"] == 1 and rows[0]["mde"] is None
    assert "| 3 vs 0 | A | precision | 2 (1) | +50.0 |" in score.report(
        {"arms": {}, "comparisons": rows, "tokens": {}, "treatment": {}, "judge_stability": {}}
    )


def test_extraction_precision_from_human_verdicts():
    ts = load(EXAMPLE)
    labels = [
        {"document": "thumbnail-cache", "memory_key": "k1", "verdict": "correct"},
        {"document": "thumbnail-cache", "memory_key": "k2", "verdict": "misleading"},
    ]
    out = score.extraction_precision(ts, labels, {})
    assert out["documents"]["thumbnail-cache"]["precision"] == 0.5
    assert out["misleading_rate"] == 0.5
    with pytest.raises(ValueError):
        score.extraction_precision(ts, [{"document": "x", "memory_key": "k", "verdict": "ok"}], {})


# ---------------------------------------------------------------- the spend cap


def test_the_ledger_refuses_a_call_that_could_cross_the_cap(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", max_usd=0.01)
    calls = []

    def complete(system, user, max_tokens):
        calls.append(1)
        return Reply("x", 1000, 100)

    model = Model("gpt-4o-mini", complete, ledger)
    model("answer", "s", "u", 100)
    assert ledger.spent() == pytest.approx(0.00021)
    with pytest.raises(CapReached):
        model("answer", "s", "u" * 4000, 20_000)
    assert len(calls) == 1
    with pytest.raises(CapReached, match="no price"):
        Model("unlisted-model", complete, ledger)("answer", "s", "u", 1)


async def test_every_provider_request_is_checked_and_recorded(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", max_usd=0.01)
    client = ProviderComplete("gpt-4o-mini", ledger)
    client.stage = "judge"
    small = httpx.Request("POST", "https://x/v1", json={"max_tokens": 100, "messages": []})
    await client._before(small)
    reply = httpx.Response(
        200, json={"usage": {"prompt_tokens": 1000, "completion_tokens": 100}}, request=small
    )
    await client._after(reply)
    assert ledger.rows()[0]["stage"] == "judge" and ledger.spent() == pytest.approx(0.00021)
    # A resend with a raised cap is checked on its own: past the cap it is never sent.
    raised = httpx.Request("POST", "https://x/v1", json={"max_completion_tokens": 100_000})
    with pytest.raises(CapReached):
        await client._before(raised)


def test_a_task_filter_naming_no_task_is_refused(tmp_path, capsys):
    argv = ["run", str(EXAMPLE), "--out", str(tmp_path / "out"), "--dry-run", "--tasks", "a1"]
    assert main(argv) == 2
    assert "--tasks names no such task: a1" in capsys.readouterr().err


def test_a_run_without_a_cap_or_a_priced_model_is_refused(tmp_path, capsys):
    out = tmp_path / "out"
    base = [
        "run",
        str(EXAMPLE),
        "--out",
        str(out),
        "--base-url",
        "http://127.0.0.1:1",
        "--arms",
        "0,3",
    ]
    assert main([*base, "--reader-model", "gpt-4o-mini", "--judge-model", "gpt-4o-mini"]) == 2
    assert "--max-usd" in capsys.readouterr().err
    assert (
        main(
            [*base, "--reader-model", "unlisted", "--judge-model", "gpt-4o-mini", "--max-usd", "1"]
        )
        == 2
    )
    assert "no price" in capsys.readouterr().err


def test_the_extraction_model_is_the_one_the_server_settings_name(tmp_path, capsys):
    base = ["run", str(EXAMPLE), "--out", str(tmp_path), "--arms", "4", "--dry-run"]
    # The unit tests' settings resolve to the fake model.
    assert main([*base, "--extract-model", "gpt-4o-mini"]) == 2
    assert "not --extract-model" in capsys.readouterr().err
    assert main(base) == 0
    assert "fake-echo-1" in capsys.readouterr().out


def test_the_dry_run_counts_every_call(capsys):
    ts = load(EXAMPLE)
    cfg = run.Config(
        out=Path("."),
        arms=[0, 3],
        k=2,
        reader="gpt-4o-mini",
        judges=["gpt-4o-mini"],
        judge_orders=2,
        judge_repeats=1,
    )
    p = run.plan(ts, cfg)
    assert p["usage"]["answer"]["calls"] == 9 * 2 * 2
    assert p["usage"]["judge"]["calls"] == 9 * 2 * 2 * 2
    assert p["usage"]["extract"]["calls"] == 0  # arm 4 not run
    run.print_plan(p)
    assert "at every output cap" in capsys.readouterr().out
    # Arm 6 reads a second search section, the knowledge hits, beside arm 4's surfaces.
    four, six = (
        run.plan(ts, dataclasses.replace(cfg, arms=[arm]))["usage"]["answer"]["in"]
        for arm in (4, 6)
    )
    assert six - four == 9 * 2 * run.SEARCH_SECTION_TOKENS


def test_the_extraction_bound_covers_every_window_the_extractor_reads():
    # A long document with few headings: overlapping chunks, windows cut by size.
    body = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(80))
    doc = Document("long", "u", "Long", f"# Title\n\n{body}\n\n## Tail\n\n" + "end " * 300)
    for window_chars in (500, 1000, 2900, 6000):
        chunks = [CorpusChunk("u", c.idx, c.text, c.section_path) for c in _chunk_text(doc.content)]
        read = len(_windows(chunks, set(), window_chars))
        calls, tin, tout = run.extraction_bound(doc, window_chars)
        # Two calls per window the extractor reads (a window read again).
        assert calls == 2 * read, window_chars
        cap = llm_max_tokens("decision_extract")
        assert tout == calls * cap
        # Each window's text as sent, its [document]/[section] lines included, and a
        # second-call note up to the output cap.
        sent = sum(len(dx.user_prompt(w, doc.title)) for w in _windows(chunks, set(), window_chars))
        assert tin >= 2 * sent // 3 + read * cap
    # A re-run over a revision lists the known decisions: a longer prompt per call.
    ts = load(EXAMPLE)
    first = ts.documents["upload-retries"]
    calls, revised_in, _ = run.extraction_bound(dataclasses.replace(first, revision_of="x"), 6000)
    _, first_in, _ = run.extraction_bound(first, 6000)
    known = settings.memory_decision_extract_known_max
    # The longest rows decision.record accepts (category 120, scenario 2000 characters),
    # as the prompt lists them.
    assert (run.DECISION_CATEGORY_MAX_CHARS, run.DECISION_SCENARIO_MAX_CHARS) == (120, 2000)
    longest = ("x" * 120, "x" * 2000, "x" * 5000)
    listed = len(dx.system_prompt(3, [longest] * known)) - len(dx.system_prompt(3))
    # Each call carries the whole list.
    assert revised_in - first_in >= listed // CHARS_PER_TOKEN_WORST * calls


# ---------------------------------------------------------------- end to end

# A code name each document's fake extraction mentions, and where the index defines it.
CODE_NAMES = {"thumbnail": "SegmentedLru", "upload": "RetryPolicy"}
DEFINITIONS = {
    "SegmentedLru": [("src/cache/segmented_lru.py", "SegmentedLru")],
    "RetryPolicy": [("src/upload/retry.py", "RetryPolicy")],
}


class FakeCodeIndex:
    def __init__(self, down: bool = False):
        self.down = down

    def definitions(self, name):
        return None if self.down else DEFINITIONS.get(name, [])


class FakeLoom:
    """The ops the harness calls, over dicts. memory.for_file serves a row whose files
    hold the path; loom_search serves rows sharing a word with the query."""

    def __init__(self, fail_on_upsert: bool = False, interrupt_upsert: bool = False):
        self.docs: dict[tuple[str, str], list[str]] = {}
        self.rows: dict[tuple[str, str], dict] = {}
        self.writes: list[tuple[str, str]] = []
        self.registered: list[tuple[str, float]] = []
        self.erased: list[str] = []
        self.fail_on_upsert = fail_on_upsert
        self.refuse_erase = False
        self.interrupt_upsert = interrupt_upsert
        # The files the fake extraction puts on its rows, as a spelled-out path would.
        self.extract_files: list[str] = []
        self.orgs: dict[str, str] = {}
        # USD the server spent per org: each document write costs `write_usd`.
        self.spend: dict[str, float] = {}
        self.write_usd = 0.01

    def bind(self, namespace, org):
        self.orgs[namespace] = org

    def register(self, namespace, description, cap_usd):
        self.registered.append((namespace, cap_usd))

    def erase(self, namespaces):
        if self.refuse_erase:
            return False
        self.erased += namespaces
        return True

    def server_spend(self, namespaces, since):
        return sum(self.spend.get(org, 0.0) for org in {self.orgs[ns] for ns in namespaces})

    def upsert_document(self, namespace, source_uri, title, content):
        if self.fail_on_upsert:
            raise RuntimeError("upsert refused")
        if self.interrupt_upsert:
            raise KeyboardInterrupt
        self.docs.setdefault((namespace, source_uri), []).append(content)
        org = self.orgs[namespace]
        self.spend[org] = self.spend.get(org, 0.0) + self.write_usd

    def write_gold(self, namespace, row, source_resource_id):
        self.writes.append((row.memory_key, row.value))
        self.rows[(namespace, row.memory_key)] = {
            "value": row.value,
            "files": row.files,
            "source": source_resource_id,
        }

    def extract(self, namespace, source_uri, window_chars):
        content = self.docs[(namespace, source_uri)][-1]
        # One key per document: the run over a revision reports the same row again.
        key = "decision:" + str(abs(hash(source_uri)) % 10**6)
        name = next(n for word, n in CODE_NAMES.items() if word in source_uri)
        self.rows[(namespace, key)] = {
            "value": content.split("\n\n")[2][:200],
            "files": list(self.extract_files),
            "source": f"knowledge:{namespace}/{source_uri}",
        }
        return 0, (
            f"{source_uri}: 1 window(s), 1 decision(s)\n- recorded {key}\n"
            f"    signal: {name}: camelcase, no definition\n- dropped [x] too short\n"
        )

    def decision_reasoning(self, namespace):
        return {}

    def set_files(self, namespace, key, files, reasoning):
        row = self.rows.get((namespace, key))
        if row is None:
            return None
        row["files"] = sorted({*row["files"], *files})
        return row["files"]

    def for_file(self, namespace, path):
        hits = [k for (ns, k), r in self.rows.items() if ns == namespace and path in r["files"]]
        rendered = "\n".join(
            f"- Decision: {self.rows[(namespace, k)]['value']} [id: {k}]" for k in hits
        )
        return {
            "path": path,
            "rendered": rendered,
            "tokens": len(rendered) // 4,
            "truncated": False,
            "keys": hits,
        }

    def search(self, namespace, query, session):
        words = set(re.findall(r"[a-z]{4,}", query.lower()))
        hits = [
            (k, r)
            for (ns, k), r in self.rows.items()
            if ns == namespace and words & set(re.findall(r"[a-z]{4,}", r["value"].lower()))
        ]
        lines = [f"loom_search '{query}': {len(hits)} memory hits."]
        if hits:
            lines += ["\nMemory hits:"] + [
                f"[2026-01-01] [{k}] (decision) {r['value']}" for k, r in hits
            ]
        docs = [
            {"source_uri": uri, "section": "S", "content": v[-1][:80]}
            for (ns, uri), v in self.docs.items()
            if ns == namespace
        ]
        return {"text": "\n".join(lines), "knowledge": docs}

    def knowledge_get(self, namespace, source_uri):
        versions = self.docs.get((namespace, source_uri))
        return {"found": bool(versions), "content": versions[-1] if versions else None}

    def memory_get(self, namespace, key):
        row = self.rows.get((namespace, key))
        return {
            "found": row is not None,
            "value": row and row["value"],
            "source_resource_id": row and row["source"],
        }


def _fake_model(ledger, usage=10):
    def complete(system, user, max_tokens):
        if "Return JSON only" in system:
            cites = re.findall(r"^- (\S+): RESOLVES", user, re.M)
            claims = (
                [
                    {
                        "verdict": "correct",
                        "gold": [0],
                        "cites": cites,
                        "cites_support": [True] * len(cites),
                    }
                ]
                if cites
                else []
            )
            verdict = {
                "claims": claims,
                "gold_covered": [0] if cites else [],
                "potentially_misleading": False,
                "abstained": not cites,
                "forbidden_proposed": [] if cites else [0],
                "constraints_violated": [],
                "cites": cites,
                "cites_support": [True] * len(cites),
                "stale_served": False,
                "current_stated": bool(cites),
                "invented_rationale": False,
                "rubric_fail": False,
            }
            return Reply(json.dumps(verdict), usage, usage)
        keys = re.findall(r"\[((?:design/|decision:)[^\]]+)\]", user)
        if keys:
            return Reply(f"- because it is recorded [{keys[0]}]", usage, usage)
        return Reply("No recorded decision covers this.", usage, usage)

    return Model("gpt-4o-mini", complete, ledger)


def _config(out, **kw):
    base = dict(
        out=out,
        arms=[0, 1, 2, 3, 4],
        k=2,
        reader="gpt-4o-mini",
        judges=["gpt-4o-mini"],
        judge_orders=2,
        judge_repeats=1,
        max_usd=1.0,
        extract_model="gpt-4o-mini",
        arm4_files=True,
    )
    return run.Config(**{**base, **kw})


def test_the_example_set_runs_end_to_end_over_a_fake_loom(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    cfg = _config(out)
    loom = FakeLoom()
    ledger = Ledger(out / "ledger.jsonl", cfg.max_usd)
    out.mkdir()
    model = _fake_model(ledger)
    assert run.run(ts, cfg, loom, model, [model], ledger, FakeCodeIndex()) is True

    # Gold rows go to the gold namespace only, the revised row after the first one.
    assert [k for k, _ in loom.writes].count("design/upload/retried-statuses") == 2
    assert not any(ns.endswith("-extract") for ns, k in loom.rows if k.startswith("design/"))
    # Each namespace carries the run's cap as its daily cost cap.
    assert {cap for _, cap in loom.registered} == {1.0}
    # The revision was extracted again after it was ingested; the run erased its namespaces.
    state = json.loads((out / "state.json").read_text())
    assert set(state["extraction"]) == {"thumbnail-cache", "upload-retries", "upload-retries-v2"}
    assert all(r["linked"] for r in state["extraction"].values())
    assert len(loom.erased) == 3 and state["erased"]
    assert any(r["estimated"] for r in ledger.rows())

    judged = json.loads((out / "judged.json").read_text())
    assert len(judged) == 9 * 5 * 2
    assert all(len(e["passes"]) == 2 for e in judged.values())
    cited = [c for e in judged.values() for c in e["citations"]]
    assert cited and all(c["resolves"] for c in cited if c["kind"] == "memory")

    m = score.score(ts, out)
    t = m["treatment"]
    assert t["arm3_for_file_served"].split("/")[0] != "0"
    # Arm 4's rows got their files from the code index: its pre-edit block fires too.
    assert t["arm4_for_file_served"].split("/")[0] != "0"
    # Three runs (one over the revision) reported two rows: each counted once.
    assert t["arm4_rows_extracted"] == 2 and t["arm4_rows_with_files"] == 2
    assert t["arm4_extract_failures"] == 0
    assert m["arms"][0]["B"]["avoids"] == 0.0  # arm 0 cites nothing: the fake proposes
    assert m["arms"][3]["B"]["avoids"] > 0
    assert m["judge_stability"]["orders"]["agreement"] == 1.0
    assert {(c["treatment"], c["reference"]) for c in m["comparisons"]} >= {(3, 0), (4, 3)}
    text = score.report(m)
    assert "| 3 vs 0 | B | avoids |" in text
    # The report carries numbers only: no task wording.
    assert not any(
        t["id"] + ":" in text or t.get("question", "@@") in text for _, t in ts.all_tasks()
    )

    # A finished run's namespaces are gone: it is scored, never resumed.
    with pytest.raises(run.RunError, match="erased"):
        run.run(ts, cfg, FakeLoom(), model, [model], ledger)


def test_without_a_code_index_arms_4_and_6_are_compared_with_gold_rows_served_alike(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    cfg = _config(out, arms=[3, 4, 5, 6], k=1, judge_orders=1, arm4_files=False)
    ledger = Ledger(out / "ledger.jsonl", cfg.max_usd)
    out.mkdir()
    model = _fake_model(ledger)
    loom = FakeLoom()
    # A row whose text spells out a path would reach the pre-edit block: arm 4 reads none.
    loom.extract_files = ["src/cache/segmented_lru.py", "src/upload/retry.py"]
    run.run(ts, cfg, loom, model, [model], ledger)
    surf = json.loads((out / "surfaces.json").read_text())
    assert all(
        v["for_file"] == [] for k, v in surf.items() if k.endswith(("/arm4", "/arm5", "/arm6"))
    )
    assert any(v["for_file"] for k, v in surf.items() if k.endswith("/arm3"))
    m = score.score(ts, out)
    assert "arm5_search_served" in m["treatment"]
    assert "arm4_for_file_served" not in m["treatment"]
    assert "arm6_for_file_served" not in m["treatment"]
    pairs = {(c["treatment"], c["reference"]) for c in m["comparisons"]}
    assert {(4, 5), (6, 5)} <= pairs
    assert not {(4, 3), (6, 3)} & pairs


def test_a_run_stopped_by_the_cap_resumes_without_spending_again(tmp_path, capsys):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    loom = FakeLoom()
    loom.write_usd = 0.0
    # Room for the extraction estimates and a few answers, not for the whole run.
    probe = Ledger(tmp_path / "probe.jsonl", 100.0)
    for d in ts.documents.values():
        probe.record("extract", "gpt-4o-mini", *run.extraction_bound(d, 6000)[1:])
    tight = probe.spent() + 0.01
    cfg = _config(out, max_usd=tight)
    ledger = Ledger(out / "ledger.jsonl", tight)
    model = _fake_model(ledger, usage=2000)
    assert run.run(ts, cfg, loom, model, [model], ledger, FakeCodeIndex()) is False
    assert loom.erased == []  # kept for the resume
    stopped = capsys.readouterr().err
    assert "to resume, or erase the namespaces with: loom namespaces-gc" in stopped
    first = ledger.rows()
    answered = len(json.loads((out / "answers.json").read_text()))
    assert 0 < answered < 9 * 5 * 2

    cfg.max_usd = ledger.max_usd = 100.0
    assert run.run(ts, cfg, loom, model, [model], ledger, FakeCodeIndex()) is True
    added = ledger.rows()[len(first) :]
    # The resume set nothing up again and only answered and judged what was left.
    assert not any(r["stage"] == "extract" for r in added)
    assert sum(r["stage"] == "answer" for r in added) == 9 * 5 * 2 - answered
    assert len(loom.erased) == 3


def test_a_failed_setup_erases_what_it_wrote_and_is_not_resumed(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    loom = FakeLoom(fail_on_upsert=True)
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    with pytest.raises(RuntimeError, match="upsert refused"):
        run.run(ts, _config(out), loom, model, [model], ledger)
    assert len(loom.erased) == 3
    with pytest.raises(run.RunError, match="setup failed"):
        run.run(ts, _config(out), FakeLoom(), model, [model], ledger)


def test_a_cap_below_the_first_extraction_stops_the_setup_and_the_resume_finishes_it(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    loom = FakeLoom()
    bound = run.extraction_bound(ts.documents["thumbnail-cache"], 6000)
    cap = cost_usd("gpt-4o-mini", bound[1], bound[2]) / 2
    cfg = _config(out, max_usd=cap)
    ledger = Ledger(out / "ledger.jsonl", cap)
    model = _fake_model(ledger)
    assert run.run(ts, cfg, loom, model, [model], ledger, FakeCodeIndex()) is False
    state = json.loads((out / "state.json").read_text())
    assert not state.get("erased") and loom.erased == []
    assert "register" in state["setup_steps"] and not state["extraction"]
    writes = len(loom.writes)

    cfg.max_usd = ledger.max_usd = 100.0
    assert run.run(ts, cfg, loom, model, [model], ledger, FakeCodeIndex()) is True
    # Steps done before the stop are not repeated: no second gold write, one registration.
    assert len(loom.writes) == writes + 1  # only the row written after the revision
    assert len(loom.registered) == 3
    assert set(json.loads((out / "state.json").read_text())["extraction"]) == set(ts.documents)


def test_two_runs_with_one_org_prefix_read_neither_rows_nor_spend_of_the_other(tmp_path):
    ts = load(EXAMPLE)
    loom = FakeLoom()
    ledgers = []
    for name in ("a", "b"):
        out = tmp_path / name
        out.mkdir()
        ledger = Ledger(out / "ledger.jsonl", 100.0)
        model = _fake_model(ledger)
        run.run(
            ts,
            _config(out, arms=[0, 3], k=1, judge_orders=1, keep_namespaces=True),
            loom,
            model,
            [model],
            ledger,
        )
        ledgers.append(ledger)
    a_orgs = {
        o
        for ns, o in loom.orgs.items()
        if ns.startswith(json.loads((tmp_path / "a" / "state.json").read_text())["prefix"])
    }
    b_orgs = {
        o
        for ns, o in loom.orgs.items()
        if ns.startswith(json.loads((tmp_path / "b" / "state.json").read_text())["prefix"])
    }
    assert a_orgs and b_orgs and not a_orgs & b_orgs
    # Each ledger carries its own run's server spend (three documents written per run).
    spent = [sum(r["usd"] for r in led.rows() if r["stage"] == "server") for led in ledgers]
    assert spent[0] == pytest.approx(spent[1]) and spent[0] == pytest.approx(0.03)


def test_set_files_keeps_every_field_and_the_paths_the_text_spells_out():
    row = {
        "found": True,
        "agent": "loom-decisions",
        "user_id": "-",
        "session_id": "-",
        "memory_type": "decision",
        "confidence": 0.5,
        "tags": ["decision"],
        "entities": ["segmented lru"],
        "source_resource_id": "knowledge:kb/u#S",
        "source_trace": "decision:abc",
        "source_class": "derived",
        "value": "Decided segmented LRU because scans churn probation. Applies to "
        "src/cache/config.py.",
    }
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((request.url.path, body))
        return httpx.Response(200, json=row if request.url.path.endswith("memory.get") else {})

    loom = Loom("http://loom.test")
    loom.client = httpx.Client(transport=httpx.MockTransport(handler))
    loom.bind("ns", "org")
    # The value's path is the outcome's, not the reasoning's: the extractor would not
    # have linked it. The reasoning's is.
    reasoning = "scans churn probation, as src/cache/probation.py shows"
    written = loom.set_files("ns", "decision:abc", ["src/cache/segmented_lru.py"], reasoning)
    assert written == ["src/cache/probation.py", "src/cache/segmented_lru.py"]
    path, body = sent[-1]
    assert path == "/v1/memory.set"
    for field in (
        "agent",
        "user_id",
        "session_id",
        "memory_type",
        "confidence",
        "tags",
        "entities",
        "source_resource_id",
        "source_trace",
        "source_class",
        "value",
    ):
        assert body[field] == row[field], field
    assert sorted(body["files"]) == written


def test_a_task_id_with_a_slash_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    tasks["A"][0]["id"] = "A/1"
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="without '/'"):
        load(root)


def test_a_change_verdict_must_answer_the_tasks_rubric():
    ts = load(EXAMPLE)
    with_rubric = ts.tasks["B"][1]  # B2: a judge detector
    verdict = {"forbidden_proposed": [], "constraints_violated": []}
    assert not score.readable("B", with_rubric, verdict)
    assert not score.readable("B", with_rubric, {**verdict, "rubric_fail": None})
    assert score.readable("B", with_rubric, {**verdict, "rubric_fail": False})
    assert score.readable("B", ts.tasks["B"][0], verdict)  # B1 has no rubric


def test_a_supersession_verdict_needs_current_stated():
    ts = load(EXAMPLE)
    task = ts.tasks["C"][0]
    verdict = {"stale_served": False, "abstained": False, "invented_rationale": False}
    assert not score.readable("C", task, verdict)
    assert score.readable("C", task, {**verdict, "current_stated": True})
    assert score.readable("C", ts.tasks["C"][1], verdict)  # abstention needs no such key


def test_a_cap_stop_exits_1_and_a_fallback_model_refuses_arm_4(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.runner, "run", lambda *a, **k: False)
    base = ["run", str(EXAMPLE), "--out", str(tmp_path), "--base-url", "http://loom.test"]
    assert main([*base, "--arms", "0", "--offline"]) == 1
    monkeypatch.setattr(cli, "server_extract_model", lambda: ("openai", "gpt-4o-mini", "gpt-4o"))
    assert main([*base, "--arms", "4", "--dry-run"]) == 2
    assert "LOOM_LLM_FALLBACK_MODEL" in capsys.readouterr().err


def test_a_task_needs_gold_rows_and_a_supersession_task_its_current_design(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    del tasks["C"][0]["current"]
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="current"):
        load(root)
    tasks["C"][0]["current"] = "x"
    del tasks["A"][0]["gold_rows"]
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="gold_rows"):
        load(root)


def test_a_paid_run_refuses_an_extraction_on_the_fake_model(tmp_path, capsys):
    base = ["run", str(EXAMPLE), "--out", str(tmp_path), "--base-url", "http://loom.test"]
    paid = ["--reader-model", "gpt-4o-mini", "--judge-model", "gpt-4o-mini", "--max-usd", "1"]
    # The unit tests' settings resolve to the fake model.
    assert main([*base, "--arms", "4", *paid]) == 2
    assert "fake model" in capsys.readouterr().err


def test_arm_5_is_added_when_arm_4_has_no_code_index(tmp_path, capsys):
    base = ["run", str(EXAMPLE), "--out", str(tmp_path), "--arms", "3,4", "--dry-run"]
    assert main(base) == 0
    assert "5 = gold rows through loom_search only" in capsys.readouterr().out
    assert main([*base, "--code-index", "http://index.test"]) == 0
    assert "5 = " not in capsys.readouterr().out
    # Whether arm 3 runs or not.
    assert main([*base[:5], "4", "--dry-run"]) == 0
    assert "5 = gold rows through loom_search only" in capsys.readouterr().out
    # Arm 6 reads the pre-edit block only with files, as arm 4 does.
    assert main([*base[:5], "6", "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "arm 6: no code index" in printed
    assert "5 = gold rows through loom_search only" in printed


def test_a_resume_with_other_flags_is_refused(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 0.001)
    model = _fake_model(ledger, usage=2000)
    cfg = _config(out, arms=[0], max_usd=0.001)
    assert run.run(ts, cfg, FakeLoom(), model, [model], ledger) is False
    with pytest.raises(run.RunError, match="arms"):
        run.run(ts, _config(out, arms=[0, 1], max_usd=1.0), FakeLoom(), model, [model], ledger)


def test_an_error_after_setup_keeps_the_namespaces_and_says_how_to_erase_them(tmp_path, capsys):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)

    def broken(system, user, max_tokens):
        raise RuntimeError("provider down")

    model = Model("gpt-4o-mini", broken, ledger)
    loom = FakeLoom()
    with pytest.raises(RuntimeError, match="provider down"):
        run.run(ts, _config(out, arms=[0, 3]), loom, model, [model], ledger)
    assert loom.erased == []
    err = capsys.readouterr().err
    assert "run the same command again to resume" in err and "loom namespaces-gc" in err


def test_a_code_index_that_does_not_answer_is_reported(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    run.run(
        ts,
        _config(out, arms=[4], k=1, judge_orders=1),
        FakeLoom(),
        model,
        [model],
        ledger,
        FakeCodeIndex(down=True),
    )
    t = score.score(ts, out)["treatment"]
    assert t["arm4_code_lookups_failed"] == 3 and t["arm4_rows_with_files"] == 0


def test_an_interrupt_after_setup_keeps_the_namespaces_and_says_how_to_erase_them(tmp_path, capsys):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)

    def interrupted(system, user, max_tokens):
        raise KeyboardInterrupt

    model = Model("gpt-4o-mini", interrupted, ledger)
    loom = FakeLoom()
    with pytest.raises(KeyboardInterrupt):
        run.run(ts, _config(out, arms=[0, 3]), loom, model, [model], ledger)
    assert loom.erased == []
    assert "loom namespaces-gc" in capsys.readouterr().err


def test_an_answer_key_parses_into_task_arm_and_sample():
    # A task id may hold "arm"; the arm and the sample follow it.
    assert parse_key("A1-farm/arm3/s2") == ("A1-farm", 3)
    assert parse_sample("A1-farm/arm3/s2") == 2


def test_task_ids_holding_arm_are_scored(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    tasks["A"][0]["id"], tasks["A"][1]["id"] = "warm-start", "arm2-x"
    (root / "tasks.json").write_text(json.dumps(tasks))
    ts = load(root)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    run.run(ts, _config(out, arms=[0, 3], k=1, judge_orders=1), FakeLoom(), model, [model], ledger)
    m = score.score(ts, out)
    assert {"warm-start", "arm2-x"} <= set(m["per_task"][3]) and set(m["tokens"]) == {0, 3}


def test_a_timed_out_extraction_keeps_both_output_streams(monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1, output=b"\xff out", stderr=b"err")

    monkeypatch.setattr(subprocess, "run", slow)
    loom = Loom("http://loom.test")
    loom.bind("ns", "org")
    rc, report = loom.extract("ns", "u", 6000)
    assert rc == 1
    assert report == f"\ufffd outerr\nerror: stopped after {EXTRACT_TIMEOUT_S} s"


def test_each_knowledge_pointer_ends_where_the_next_begins():
    uri = "https://docs.example.com/d"
    line = f"see knowledge:kb/{uri}#Doc > A and knowledge:kb/{uri}#Doc > B here"
    cites = [c["cite"] for c in detectors.find_citations(line, [uri])]
    assert len(cites) == 2 and "knowledge:" not in cites[0][len("knowledge:") :]
    sections = [detectors.parse_knowledge(c, [uri])[2] for c in cites]
    assert sections == ["Doc > A and", "Doc > B here"]
    # Two pointers with no space between them.
    joined = detectors.find_citations(f"knowledge:kb/{uri}#A,knowledge:kb/{uri}#B", [uri])
    assert [c["cite"] for c in joined] == [f"knowledge:kb/{uri}#A,", f"knowledge:kb/{uri}#B"]
    assert [detectors.parse_knowledge(c["cite"], [uri])[2] for c in joined] == ["A", "B"]
    # A pointer with no section: the words after it are the sentence's, not a section.
    (bare,) = detectors.find_citations(f"knowledge:kb/{uri} is what we read", [uri])
    assert detectors.parse_knowledge(bare["cite"], [uri]) == ("kb", uri, "")


def test_a_citation_of_a_whole_path_names_that_section_though_another_ends_so():
    content = "# A\n\n## B\n\nfirst.\n\n# X\n\n## A\n\n### B\n\nsecond.\n"
    assert detectors.section_text(content, "A > B") == "B\n\nfirst.\n\n"
    assert detectors.section_text(content, "X > A > B").startswith("B\n\nsecond.")


def test_a_citation_of_a_deleted_document_does_not_resolve():
    ts = load(EXAMPLE)
    loom = FakeLoom()
    loom.bind("kb", "org")
    ns = {"kb": "kb", "gold": "gold", "extract": "extract"}
    uri = ts.documents["thumbnail-cache"].source_uri
    loom.upsert_document("kb", uri, "t", ts.documents["thumbnail-cache"].content)
    resolver = run.Resolver(ts, loom, ns)
    cite = {
        "kind": "knowledge",
        "cite": f"knowledge:kb/{uri}#Thumbnail cache eviction > Cache size",
    }
    assert resolver.resolve(cite, 1)[0] is True
    del loom.docs[("kb", uri)]  # knowledge.get answers found=false once it is deleted
    assert run.Resolver(ts, loom, ns).resolve(cite, 1) == (False, "document not found")


def test_a_resume_with_another_reader_is_refused(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 0.001)
    model = _fake_model(ledger, usage=2000)
    assert (
        run.run(ts, _config(out, arms=[0], max_usd=0.001), FakeLoom(), model, [model], ledger)
        is False
    )
    other = _config(out, arms=[0], max_usd=1.0, reader="gpt-4.1-mini")
    with pytest.raises(run.RunError, match="reader"):
        run.run(ts, other, FakeLoom(), model, [model], ledger)


def _mock_loom(handler):
    loom = Loom("http://loom.test")
    loom.client = httpx.Client(transport=httpx.MockTransport(handler))
    loom.bind("ns", "org")
    return loom


def test_a_pre_edit_block_counts_only_the_rows_it_prints():
    hits = [
        # Past the whole-row ceiling: left out, and named on the closing line. Its key
        # is the start of the printed row's, so a plain prefix test would count it.
        {
            "memory_key": "design/a",
            "memory_type": "decision",
            "value": "x" * 7000,
            "tags": ["decision"],
            "namespace": "ns",
        },
        {
            "memory_key": "design/a-v2",
            "memory_type": "decision",
            "value": "short",
            "tags": ["decision"],
            "namespace": "ns",
        },
        {
            "memory_key": "design/team",
            "memory_type": "decision",
            "value": "a team one",
            "tags": ["decision"],
            "namespace": "other",
            "actor": "bob",
        },
    ]
    rendered, tokens, truncated = render_block("src/x.py", hits, 300, home="ns")
    assert truncated and "left out" in rendered and "design/a." in rendered
    out = {
        "rendered": rendered,
        "tokens_estimate": tokens,
        "truncated": truncated,
        "hits": [{"memory_key": h["memory_key"]} for h in hits],
    }
    loom = _mock_loom(lambda request: httpx.Response(200, json=out))
    assert loom.for_file("ns", "src/x.py")["keys"] == ["design/a-v2", "design/team"]


def test_server_spend_counts_every_traffic_class_in_the_run_orgs():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        # A worker's cost row on the run's rows carries no traffic class.
        return httpx.Response(200, json={"total_cost_usd": 0.25})

    loom = _mock_loom(handler)
    assert loom.server_spend(["ns"], date.today()) == 0.25
    assert "traffic_class" not in sent[0] and sent[0]["org"] == "org"


def test_a_search_error_is_raised_with_its_text():
    replies = iter(
        [
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad arguments"}},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "namespace refused"}],
                },
            },
        ]
    )
    loom = _mock_loom(lambda request: httpx.Response(200, json=next(replies)))
    with pytest.raises(RuntimeError, match="bad arguments"):
        loom.search("ns", "q", "s")
    with pytest.raises(RuntimeError, match="namespace refused"):
        loom.search("ns", "q", "s")


def test_a_resume_with_another_judge_is_refused(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 0.001)
    model = _fake_model(ledger, usage=2000)
    cfg = _config(out, arms=[0], max_usd=0.001)
    assert run.run(ts, cfg, FakeLoom(), model, [model], ledger) is False
    other = _config(out, arms=[0], max_usd=1.0, judges=["gpt-4.1-mini"])
    with pytest.raises(run.RunError, match="judges"):
        run.run(ts, other, FakeLoom(), model, [model], ledger)


def test_a_resume_with_another_extraction_model_is_refused(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 0.001)
    model = _fake_model(ledger, usage=2000)
    assert (
        run.run(ts, _config(out, arms=[0], max_usd=0.001), FakeLoom(), model, [model], ledger)
        is False
    )
    other = _config(out, arms=[0], max_usd=1.0, extract_model="gpt-4.1-mini")
    with pytest.raises(run.RunError, match="extract_model"):
        run.run(ts, other, FakeLoom(), model, [model], ledger)


def test_the_pre_edit_bound_covers_a_block_at_its_margin():
    # Decisions filling the whole-row ceiling to within a few characters (each line is
    # its 28-character head, the value and a newline), six past it (the left-out line
    # prints), and facts past the budget (the "… more" line prints).
    whole = settings.memory_for_file_whole_tokens
    decisions = [
        {
            "memory_key": f"decision:{i:04d}",
            "memory_type": "decision",
            "value": "d" * 1170,
            "tags": ["decision"],
            "namespace": "ns",
        }
        for i in range(5)
    ]
    assert whole * 4 - 5 * (28 + 1170 + 1) < 28
    # Six left out, keys at the 120 characters the left-out line prints of each: the line
    # names five and counts the sixth, at its longest.
    decisions += [
        {
            "memory_key": f"decision:{i}" + "k" * (120 - len(f"decision:{i}")),
            "memory_type": "decision",
            "value": "x" * 7000,
            "tags": ["decision"],
            "namespace": "ns",
        }
        for i in range(6)
    ]
    facts = [
        {
            "memory_key": f"fact:{i:03d}",
            "memory_type": "semantic",
            "value": "f" * 300,
            "tags": [],
            "namespace": "ns",
        }
        for i in range(40)
    ]
    rendered, tokens, _ = render_block(
        "src/cache/segmented_lru.py", decisions + facts, PRE_EDIT_BUDGET, home="ns"
    )
    assert "- decision:0004 [decision]" in rendered  # the ceiling holds all five
    assert "more; memory.for_file" in rendered and "6 more decisions or constraints" in rendered
    assert "and 1 more." in rendered
    # Past the rows' own budgets, inside the plan's.
    assert whole + PRE_EDIT_BUDGET < tokens <= run.pre_edit_block_tokens()


def test_a_setup_that_failed_is_not_resumed_even_with_its_namespaces_kept(tmp_path, capsys):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    cfg = _config(out, keep_namespaces=True)
    loom = FakeLoom(fail_on_upsert=True)
    with pytest.raises(RuntimeError, match="upsert refused"):
        run.run(ts, cfg, loom, model, [model], ledger)
    assert loom.erased == []
    err = capsys.readouterr().err
    assert "loom namespaces-gc" in err and "resume" not in err
    again = FakeLoom()
    with pytest.raises(run.RunError, match="setup failed .erase its namespaces with: loom"):
        run.run(ts, cfg, again, model, [model], ledger)
    assert again.erased == []  # kept, as asked: only the command is shown


def test_an_interrupted_setup_erases_its_namespaces_and_is_not_resumed(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    loom = FakeLoom(interrupt_upsert=True)
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    with pytest.raises(KeyboardInterrupt):
        run.run(ts, _config(out), loom, model, [model], ledger)
    assert len(loom.erased) == 3
    with pytest.raises(run.RunError, match="setup failed"):
        run.run(ts, _config(out), FakeLoom(), model, [model], ledger)


def test_a_finished_run_with_its_namespaces_kept_reruns_with_nothing_to_do(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    cfg = _config(out, arms=[0, 3], k=1, judge_orders=1, keep_namespaces=True)
    loom = FakeLoom()
    assert run.run(ts, cfg, loom, model, [model], ledger) is True
    rows = len(ledger.rows())
    assert run.run(ts, cfg, loom, model, [model], ledger) is True
    assert len(ledger.rows()) == rows and loom.erased == []


def test_an_a_task_without_rejected_options_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    del tasks["A"][0]["rejected_options"]
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="rejected_options"):
        load(root)


def test_a_feature_task_without_gold_facts_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    del tasks["D"][0]["gold_facts"]
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="gold_facts"):
        load(root)


def test_arm_6_serves_the_extracted_rows_with_the_documents_knowledge_hits(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    cfg = _config(out, arms=[2, 4, 6], k=1, judge_orders=1)
    ledger = Ledger(out / "ledger.jsonl", cfg.max_usd)
    out.mkdir()
    model = _fake_model(ledger)
    assert run.run(ts, cfg, FakeLoom(), model, [model], ledger, FakeCodeIndex()) is True
    surf = json.loads((out / "surfaces.json").read_text())
    six = surf["B1/arm6"]
    # The rows of arm 4 and the knowledge hits of arm 2, from one search of the
    # namespace the documents were extracted in.
    assert six["for_file_keys"] == surf["B1/arm4"]["for_file_keys"] != []
    assert "loom_search knowledge section:" in six["context"]
    assert six["tokens"]["search_knowledge"] > 0 and six["knowledge_uris"]
    assert "knowledge section" not in surf["B1/arm4"]["context"]
    m = score.score(ts, out)
    t = m["treatment"]
    assert t["arm6_knowledge_served"] == t["arm2_knowledge_served"] == "8/8"  # gold-row tasks
    assert t["arm6_for_file_served"].split("/")[0] != "0"
    pairs = {(c["treatment"], c["reference"]) for c in m["comparisons"]}
    assert {(6, 2), (6, 4)} <= pairs
    assert m["arms"][6]["D"]["tasks"] == 1


def test_scoring_with_the_unfilled_label_sheet_fails_clearly(tmp_path, capsys):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    cfg = _config(out, arms=[4], k=1, judge_orders=1)
    run.run(ts, cfg, FakeLoom(), model, [model], ledger, FakeCodeIndex())
    sheet = out / "extraction_labels.todo.json"
    assert json.loads(sheet.read_text())  # the run wrote rows, each verdict empty
    code = main(["score", str(EXAMPLE), "--out", str(out), "--extraction-labels", str(sheet)])
    assert code == 2
    assert "fill each row's verdict first" in capsys.readouterr().err


def test_score_writes_the_summary_without_per_task_rows(tmp_path):
    # A second revision of one document: two revisions, one revised document.
    root = _copy_example(tmp_path)
    docs = json.loads((root / "documents.json").read_text())
    third = {**docs[2], "id": "upload-retries-v3", "path": "docs/upload-retries-v3.md"}
    (root / third["path"]).write_text((root / docs[2]["path"]).read_text())
    (root / "documents.json").write_text(json.dumps([*docs, third]))
    ts = load(root)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    cfg = _config(out, arms=[0, 1, 3, 4], k=2)
    run.run(ts, cfg, FakeLoom(), model, [model], ledger, FakeCodeIndex())
    sheet = out / "extraction_labels.todo.json"
    labels = [{**row, "verdict": "correct"} for row in json.loads(sheet.read_text())]
    sheet.write_text(json.dumps(labels))
    path = tmp_path / "summary.json"
    argv = ["score", str(root), "--out", str(out), "--extraction-labels", str(sheet)]
    assert main([*argv, "--summary", str(path)]) == 0
    first = path.read_text()
    summary = json.loads(first)
    assert "per_task" not in summary
    assert summary["run"]["reader"] == "gpt-4o-mini" and summary["run"]["k"] == 2
    assert set(summary["run"]) == {
        "arms",
        "arm4_files",
        "extract_window_chars",
        "reader",
        "judges",
        "extract_model",
        "k",
        "judge_orders",
        "judge_repeats",
    }
    # Document ids can name private material: the extraction figures are a list.
    assert isinstance(summary["extraction"]["documents"], list)
    assert summary["extraction"]["documents"]
    for doc_id in ts.documents:
        assert doc_id not in first
    for _, task in ts.all_tasks():
        assert task["id"] not in first
    spend = summary["spend_usd"]
    assert spend["total"] == round(sum(r["usd"] for r in ledger.rows()), 4)
    assert spend["answers_by_arm"]["0"]["answers"] == 2 * len(list(ts.all_tasks()))
    assert (1, 0) in {(c["treatment"], c["reference"]) for c in summary["comparisons"]}
    assert summary["task_set"] == {
        "documents": 2,
        "revisions": 2,
        "revised_documents": 1,
        "tasks": {"A": 3, "B": 3, "C": 2, "D": 1},
        "c_subtypes": {"abstention": 1, "supersession": 1},
        "tasks_with_gold_rows": 8,
    }
    assert summary["run"]["judge_orders"] == 2
    assert summary["run"]["judge_repeats"] == cfg.judge_repeats
    assert set(summary["answers_without_claims"]["A"]) == {"0", "1", "3", "4"}
    # Scoring again regenerates the same file.
    assert main([*argv, "--summary", str(path)]) == 0
    assert path.read_text() == first


def test_the_summary_reads_a_cut_ledger_without_rewriting_it(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    run.run(ts, _config(out, arms=[0], k=1, judge_orders=1), FakeLoom(), model, [model], ledger)
    total = round(sum(r["usd"] for r in ledger.rows()), 4)
    path = out / "ledger.jsonl"
    whole = path.read_text()
    path.write_text(whole + '{"stage": "answer", "usd": 0.')  # an interrupt cut this row
    before = path.read_text()
    spend = score.summary(ts, score.score(ts, out), out)["spend_usd"]
    assert path.read_text() == before
    assert spend["ledger_rows_dropped"] == 1 and spend["total"] == total
    # A broken row before the last, or a broken last row that ends in a newline, is
    # not an interrupt mid-append: refused. A blank line is skipped.
    for text in ('{"stage": "answer", "usd": 0.\n' + whole, whole + '{"usd": 0.\n'):
        path.write_text(text)
        with pytest.raises(json.JSONDecodeError):
            score.summary(ts, score.score(ts, out), out)
    path.write_text(whole + "\n")
    assert score.summary(ts, score.score(ts, out), out)["spend_usd"]["total"] == total
    # A whole last row without its newline is kept and counted.
    path.write_text(whole)
    answer = score.summary(ts, score.score(ts, out), out)["spend_usd"]["by_stage"]["answer"]
    last = '{"stage": "answer", "usd": 0.5}'
    path.write_text(whole + last)
    spend = score.summary(ts, score.score(ts, out), out)["spend_usd"]
    assert path.read_text() == whole + last
    assert spend["ledger_rows_dropped"] == 0
    assert spend["total"] == round(total + 0.5, 4)
    assert spend["by_stage"]["answer"] == round(answer + 0.5, 4)


def test_the_summary_of_an_unpriced_reader_has_no_answer_cost(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    run.run(ts, _config(out, arms=[0], k=1, judge_orders=1), FakeLoom(), model, [model], ledger)
    state = json.loads((out / "state.json").read_text())
    state["flags"]["reader"] = "a-model-with-no-price"
    (out / "state.json").write_text(json.dumps(state))
    summary = score.summary(ts, score.score(ts, out), out)
    assert summary["spend_usd"]["answers_by_arm"][0]["usd"] is None
    assert summary["spend_usd"]["answers_by_arm"][0]["answers"] == len(list(ts.all_tasks()))


def test_a_ledger_line_cut_short_by_an_interrupt_is_dropped(tmp_path, capsys):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, 1.0)
    ledger.record_usd("server", 0.25)
    with path.open("a") as f:
        f.write('{"stage": "answer", "usd": 0.')  # the process died here
    resumed = Ledger(path, 1.0)
    assert resumed.spent() == 0.25
    assert "undercounts by one call" in capsys.readouterr().err
    resumed.record_usd("server", 0.5)
    assert [r["usd"] for r in Ledger(path, 1.0).rows()] == [0.25, 0.5]


def test_save_json_leaves_the_old_file_whole_when_a_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "answers.json"
    run.save_json(path, {"A1/arm0/s1": {"text": "kept"}})

    def broken(src, dst):
        raise OSError("rename refused")

    # The temp file is written, then the rename fails.
    monkeypatch.setattr(llm.os, "replace", broken)
    with pytest.raises(OSError):
        run.save_json(path, {"A1/arm0/s1": {"text": "lost"}})
    monkeypatch.undo()
    assert json.loads(path.read_text()) == {"A1/arm0/s1": {"text": "kept"}}
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_whole_ledger_row_without_its_newline_is_kept(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"stage": "server", "usd": 0.25}')  # written whole, newline lost
    ledger = Ledger(path, 1.0)
    assert ledger.spent() == 0.25
    ledger.record_usd("server", 0.5)
    assert [r["usd"] for r in Ledger(path, 1.0).rows()] == [0.25, 0.5]


def test_a_diff_is_read_file_by_file_without_trusting_hunk_counts():
    two = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,1 +1,1 @@\n"  # undercounted: three lines follow
        " keep\n\n-old\n+new\n"  # a blank context line too
        "@@ -9 +9 @@\n-later\n+later2\n"  # a second hunk in the same file
        "--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-b0\n+b1\n"
    )
    assert detectors.diff_files(two) == {
        "src/a.py": {"-": ["old", "later"], "+": ["new", "later2"]},
        "src/b.py": {"-": ["b0"], "+": ["b1"]},
    }
    # `+++` headers with no `---`, each before its hunk.
    only = "+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n+++ b/b.py\n@@ -1 +0,0 @@\n-y = 2\n"
    assert detectors.diff_files(only) == {
        "a.py": {"+": ["x = 1"], "-": []},
        "b.py": {"+": [], "-": ["y = 2"]},
    }
    # A removed `-- x` and an added `++ x` alone are lines.
    lines = "--- a/q.sql\n+++ b/q.sql\n@@ -1 +1 @@\n--- drop it\n context\n+++ add it\n"
    assert detectors.diff_files(lines) == {"q.sql": {"-": ["-- drop it"], "+": ["++ add it"]}}


def test_a_detector_without_its_keys_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    tasks = json.loads((root / "tasks.json").read_text())
    tasks["B"][1]["detector"] = [{"type": "diff_added_regex", "path_glob": "src/*"}]
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="diff_added_regex detector without"):
        load(root)


def test_a_detector_of_the_wrong_shape_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    for broken, message in (
        ({"detector": {"type": "judge", "rubric": "r"}}, "detector is a list"),
        ({"detector": ["file_touched"]}, "an object with a known type"),
        ({"detector": [{"type": "file_touched", "paths": "src/a.py"}]}, "list of strings"),
        ({"detector": [{"type": "diff_added_regex", "pattern": 3}]}, "pattern is a string"),
        ({"detector": [{"type": "judge", "rubric": ["r"]}]}, "rubric is a string"),
    ):
        tasks = json.loads((EXAMPLE / "tasks.json").read_text())
        tasks["B"][0].update(broken)
        (root / "tasks.json").write_text(json.dumps(tasks))
        with pytest.raises(TaskSetError, match=message):
            load(root)


def test_a_diff_fence_closes_only_at_a_line_start():
    text = "## Diff\n```diff\n--- a/x.md\n+++ b/x.md\n+see ```python here\n+more\n```\n"
    assert detectors.diff_files(detectors.extract_diff(text)) == {
        "x.md": {"+": ["see ```python here", "more"], "-": []}
    }


def test_the_loader_refuses_malformed_task_fields(tmp_path):
    root = _copy_example(tmp_path)
    for kind, index, broken, message in (
        ("B", 0, {"detector": [{"type": "diff_added_regex", "pattern": "("}]}, "compile"),
        ("A", 0, {"gold_arguments": "one argument"}, "gold_arguments is a list"),
        ("A", 0, {"rejected_options": [1]}, "rejected_options is a list"),
        ("B", 0, {"forbidden_options": None}, "forbidden_options is a list"),
        ("C", 0, {"stale_claims": ["ok", 2]}, "stale_claims is a list"),
    ):
        tasks = json.loads((EXAMPLE / "tasks.json").read_text())
        tasks[kind][index].update(broken)
        (root / "tasks.json").write_text(json.dumps(tasks))
        with pytest.raises(TaskSetError, match=message):
            load(root)
    (root / "tasks.json").write_text((EXAMPLE / "tasks.json").read_text())
    docs = json.loads((EXAMPLE / "documents.json").read_text())
    docs[0]["id"] = "a/b"
    (root / "documents.json").write_text(json.dumps(docs))
    with pytest.raises(TaskSetError, match="without '/'"):
        load(root)


def test_judge_agreement_leaves_out_change_answers_a_detector_decided():
    ts = load(EXAMPLE)
    base = {"forbidden_proposed": [], "constraints_violated": []}

    def entry(fired):
        passes = [
            {
                "model": "m",
                "order": order,
                "repeat": 0,
                "verdict": {**base, "forbidden_proposed": [0] if order else []},
            }
            for order in (0, 1)
        ]
        return {"citations": [], "detectors": [{"type": "x", "fired": fired}], "passes": passes}

    decided = score.judge_stability(ts, {"B1/arm0/s1": entry(True)})
    assert decided == {}  # the detector fired: the passes could not disagree
    open_ = score.judge_stability(ts, {"B1/arm0/s1": entry(False)})
    assert open_["orders"] == {"answers": 1, "agreement": 0.0}


def test_judge_accuracy_against_humans_leaves_out_what_a_detector_decided():
    ts = load(EXAMPLE)

    def entry(fired):
        verdict = {"forbidden_proposed": [], "constraints_violated": [], "pushed_back": True}
        return {
            "citations": [],
            "detectors": [{"type": "x", "fired": fired}],
            "passes": [{"model": "m", "order": 0, "repeat": 0, "verdict": verdict}],
        }

    judged = {"B1/arm0/s1": entry(True), "B1/arm0/s2": entry(False)}
    labels = [
        # The detector decided avoidance here; the label says nothing about the judge.
        {"answer": "B1/arm0/s1", "field": "avoids", "value": False},
        # Push-back is the judge's call on that same answer: counted.
        {"answer": "B1/arm0/s1", "field": "pushed_back", "value": True},
        {"answer": "B1/arm0/s2", "field": "avoids", "value": True},
        # Whether a detector fired is never the judge's call, decided or not.
        {"answer": "B1/arm0/s2", "field": "detector_fired", "value": True},
    ]
    assert score.judge_vs_human(ts, judged, labels) == {"items": 2, "accuracy": 1.0, "kappa": 1.0}


def test_a_refused_erasure_is_tried_again_on_the_next_run(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    cfg = _config(out, arms=[0, 3], k=1, judge_orders=1)
    loom = FakeLoom()
    loom.refuse_erase = True
    assert run.run(ts, cfg, loom, model, [model], ledger) is True
    state = json.loads((out / "state.json").read_text())
    assert not state.get("erased") and loom.erased == []
    loom.refuse_erase = False
    assert run.run(ts, cfg, loom, model, [model], ledger) is True
    assert json.loads((out / "state.json").read_text())["erased"] and len(loom.erased) == 2


def test_a_refused_erasure_logs_the_command_that_finishes_it(caplog):
    loom = _mock_loom(lambda request: httpx.Response(403, json={"detail": "not an admin"}))
    loom.bind("ns2", "org")
    with caplog.at_level("WARNING"):
        assert loom.erase(["ns", "ns2"]) is False
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    # One warning per refused namespace, each with the command that erases it.
    assert len(warnings) == 2
    assert all("clean up with: loom namespaces-gc --org org" in w for w in warnings)


def test_a_failed_setup_whose_erasure_was_refused_is_erased_on_the_next_try(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    loom = FakeLoom(fail_on_upsert=True)
    loom.refuse_erase = True
    with pytest.raises(RuntimeError, match="upsert refused"):
        run.run(ts, _config(out), loom, model, [model], ledger)
    assert loom.erased == []
    loom.refuse_erase = False
    with pytest.raises(run.RunError, match="setup failed .its namespaces are erased"):
        run.run(ts, _config(out), loom, model, [model], ledger)
    assert len(loom.erased) == 3
    assert json.loads((out / "state.json").read_text())["erased"]


def test_documents_and_gold_rows_missing_their_keys_are_refused(tmp_path):
    root = _copy_example(tmp_path)
    for name, index, key, message in (
        ("documents.json", 0, "source_uri", "documents.json: an entry without"),
        ("documents.json", 1, "path", "documents.json: an entry without"),
        ("gold_rows.json", 0, "value", "gold_rows.json: an entry without"),
        ("gold_rows.json", 1, "document", "gold_rows.json: an entry without"),
    ):
        for f in ("documents.json", "gold_rows.json"):
            (root / f).write_text((EXAMPLE / f).read_text())
        rows = json.loads((root / name).read_text())
        del rows[index][key]
        (root / name).write_text(json.dumps(rows))
        with pytest.raises(TaskSetError, match=message):
            load(root)


def test_a_task_set_entry_that_is_not_an_object_is_refused(tmp_path):
    root = _copy_example(tmp_path)
    docs = json.loads((root / "documents.json").read_text())
    (root / "documents.json").write_text(json.dumps([*docs, "thumbnail-cache.md"]))
    with pytest.raises(TaskSetError, match="an entry is an object"):
        load(root)
    (root / "documents.json").write_text(json.dumps(docs))
    gold = json.loads((root / "gold_rows.json").read_text())
    (root / "gold_rows.json").write_text(json.dumps([*gold, "design/x"]))
    with pytest.raises(TaskSetError, match="an entry is an object"):
        load(root)
    (root / "gold_rows.json").write_text(json.dumps(gold))
    tasks = json.loads((root / "tasks.json").read_text())
    (root / "tasks.json").write_text(json.dumps({**tasks, "A": [*tasks["A"], "A9"]}))
    with pytest.raises(TaskSetError, match="an entry is an object"):
        load(root)
    (root / "tasks.json").write_text(json.dumps(tasks["A"]))
    with pytest.raises(TaskSetError, match="an object of task lists"):
        load(root)
    del tasks["A"][0]["id"]
    (root / "tasks.json").write_text(json.dumps(tasks))
    with pytest.raises(TaskSetError, match="tasks.json: an entry without"):
        load(root)


def test_a_rerun_with_other_flags_is_refused_before_it_erases_anything(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    out.mkdir()
    ledger = Ledger(out / "ledger.jsonl", 1.0)
    model = _fake_model(ledger)
    loom = FakeLoom(fail_on_upsert=True)
    loom.refuse_erase = True
    with pytest.raises(RuntimeError, match="upsert refused"):
        run.run(ts, _config(out), loom, model, [model], ledger)
    loom.refuse_erase = False
    with pytest.raises(run.RunError, match="other arms"):
        run.run(ts, _config(out, arms=[0]), loom, model, [model], ledger)
    assert loom.erased == []


def test_the_report_has_d_columns_and_prints_a_zero_bound_with_a_plus():
    m = {
        "arms": {6: {"D": {"precision": 0.5, "recall": 0.25, "misleading": 0.0}}},
        "comparisons": [
            {
                "treatment": 6,
                "reference": 4,
                "kind": "D",
                "metric": "recall",
                "n": 2,
                "clusters": 1,
                "delta": 0.1,
                "lb": -0.0,
                "ub": 0.2,
                "mde": 0.1,
            }
        ],
        "tokens": {},
        "treatment": {},
        "judge_stability": {},
        "unreadable": {},
    }
    text = score.report(m)
    assert "D precision | D recall | D misleading | D abstains" in text
    assert "| 6 | - | - | - | - | - | - | - | - | 50.0 | 25.0 | 0.0 | - | - | - |" in text
    assert "| 6 vs 4 | D | recall | 2 (1) | +10.0 | +0.0 .. +20.0 |" in text


def test_a_feature_task_is_asked_and_judged_with_its_own_prompts():
    task = {
        "id": "D9",
        "question": "How are entries evicted?",
        "gold_facts": ["whole segments go", "a hit moves up"],
    }
    system, user, cap = prompts.answer_prompt("D", task, "ctx", "the system")
    assert system == prompts.ANSWER_FEATURE.format(system="the system")
    assert user.endswith("Question: How are entries evicted?") and cap == 2500
    system, user = prompts.judge_prompt(
        "D", task, answer="answer", gold_rows="rows", cited="", rubric=[], answer_first=False
    )
    assert system == prompts.JUDGE_FEATURE
    assert "Gold facts:\n0. whole segments go\n1. a hit moves up" in user


def test_arm_6_document_citations_resolve_in_the_namespace_it_searched():
    ts = load(EXAMPLE)
    seen: list[str] = []

    class _Loom:
        def knowledge_get(self, ns, uri):
            seen.append(ns)
            return {"found": False}

    ns = {"kb": "kb-ns", "extract": "extract-ns", "gold": "gold-ns"}
    resolver = run.Resolver(ts, _Loom(), ns)
    cite = {"kind": "document", "cite": "doc://x#S"}
    resolver.resolve(cite, 6)
    resolver.resolve(cite, 2)
    assert seen == ["extract-ns", "kb-ns"]
