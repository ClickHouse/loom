"""Schema checks for the code-plane bench fixtures that no bench run loads in CI.

The code-intel question files are checked by bench/test_code_intel_qa.py (every
file through the loader and --dry); the op-planning and skill-eval fixtures by
their own tests. These are the rest: the embedder calibration probes (read at
runtime by code.calibrate) and the synthetic plane-ablation fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

from bench import code_plane_ablation as abl

FIXTURES = Path(__file__).resolve().parents[2] / "bench" / "fixtures"


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            rows.append(json.loads(line))
    return rows


def test_calibration_probes_have_the_shape_code_calibrate_reads() -> None:
    rows = _jsonl(FIXTURES / "code_calibration_probes.jsonl")
    assert all(set(r) <= {"query", "positive", "distractor"} for r in rows)
    assert all(isinstance(r["query"], str) and r["query"].strip() for r in rows)
    assert all(isinstance(r["positive"], str) and r["positive"].strip() for r in rows)
    queries = [r["query"] for r in rows]
    assert len(set(queries)) == len(queries), "the query is the probe's identity"
    # code.calibrate refuses a corpus below 5 positives / 3 distractors
    assert len(rows) >= 5 and sum(1 for r in rows if r.get("distractor")) >= 3
    # a positive and a distractor are chunk texts led by their path
    for r in rows:
        for text in (r["positive"], r.get("distractor") or ""):
            if text:
                assert text.split(" ", 1)[0].count("/") >= 1, r["query"]


def test_plane_ablation_fixtures_load_and_are_synthetic() -> None:
    d = FIXTURES / "code_plane_ablation"
    tasks, stats = abl.load_tasks(d / "tasks.jsonl")
    assert [t.id for t in tasks] == ["pr900001"] and stats == {"scanned": 1, "kept": 1}
    for arm in ("A", "C"):
        rows = _jsonl(d / f"transcript_arm_{arm}.jsonl")
        assert rows[0]["type"] == "system" and rows[0]["subtype"] == "init"
        assert rows[-1]["type"] == "result"
        assert {r["type"] for r in rows} <= {"system", "assistant", "user", "result"}
        ids = [
            b["id"]
            for r in rows
            if r["type"] == "assistant"
            for b in r["message"]["content"]
            if b.get("type") == "tool_use"
        ]
        assert len(ids) == len(set(ids)), "tool_use ids pair calls with their results"
        assert "/home/" not in (d / f"transcript_arm_{arm}.jsonl").read_text()
