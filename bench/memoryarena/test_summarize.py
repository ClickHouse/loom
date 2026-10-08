"""Pure scoring of MemoryArena result files."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import summarize as sm


def _write(root: pathlib.Path, arm: str, task: str, flags: list[bool]) -> None:
    d = root / arm / task
    d.mkdir(parents=True)
    rows = [
        json.dumps({"query_id": i, "is_correct": ok, "time": 2.0}) for i, ok in enumerate(flags)
    ]
    (d / "result.jsonl").write_text("\n".join(rows))


def test_sr_ps_and_depth(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "loom-verbatim", "p1", [True, True, True])
    _write(tmp_path, "loom-verbatim", "p2", [True, False, True, False])
    _write(tmp_path, "long_context", "p1", [False, False])
    arms = {s.arm: s.as_dict() for s in sm.summarise(tmp_path)}
    lv = arms["loom-verbatim"]
    assert lv["tasks"] == 2 and lv["subtasks"] == 7
    assert lv["SR"] == 0.5, "one of two tasks fully correct"
    assert lv["PS"] == round((1.0 + 0.5) / 2, 3)
    assert lv["SR@k"] == {1: 1.0, 2: 0.5, 3: 1.0, 4: 0.0}
    assert lv["s_per_subtask"] == 2.0
    assert arms["long_context"]["SR"] == 0.0 and arms["long_context"]["PS"] == 0.0


def test_task_rows_are_ordered_by_query_id() -> None:
    ok, frac, depth, secs = sm.summarise_task(
        [
            {"query_id": 1, "is_correct": False, "time": 1},
            {"query_id": 0, "is_correct": True, "time": 1},
        ]
    )
    assert (ok, frac, depth, secs) == (False, 0.5, [True, False], 2.0)


def test_torn_line_is_skipped(tmp_path: pathlib.Path) -> None:
    d = tmp_path / "arm" / "t"
    d.mkdir(parents=True)
    good = json.dumps({"query_id": 0, "is_correct": True, "time": 1})
    (d / "result.jsonl").write_text(good + '\n{"query_id": 1, "is_corr')
    s = sm.summarise(tmp_path)[0].as_dict()
    assert s["subtasks"] == 1 and s["SR"] == 1.0


def test_floor_reports_all_arms_wrong_and_sensitive_accuracy(tmp_path):
    from bench.memoryarena.summarize import floor, summarise

    rows_a = [
        {"query_id": 0, "is_correct": True},
        {"query_id": 1, "is_correct": False},
        {"query_id": 2, "is_correct": False},
    ]
    rows_b = [
        {"query_id": 0, "is_correct": False},
        {"query_id": 1, "is_correct": True},
        {"query_id": 2, "is_correct": False},
    ]
    for arm, rows in (("a", rows_a), ("b", rows_b)):
        d = tmp_path / arm / "paper1"
        d.mkdir(parents=True)
        (d / "result.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    fl = floor(summarise(tmp_path))
    assert fl["common_subtasks"] == 3 and fl["all_arms_wrong"] == 1
    assert fl["memory_sensitive"] == 2
    assert fl["accuracy_on_sensitive"] == {"a": 0.5, "b": 0.5}
