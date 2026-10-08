"""bench/matrix.py — the sweep passes its dataset through, reports what each
config ran on, and exits non-zero when a config failed."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from bench import matrix


def _csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["recalled", "judge_label", "search_latency_ms"])
        w.writeheader()
        w.writerows(rows)


def test_summarize_csv_counts_recall_judge_and_latency(tmp_path: Path) -> None:
    out = tmp_path / "c.csv"
    _csv(
        out,
        [
            {"recalled": "1", "judge_label": "CORRECT", "search_latency_ms": "10"},
            {"recalled": "0", "judge_label": "WRONG", "search_latency_ms": "30"},
        ],
    )
    cfg = matrix.CONFIGS[0]
    r = matrix._summarize_csv("c", cfg, out)
    assert (r.n, r.recall_n, r.judge_n, r.p50_latency_ms) == (2, 1, 1, 10)
    assert not r.failed
    assert matrix._summarize_csv("c", cfg, tmp_path / "missing.csv", 0).failed
    assert matrix._summarize_csv("c", cfg, out, 3).failed


def test_main_passes_the_dataset_and_exits_one_on_a_failed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cmds: list[list[str]] = []

    def fake_call(cmd: list[str]) -> int:
        cmds.append(cmd)
        out = Path(cmd[cmd.index("--out") + 1])
        _csv(out, [{"recalled": "1", "judge_label": "CORRECT", "search_latency_ms": "5"}])
        return 0 if len(cmds) > 1 else 1

    monkeypatch.setattr(matrix.subprocess, "call", fake_call)
    rc = matrix.main(["--dataset", "d.json", "--no-answer-judge", "--out", "m.md"])
    assert rc == 1
    assert all(c[c.index("--dataset") + 1] == "d.json" for c in cmds)
    assert all("--answer-judge" not in c and "--shuffle" in c for c in cmds)
    assert "`d.json`" in (tmp_path / "m.md").read_text()
