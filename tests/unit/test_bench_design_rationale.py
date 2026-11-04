"""The design-rationale bench without a server or a provider: the task-set loader, the
detectors and citation parser, the scoring and statistics, the spend cap, and the
synthetic example set end to end over a fake Loom and a fake model."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bench.design_rationale import detectors, run, score
from bench.design_rationale.__main__ import main
from bench.design_rationale.llm import CapReached, Ledger, Model, Reply
from bench.design_rationale.taskset import TaskSetError, load

EXAMPLE = Path(__file__).resolve().parents[2] / "bench" / "design_rationale" / "example"


# ---------------------------------------------------------------- task set


def test_the_example_set_loads_with_its_revision_and_kinds():
    ts = load(EXAMPLE)
    assert {k: len(v) for k, v in ts.tasks.items()} == {"A": 3, "B": 3, "C": 2}
    assert ts.documents["upload-retries-v2"].revision_of == "upload-retries"
    # The key written again after the revision: two writes, the later one current.
    writes = [r for r in ts.gold_writes if r.memory_key == "design/upload/retried-statuses"]
    assert [r.after_revision for r in writes] == [False, True]
    assert ts.gold["design/upload/retried-statuses"].after_revision
    assert ts.task_paths(ts.tasks["B"][0]) == ["src/cache/segmented_lru.py"]
    assert [d.id for d in ts.current_documents()] == ["thumbnail-cache", "upload-retries-v2"]


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
    setting = {"type": "setting_default_changed", "setting": "thumb_cache_bytes", "from": "512"}
    touched = {"type": "file_touched", "paths": ["src/cache/config.py"]}
    assert detectors.run_detector(removed, files) is True
    assert detectors.run_detector(added, files) is False
    assert detectors.run_detector(setting, files) is True
    assert detectors.run_detector(touched, files) is True
    assert detectors.run_detector({"type": "judge", "rubric": "r"}, files) is None


def test_an_unchanged_default_does_not_fire_the_setting_detector():
    files = detectors.diff_files("+++ b/a.py\n+thumb_cache_bytes = 512 * MiB  # documented\n")
    setting = {"type": "setting_default_changed", "setting": "thumb_cache_bytes", "from": "512"}
    assert detectors.run_detector(setting, files) is False


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


def test_a_section_is_found_by_its_last_breadcrumb():
    content = "# T\n\n## Backoff\n\nbody"
    assert detectors.section_text(content, "T > Backoff").startswith("Backoff")
    assert detectors.section_text(content, "T > Missing") is None


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
    # The planning figure the evaluation design quotes: about 25 points at n = 15.
    assert round(score.planning_mde(15), 2) == 0.25


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


def test_a_run_without_a_cap_or_a_priced_model_is_refused(tmp_path, capsys):
    out = tmp_path / "out"
    base = ["run", str(EXAMPLE), "--out", str(out), "--base-url", "http://127.0.0.1:1"]
    assert (
        main(
            [
                *base,
                "--reader-model",
                "gpt-4o-mini",
                "--judge-model",
                "gpt-4o-mini",
                "--extract-model",
                "gpt-4o-mini",
            ]
        )
        == 2
    )
    assert "--max-usd" in capsys.readouterr().err
    assert (
        main(
            [*base, "--reader-model", "unlisted", "--judge-model", "gpt-4o-mini", "--max-usd", "1"]
        )
        == 2
    )
    assert "no price" in capsys.readouterr().err


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
        extract_model="gpt-4o-mini",
    )
    p = run.plan(ts, cfg)
    assert p["usage"]["answer"]["calls"] == 8 * 2 * 2
    assert p["usage"]["judge"]["calls"] == 8 * 2 * 2 * 2
    assert p["usage"]["extract"]["calls"] == 0  # arm 4 not run
    run.print_plan(p)
    assert "worst case" in capsys.readouterr().out


# ---------------------------------------------------------------- end to end


class FakeLoom:
    """The ops the harness calls, over dicts. memory.for_file serves a row whose files
    hold the path; loom_search serves rows sharing a word with the query."""

    def __init__(self):
        self.docs: dict[tuple[str, str], list[str]] = {}
        self.rows: dict[tuple[str, str], dict] = {}
        self.writes: list[tuple[str, str]] = []
        self.erased: list[str] = []

    def bind(self, namespace, org):
        pass

    def register(self, namespace, description, keep):
        pass

    def erase(self, namespaces):
        self.erased += namespaces

    def upsert_document(self, namespace, source_uri, title, content):
        self.docs.setdefault((namespace, source_uri), []).append(content)

    def write_gold(self, namespace, row, source_resource_id):
        self.writes.append((row.memory_key, row.value))
        self.rows[(namespace, row.memory_key)] = {
            "value": row.value,
            "files": row.files,
            "source": source_resource_id,
        }

    def extract(self, namespace, source_uri, repo=""):
        content = self.docs[(namespace, source_uri)][-1]
        key = "decision:" + str(abs(hash(content)) % 10**6)
        self.rows[(namespace, key)] = {
            "value": content.split("\n\n")[2][:200],
            "files": [],
            "source": f"knowledge:{namespace}/{source_uri}",
        }
        return 0, f"{source_uri}: 1 window(s), 1 decision(s)\n- recorded {key}\n"

    def for_file(self, namespace, path, token_budget=300):
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


def _fake_model(ledger):
    def complete(system, user, max_tokens):
        if "Return JSON only" in system:
            cites = re.findall(r"^- (\S+): RESOLVES", user, re.M)
            verdict = {
                "claims": [
                    {
                        "verdict": "correct",
                        "gold": [0],
                        "cites": cites,
                        "cites_support": [True] * len(cites),
                    }
                ]
                if cites
                else [],
                "gold_covered": [0] if cites else [],
                "abstained": not cites,
                "forbidden_proposed": [] if cites else [0],
                "cites": cites,
                "cites_support": [True] * len(cites),
                "stale_served": False,
            }
            return Reply(json.dumps(verdict), 10, 10)
        keys = re.findall(r"\[((?:design/|decision:)[^\]]+)\]", user)
        if keys:
            return Reply(f"- because it is recorded [{keys[0]}]", 10, 10)
        return Reply("No recorded decision covers this.", 10, 10)

    return Model("gpt-4o-mini", complete, ledger)


def test_the_example_set_runs_end_to_end_over_a_fake_loom(tmp_path):
    ts = load(EXAMPLE)
    out = tmp_path / "run"
    cfg = run.Config(
        out=out,
        arms=[0, 1, 2, 3, 4],
        k=2,
        reader="gpt-4o-mini",
        judges=["gpt-4o-mini"],
        judge_orders=2,
        judge_repeats=1,
        max_usd=1.0,
        extract_model="gpt-4o-mini",
    )
    loom = FakeLoom()
    ledger = Ledger(out / "ledger.jsonl", cfg.max_usd)
    out.mkdir()
    model = _fake_model(ledger)
    run.run(ts, cfg, loom, model, [model], ledger)

    # Gold rows go to the gold namespace only, the revised row after the first one.
    assert [k for k, _ in loom.writes].count("design/upload/retried-statuses") == 2
    assert not any(ns.endswith("-extract") for ns, k in loom.rows if k.startswith("design/"))
    # The revision was extracted again after it was ingested; the run erased its namespaces.
    state = json.loads((out / "state.json").read_text())
    assert set(state["extraction"]) == {"thumbnail-cache", "upload-retries", "upload-retries-v2"}
    assert len(loom.erased) == 3
    assert any(r["estimated"] for r in ledger.rows())

    judged = json.loads((out / "judged.json").read_text())
    assert len(judged) == 8 * 5 * 2
    assert all(len(e["passes"]) == 2 for e in judged.values())
    cited = [c for e in judged.values() for c in e["citations"]]
    assert cited and all(c["resolves"] for c in cited if c["kind"] == "memory")

    m = score.score(ts, out)
    assert m["treatment"]["arm3_tasks_served_a_gold_row"].split("/")[0] != "0"
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

    # A second run finds every stage done and calls nothing.
    calls_before = len(ledger.rows())
    run.run(ts, cfg, FakeLoom(), model, [model], ledger)
    extract_rows = [r for r in ledger.rows()[calls_before:] if r["stage"] != "extract"]
    assert extract_rows == []
