"""The one-switch A/B bench's arithmetic and the plugin's run hook."""

from __future__ import annotations


def test_ab_bench_summary_counts_recall_and_disagreements() -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "search_switch_ab",
        Path(__file__).resolve().parents[2] / "bench" / "search_switch_ab.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert mod.rank_of(["a", "b", "c"], "b") == 2 and mod.rank_of(["a"], "z") == 0
    rows = [
        {"qid": "1", "rank_off": 1, "rank_on": 1, "added": 0},
        {"qid": "2", "rank_off": 0, "rank_on": 3, "added": 2},  # the switch found it
        {"qid": "3", "rank_off": 4, "rank_on": 7, "added": 1},  # the switch pushed it down
        {"qid": "4", "rank_off": 0, "rank_on": 0, "added": 0},
    ]
    s = mod.summarise(rows)
    assert s["n"] == 4 and s["off"]["recall@5"] == 0.5 and s["on"]["recall@5"] == 0.5
    assert s["on"]["recall@10"] == 0.75 and s["off"]["recall@10"] == 0.5
    assert s["changed"] == 2 and s["improved"] == 1 and s["regressed"] == 1
    assert s["rows_added_total"] == 3 and s["questions_with_added_rows"] == 2
    assert [d["qid"] for d in s["disagreements"]] == ["2", "3"]


def test_plugin_manifest_version_moved_with_the_run_hook() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "integrations" / "claude-code-plugin"
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    major, minor, _ = (int(x) for x in manifest["version"].split("."))
    assert (major, minor) >= (0, 3), (
        "loom-run.mjs shipped without a version bump; clients never updated"
    )
    hooks = json.loads((root / "hooks" / "hooks.json").read_text())
    assert any(
        "loom-run.mjs" in h["args"][0]
        for grp in hooks["hooks"]["UserPromptSubmit"]
        for h in grp["hooks"]
    )
