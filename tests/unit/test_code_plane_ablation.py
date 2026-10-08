"""bench/code_plane_ablation.py — the scorer, redaction, leak audit and
paired statistics of the agent-level code-plane ablation, CH-free and
agent-free: the fixture transcripts stand in for `claude -p`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.agent_traj.claude_code_driver import (
    Cell,
    ClaudeCliDriver,
    DriverConfig,
    DryRunDriver,
    build_shunt_only_plugin,
    redact_argv,
)
from bench.code_plane_ablation import (
    FIXTURE_DIR,
    CellScore,
    arm_summary,
    compare_arms,
    hunk_identifiers,
    leak_reasons_for,
    load_tasks,
    paired_bootstrap_lb,
    parse_bash_reads,
    parse_files_line,
    parse_hunks,
    parse_transcript,
    per_task_means,
    redact,
    rel_path,
    score_cell,
    strip_pr_template,
    wilcoxon_signed_rank,
    write_exclusions,
)

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = ROOT / "integrations" / "claude-code-plugin"


@pytest.fixture
def task():
    tasks, stats = load_tasks(FIXTURE_DIR / "tasks.jsonl")
    assert stats["kept"] == 1
    return tasks[0]


# -- redaction + leak audit ---------------------------------------------------


def test_redact_hits_identifier_boundaries_case_insensitively() -> None:
    text = "Rename getCodecDesc to getCodecDescription in CompressionFactory; keep codec."
    out, hit = redact(text, {"getCodecDesc", "CompressionFactory", "codec"})
    # identifier boundaries: getCodecDescription survives, getCodecDesc does not
    assert out == "Rename [redacted] to getCodecDescription in [redacted]; keep [redacted]."
    assert hit == ["CompressionFactory", "codec", "getCodecDesc"]


def test_leak_reasons_are_substring_and_fail_closed() -> None:
    gold = ["src/Functions/FunctionBinaryArithmetic.h", "src/Common/IO.cpp"]
    assert leak_reasons_for("fix functionbinaryarithmetic overflow", gold) == [
        "stem:functionbinaryarithmetic"
    ]
    assert leak_reasons_for("arithmetic functions overflow", gold) == []
    # a 2-letter stem never counts: "IO" would flag every text
    assert leak_reasons_for("io error", ["src/Common/IO.cpp"]) == []


def test_strip_pr_template_drops_comments_and_checklists() -> None:
    body = (
        "<!-- template -->\nChangelog category (leave one):\n- Bug Fix\n\n"
        "- [x] ran tests\n\nThe fallback throws on an empty description.\n\n\n\nMore."
    )
    out = strip_pr_template(body)
    assert "template" not in out and "Changelog" not in out and "[x]" not in out
    assert out.startswith("- Bug Fix") and out.endswith("More.")
    assert "\n\n\n" not in out


def test_hunk_identifiers_and_hunk_spans() -> None:
    diff = (
        "diff --git a/src/A/Factory.cpp b/src/A/Factory.cpp\n"
        "--- a/src/A/Factory.cpp\n+++ b/src/A/Factory.cpp\n"
        "@@ -120,9 +120,12 @@ String CompressionCodecFactory::getCodecDesc(const ASTPtr & ast)\n"
        "-    return old_value;\n+    return fallbackDescription(ast);\n"
        "@@ -140,0 +144,2 @@ void other()\n+    int inserted_here = 1;\n"
        "--- a/src/A/Factory.h\n+++ b/src/A/Factory.h\n@@ -44 +44 @@\n-x\n+y\n"
    )
    ids = hunk_identifiers(diff)
    assert {"CompressionCodecFactory", "getCodecDesc", "fallbackDescription"} <= ids
    assert "String" not in ids and "return" not in ids
    spans = parse_hunks(diff)
    assert spans["src/A/Factory.cpp"] == [[120, 128], [140, 140]]
    assert spans["src/A/Factory.h"] == [[44, 44]]


# -- transcript parsing ------------------------------------------------------


def test_rel_path_strips_workdir_and_finds_repo_roots() -> None:
    assert rel_path("/work/ch/src/A.cpp", "/work/ch") == "src/A.cpp"
    assert rel_path("/elsewhere/repo/src/B.h", "/work/ch") == "src/B.h"
    assert rel_path("/etc/passwd", "/work/ch") is None
    assert rel_path("src/C.cpp", None) == "src/C.cpp"


def test_parse_bash_reads_covers_the_read_shapes() -> None:
    assert parse_bash_reads("cat src/A.cpp") == [
        {"path": "src/A.cpp", "start": 1, "end": 0, "kind": "cat"}
    ]
    assert parse_bash_reads("sed -n '10,20p' src/A.cpp")[0] == {
        "path": "src/A.cpp",
        "start": 10,
        "end": 20,
        "kind": "sed",
    }
    assert parse_bash_reads("head -n 40 src/A.cpp")[0] == {
        "path": "src/A.cpp",
        "start": 1,
        "end": 40,
        "kind": "head",
    }
    assert parse_bash_reads("head -25 src/A.cpp")[0]["end"] == 25
    assert parse_bash_reads("grep -rn foo src/")[0]["kind"] == "grep_tree"
    assert parse_bash_reads("grep -n foo src/A.cpp")[0]["kind"] == "grep_file"
    assert parse_bash_reads("rg foo")[0] == {"path": ".", "start": 0, "end": 0, "kind": "grep_tree"}
    assert parse_bash_reads("ls -la && git status") == []
    assert parse_bash_reads("cat src/A.cpp | head -20")[0]["kind"] == "cat"


def test_parse_files_line_takes_the_last_answer_and_keeps_paths_only() -> None:
    text = "FILES: src/x.cpp\n...later...\nFILES: `src/A.cpp`, src/B.h, and src/C.cpp."
    assert parse_files_line(text) == ["src/A.cpp", "src/B.h", "src/C.cpp"]
    assert parse_files_line("no answer") == []


def test_parse_transcript_arm_a_spans_and_cost() -> None:
    tr = parse_transcript(FIXTURE_DIR / "transcript_arm_A.jsonl")
    assert tr.tool_calls == 4 and tr.loom_calls == 0 and tr.denied_calls == 0
    reads = [e for e in tr.events if e.tool == "Read"]
    assert [(e.path, e.start, e.end) for e in reads] == [
        ("src/Parsers/ParserCodec.cpp", 1, 400),
        ("src/Storages/ColumnsDescription.cpp", 80, 99),
        ("src/Compression/CompressionFactory.cpp", 1, 600),
    ]
    grep_hits = [(e.path, e.start) for e in tr.events if e.tool == "Grep" and e.start > 0]
    assert ("src/Compression/CompressionFactory.cpp", 124) in grep_hits
    assert tr.cost_usd == 0.42 and tr.plugins == []
    assert tr.tokens_in + tr.tokens_out + tr.tokens_cache == 26300
    assert tr.answered_files[0] == "src/Compression/CompressionFactory.cpp"


def test_parse_transcript_arm_c_denial_and_loom_provenance() -> None:
    tr = parse_transcript(FIXTURE_DIR / "transcript_arm_C.jsonl")
    assert tr.denied_calls == 1 and tr.loom_calls == 1
    assert tr.plugins == ["loom-shunt-only"] and tr.mcp_servers == ["loom"]
    denied = [e for e in tr.events if e.denied]
    assert len(denied) == 1 and denied[0].path == "src/Compression/CompressionFactory.cpp"
    loom = [e for e in tr.events if e.provenance == "loom"]
    assert [e.path for e in loom] == [
        "src/Compression/CompressionFactory.cpp",
        "src/Compression/CompressionFactory.h",
    ]


# -- scoring -----------------------------------------------------------------


def test_score_cell_view_b_ignores_loom_only_provenance(task) -> None:
    tr = parse_transcript(FIXTURE_DIR / "transcript_arm_C.jsonl")
    sc = score_cell(task, tr, arm="C", seed=1, shunt_log_lines=2)
    # the .h was named by loom AND grepped by the agent -> View B credits it;
    # a path only loom named would not appear in view_b_files
    assert sc.view_b_files == [
        "src/Compression/CompressionFactory.cpp",
        "src/Compression/CompressionFactory.h",
    ]
    assert sc.view_a_files[:2] == sc.view_b_files
    assert sc.acc5_view_b == 1.0 and sc.first_gold_rank_b == 1 and sc.hook_fired
    assert sc.file_precision == 1.0 and sc.file_recall == 1.0
    # the targeted read 118-147 overlaps the padded gold hunks 117-131 and 137-144
    # on 14 + 8 of its 30 lines
    assert sc.line_precision_event == pytest.approx(22 / 30)
    assert sc.wasted_read_rate == 0.0 and not sc.excluded


def test_score_cell_arm_a_counts_the_wasted_whole_file_read(task) -> None:
    tr = parse_transcript(FIXTURE_DIR / "transcript_arm_A.jsonl")
    sc = score_cell(task, tr, arm="A", seed=1)
    assert sc.first_gold_rank_b == 3 and sc.acc5_view_b == 1.0
    assert sc.file_precision == pytest.approx(2 / 3)  # parser file is not gold or caller
    assert sc.wasted_read_rate == pytest.approx(1 / 3)
    assert sc.line_precision_event < 0.05  # 600-line whole-file read over a 12-line hunk
    assert sc.line_coverage == pytest.approx(23 / 30)  # the .h hunk was never read
    assert not sc.hook_fired and sc.plugins == []


def test_score_cell_excludes_network_and_history_cells(task, tmp_path) -> None:
    rows = [
        {"type": "system", "subtype": "init", "cwd": "/work/ch", "plugins": [], "mcp_servers": []},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "b1",
                        "name": "Bash",
                        "input": {
                            "command": "git log -3 --oneline && LOOM_CODE_SHUNT=off curl http://x"
                        },
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "b1", "content": "abc"}]
            },
        },
        {
            "type": "result",
            "total_cost_usd": 0.1,
            "num_turns": 1,
            "usage": {},
            "permission_denials": [],
        },
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sc = score_cell(task, parse_transcript(p), arm="A", seed=1)
    assert sc.excluded and sc.exclusion_reasons == ["git_history", "network"]
    assert sc.bypass_calls == 1
    n = write_exclusions([sc], tmp_path / "exclusions.csv")
    assert n == 1 and "git_history;network" in (tmp_path / "exclusions.csv").read_text()


def test_symbol_resolver_yields_symbol_precision(task) -> None:
    tr = parse_transcript(FIXTURE_DIR / "transcript_arm_C.jsonl")

    def resolver(locs):
        return {
            (p, ln): ("sym_get" if 118 <= ln <= 147 or ln == 120 or ln == 140 else "sym_other")
            for p, ln in locs
            if p.endswith(".cpp")
        }

    sc = score_cell(task, tr, arm="C", seed=1, symbol_resolver=resolver)
    assert sc.symbol_precision == 1.0 and sc.symbol_coverage == 1.0


# -- statistics --------------------------------------------------------------


def test_wilcoxon_exact_small_n_and_ties() -> None:
    p, n = wilcoxon_signed_rank([1, 2, 3, 4, 5, 6], [0, 0, 0, 0, 0, 0])
    assert n == 6 and p == pytest.approx(2 / 64)  # all six signs positive: 2 * 1/64
    p_same, n0 = wilcoxon_signed_rank([1, 1], [1, 1])
    assert (p_same, n0) == (1.0, 0)
    p_mixed, _ = wilcoxon_signed_rank([1, 0, 1, 0], [0, 1, 0, 1])
    assert p_mixed == 1.0


def test_wilcoxon_normal_approximation_large_n() -> None:
    x = [float(i) for i in range(30)]
    y = [v - 1.0 for v in x]
    p, n = wilcoxon_signed_rank(x, y)
    assert n == 30 and p < 1e-4


def test_paired_bootstrap_lower_bound_sign() -> None:
    d, lo, hi = paired_bootstrap_lb([1, 1, 1, 1, 1, 0.9], [0, 0, 0, 0, 0, 0], iters=500)
    assert d == pytest.approx(5.9 / 6) and lo > 0.8 and hi <= 1.0
    assert paired_bootstrap_lb([], []) == (0.0, 0.0, 0.0)


def _cell(task_id: str, arm: str, acc: float, calls: int, excluded: bool = False) -> CellScore:
    return CellScore(
        task_id=task_id,
        arm=arm,
        seed=1,
        excluded=excluded,
        exclusion_reasons=[],
        fix_in_index=True,
        view_b_files=[],
        view_a_files=[],
        acc5_view_b=acc,
        acc5_view_a=acc,
        acc5_answered=acc,
        first_gold_rank_b=None,
        file_precision=None,
        file_recall=0.0,
        line_precision_event=None,
        line_precision_union=None,
        line_coverage=None,
        wasted_read_rate=None,
        redundancy=None,
        symbol_precision=None,
        symbol_coverage=None,
        read_events=0,
        tool_calls=calls,
        loom_calls=0,
        denied_calls=0,
        bypass_calls=0,
        result_bytes=0,
        tokens_total=0,
        cost_usd=0.5,
        num_turns=1,
        plugins=[],
        mcp_servers=[],
        hook_fired=arm != "A",
    )


def test_summary_and_paired_comparison_skip_excluded_cells() -> None:
    cells = [
        _cell("t1", "A", 0.0, 12),
        _cell("t1", "C", 1.0, 6),
        _cell("t2", "A", 1.0, 10),
        _cell("t2", "C", 1.0, 5),
        _cell("t3", "A", 0.0, 9),
        _cell("t3", "C", 1.0, 7),
        _cell("t4", "A", 1.0, 8, excluded=True),
        _cell("t4", "C", 0.0, 3),
    ]
    summary = arm_summary(cells)
    assert summary["A"]["cells"] == 4 and summary["A"]["excluded"] == 1
    assert summary["A"]["acc5_view_b"] == pytest.approx(1 / 3)
    assert summary["C"]["usd_per_solved"] == pytest.approx(2.0 / 3)
    assert summary["A"]["hook_fired_share"] is None and summary["C"]["hook_fired_share"] == 1.0
    means = per_task_means(cells)
    assert set(means["A"]) == {"t1", "t2", "t3"} and set(means["C"]) == {"t1", "t2", "t3", "t4"}
    cmp = compare_arms(means, "C", "A", "tool_calls")
    assert cmp is not None and cmp["n"] == 3 and cmp["delta"] == pytest.approx(-13 / 3)
    assert cmp["ub"] < 0  # every pair fell


# -- driver composition (no agent spawned) -------------------------------------


def test_dry_run_driver_composes_arms_without_spending(tmp_path) -> None:
    cfg = DriverConfig(
        plugin_src=PLUGIN_SRC,
        loom_url="http://127.0.0.1:7859",
        loom_token="secret",
        shunt_repo="clickhouse/clickhouse",
        read_cap=150,
    )
    cli = ClaudeCliDriver(cfg, tmp_path / "run")
    dry = DryRunDriver(cli)
    metas = {}
    for arm in ("A", "B", "C", "D"):
        cell = Cell(
            task_id="t",
            arm=arm,
            seed=1,
            prompt="p",
            system_prompt="s" if arm in "CD" else "",
            base_sha="0" * 40,
            cell_dir=tmp_path / "run" / "cells" / "t" / arm / "s1",
            workdir=tmp_path,
        )
        metas[arm] = dry.run(cell)
    a, b, c, d = (metas[k]["argv"] for k in "ABCD")
    assert "--bare" not in a and "--strict-mcp-config" in a and "--plugin-dir" not in a
    assert "--plugin-dir" in b and "--append-system-prompt" not in b
    assert "--plugin-dir" in c and "--append-system-prompt" in c
    assert "secret" not in json.dumps(metas)  # the bearer never reaches argv or the meta
    assert (
        metas["A"]["env"]["LOOM_CODE_SHUNT"] == "off" and "LOOM_BASE_URL" not in metas["A"]["env"]
    )
    assert (
        metas["B"]["env"]["LOOM_SHUNT_REPORT"] == "off" and metas["B"]["env"]["LOOM_USAGE"] == "off"
    )
    assert metas["D"]["env"]["BENCH_READ_CAP"] == "150"
    mcp_a = json.loads((tmp_path / "run" / "mcp_A.json").read_text())
    mcp_c = json.loads((tmp_path / "run" / "mcp_C.json").read_text())
    assert mcp_a == {"mcpServers": {}}
    assert mcp_c["mcpServers"]["loom"]["url"] == "http://127.0.0.1:7859/mcp"
    hooks_b = json.loads(
        (tmp_path / "run" / "plugins" / "arm_B" / "hooks" / "hooks.json").read_text()
    )
    assert list(hooks_b["hooks"]) == ["PreToolUse"] and len(hooks_b["hooks"]["PreToolUse"]) == 1
    hooks_d = json.loads(
        (tmp_path / "run" / "plugins" / "arm_D" / "hooks" / "hooks.json").read_text()
    )
    assert len(hooks_d["hooks"]["PreToolUse"]) == 2
    assert redact_argv(["claude", "-p", "long prompt", "--model", "x"]) == [
        "claude",
        "-p",
        "<prompt>",
        "--model",
        "x",
    ]


def test_shunt_only_plugin_copies_lib_and_one_hook(tmp_path) -> None:
    dest = build_shunt_only_plugin(PLUGIN_SRC, tmp_path / "p")
    assert (dest / "lib" / "code-shunt.mjs").exists()
    assert (dest / "hooks" / "loom-code-shunt.mjs").exists()
    assert not (dest / "hooks" / "loom-capture.mjs").exists()
    manifest = json.loads((dest / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "loom-shunt-only"


def test_api_key_source_is_read_from_the_init_row(tmp_path) -> None:
    from bench.agent_traj.claude_code_driver import api_key_source

    tp = tmp_path / "t.jsonl"
    tp.write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "ANTHROPIC_API_KEY",
                "cwd": "/w",
                "plugins": [],
                "mcp_servers": [],
            }
        )
        + "\n"
        + json.dumps({"type": "result", "total_cost_usd": 0.5, "num_turns": 1, "usage": {}})
        + "\n"
    )
    assert api_key_source(tp) == "ANTHROPIC_API_KEY"
    tr = parse_transcript(tp, workdir="/w")
    assert tr.api_key_source == "ANTHROPIC_API_KEY" and tr.cost_usd == 0.5
    # the synthetic fixture predates the field: empty, never a crash
    tr_c = parse_transcript(FIXTURE_DIR / "transcript_arm_C.jsonl", workdir="/work/ch")
    assert tr_c.api_key_source == ""
    assert api_key_source(tmp_path / "missing.jsonl") == ""


def test_arm_e_points_at_its_own_server_and_parse_arm_urls_rejects_junk(tmp_path) -> None:
    from bench.code_plane_ablation import build_system_prompt, parse_arm_urls

    assert parse_arm_urls(["E=http://127.0.0.1:7862", " D = http://x "]) == {
        "E": "http://127.0.0.1:7862",
        "D": "http://x",
    }
    with pytest.raises(ValueError):
        parse_arm_urls(["E"])
    with pytest.raises(ValueError):
        parse_arm_urls(["=http://x"])
    cfg = DriverConfig(
        plugin_src=PLUGIN_SRC,
        loom_url="http://127.0.0.1:7861",
        loom_token="secret",
        arm_urls={"E": "http://127.0.0.1:7862"},
    )
    cli = ClaudeCliDriver(cfg, tmp_path / "run")
    dry = DryRunDriver(cli)
    metas = {}
    for arm in ("C", "E"):
        cell = Cell(
            task_id="t",
            arm=arm,
            seed=1,
            prompt="p",
            system_prompt="s",
            base_sha="0" * 40,
            cell_dir=tmp_path / "run" / "cells" / "t" / arm / "s1",
            workdir=tmp_path,
        )
        metas[arm] = dry.run(cell)
    mcp_c = json.loads((tmp_path / "run" / "mcp_C.json").read_text())
    mcp_e = json.loads((tmp_path / "run" / "mcp_E.json").read_text())
    assert mcp_c["mcpServers"]["loom"]["url"] == "http://127.0.0.1:7861/mcp"
    assert mcp_e["mcpServers"]["loom"]["url"] == "http://127.0.0.1:7862/mcp"
    assert metas["C"]["env"]["LOOM_BASE_URL"] == "http://127.0.0.1:7861"
    assert metas["E"]["env"]["LOOM_BASE_URL"] == "http://127.0.0.1:7862"
    # E is C with another server: same one-hook plugin, no read cap, same system prompt
    hooks_e = json.loads(
        (tmp_path / "run" / "plugins" / "arm_E" / "hooks" / "hooks.json").read_text()
    )
    assert len(hooks_e["hooks"]["PreToolUse"]) == 1 and "BENCH_READ_CAP" not in metas["E"]["env"]
    assert build_system_prompt("E", "ns", "abc") == build_system_prompt("C", "ns", "abc") != ""


def test_arm_env_sets_one_arms_env_only(tmp_path) -> None:
    from bench.code_plane_ablation import parse_arm_env

    assert parse_arm_env(["F=LOOM_SHUNT_READ_CAP=200", "F=X=a=b"]) == {
        "F": {"LOOM_SHUNT_READ_CAP": "200", "X": "a=b"}
    }
    for junk in (["F"], ["F=X"], ["=X=1"], ["F==1"]):
        with pytest.raises(ValueError):
            parse_arm_env(junk)
    cfg = DriverConfig(
        plugin_src=PLUGIN_SRC,
        loom_url="http://127.0.0.1:7861",
        loom_token="secret",
        arm_env={"F": {"LOOM_SHUNT_READ_CAP": "200"}},
    )
    dry = DryRunDriver(ClaudeCliDriver(cfg, tmp_path / "run"))
    metas = {}
    for arm in ("C", "F"):
        cell = Cell(
            task_id="t",
            arm=arm,
            seed=1,
            prompt="p",
            system_prompt="s",
            base_sha="0" * 40,
            cell_dir=tmp_path / "run" / "cells" / "t" / arm / "s1",
            workdir=tmp_path,
        )
        metas[arm] = dry.run(cell)
    # F is C plus the plugin's read cap: same server, one-hook plugin, the cap on
    assert "LOOM_SHUNT_READ_CAP" not in metas["C"]["env"]
    assert metas["F"]["env"]["LOOM_SHUNT_READ_CAP"] == "200"
    assert metas["F"]["env"]["LOOM_BASE_URL"] == metas["C"]["env"]["LOOM_BASE_URL"]
    hooks_f = json.loads(
        (tmp_path / "run" / "plugins" / "arm_F" / "hooks" / "hooks.json").read_text()
    )
    assert len(hooks_f["hooks"]["PreToolUse"]) == 1


def test_read_cap_hook_logs_its_rewrites(tmp_path) -> None:
    dest = build_shunt_only_plugin(PLUGIN_SRC, tmp_path / "p", read_cap=200)
    hook = (dest / "hooks" / "read-cap.mjs").read_text()
    # an ES module: `require` is undefined there, the import is what runs
    assert 'import fs from "node:fs"' in hook and "require(" not in hook
    assert "bench-read-cap.log" in hook and "updatedInput" in hook


def test_score_run_reads_wall_and_cap_hits_from_the_cell(tmp_path, task) -> None:
    from bench.code_plane_ablation import arm_summary, render_report, score_run, write_tasks

    run = tmp_path / "run"
    for arm, cap in (("A", 0), ("D", 2)):
        cell = run / "cells" / task.id / arm / "s1"
        cell.mkdir(parents=True)
        src = FIXTURE_DIR / ("transcript_arm_C.jsonl" if arm == "D" else "transcript_arm_A.jsonl")
        (cell / "transcript.jsonl").write_text(src.read_text())
        (cell / "meta.json").write_text(json.dumps({"duration_s": 12.5 if arm == "A" else 30.0}))
        if cap:
            (cell / "read_cap.log").write_text(
                "cap src/a.cpp 1 none -> 200\ncap src/b.cpp 1 500 -> 200\nnoise\n"
            )
    write_tasks([task], {}, tmp_path / "tasks.jsonl")
    scores, _ = score_run(run, [task])
    by = {s.arm: s for s in scores}
    assert by["A"].wall_s == 12.5 and by["A"].cap_hits == 0
    assert by["D"].wall_s == 30.0 and by["D"].cap_hits == 2
    summary = arm_summary(scores)
    assert summary["D"]["cap_hits"] == 2.0 and summary["A"]["wall_s"] == 12.5
    assert summary["A"]["api_key_share"] == 0.0 and summary["D"]["api_key_sources"] == ["?"]
    text = render_report(summary, [], 0, [task], {})
    assert "| cap |" in text and "wall_s" in text and "CLI-accounted spend" in text


def test_run_refuses_to_bill_the_login_by_default(tmp_path, monkeypatch) -> None:
    from bench.code_plane_ablation import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tasks = FIXTURE_DIR / "tasks.jsonl"
    rc = main(
        [
            "run",
            "--tasks",
            str(tasks),
            "--out",
            str(tmp_path / "r"),
            "--driver",
            "claude",
            "--arms",
            "A",
            "--repo",
            "",
        ]
    )
    assert rc == 2
    # the dry driver never spends, so it never needs the key
    rc = main(
        [
            "run",
            "--tasks",
            str(tasks),
            "--out",
            str(tmp_path / "d"),
            "--driver",
            "dry",
            "--arms",
            "A",
            "--plugin-src",
            str(PLUGIN_SRC),
        ]
    )
    assert rc == 0


def test_run_resolves_a_relative_out_dir_against_the_cwd(tmp_path, monkeypatch) -> None:
    """A relative --out is resolved before `git -C <mirror> worktree add`, so the
    worktree is never planted inside the bare mirror; the run dir is absolute."""
    from bench.code_plane_ablation import main

    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "run",
            "--tasks",
            str(FIXTURE_DIR / "tasks.jsonl"),
            "--out",
            "rel",
            "--driver",
            "dry",
            "--arms",
            "A",
            "--plugin-src",
            str(PLUGIN_SRC),
        ]
    )
    assert rc == 0
    meta = json.loads(next((tmp_path / "rel" / "cells").rglob("meta.json")).read_text())
    assert Path(meta["env"]["TMPDIR"]).is_absolute()
    assert meta["env"]["TMPDIR"].startswith(str(tmp_path.resolve()))


def test_plugin_dir_is_built_once_under_concurrent_cells(tmp_path, monkeypatch) -> None:
    """The two seeds of a task start together, so two cells can race
    build_shunt_only_plugin (rmtree + copytree on one directory) into
    FileExistsError. The build is serialised."""
    from concurrent.futures import ThreadPoolExecutor

    import bench.agent_traj.claude_code_driver as drv

    calls: list[str] = []
    real = drv.build_shunt_only_plugin

    def slow_build(src, dest, *, read_cap=0):
        calls.append(dest.name)
        return real(src, dest, read_cap=read_cap)

    monkeypatch.setattr(drv, "build_shunt_only_plugin", slow_build)
    cfg = DriverConfig(plugin_src=PLUGIN_SRC, loom_url="http://127.0.0.1:1", loom_token="t")
    cli = ClaudeCliDriver(cfg, tmp_path / "run")
    with ThreadPoolExecutor(max_workers=6) as pool:
        dirs = list(pool.map(cli.plugin_dir_for, ["E"] * 6 + ["C"] * 6))
    assert calls.count("arm_E") == 1 and calls.count("arm_C") == 1
    assert {d.name for d in dirs} == {"arm_E", "arm_C"}
    assert (tmp_path / "run" / "plugins" / "arm_E" / "lib").is_dir()


# --------------------------------------------------------------------------- shunt repo slug


def test_shunt_repo_is_derived_from_the_mirrors_origin_never_a_path() -> None:
    from bench.agent_traj.claude_code_driver import slug_of_remote

    assert slug_of_remote("https://github.com/ClickHouse/loom.git") == "clickhouse/loom"
    assert slug_of_remote("git@github.com:ClickHouse/loom.git") == "clickhouse/loom"
    assert slug_of_remote("ssh://git@host/ClickHouse/loom") == "clickhouse/loom"
    assert slug_of_remote("/opt/loom") == "" and slug_of_remote("") == ""


def test_shunt_resolved_reads_the_hooks_cache_in_the_cells_tmp(tmp_path) -> None:
    from bench.agent_traj.claude_code_driver import shunt_resolved

    assert shunt_resolved(tmp_path) is None  # the hook never ran
    (tmp_path / "loom-shunt-abc.json").write_text(json.dumps({"ts": 1, "indexed": False}))
    assert shunt_resolved(tmp_path) is False
    (tmp_path / "loom-shunt-abc.json").write_text(
        json.dumps({"ts": 1, "indexed": True, "org": "o", "namespace": "code-x"})
    )
    assert shunt_resolved(tmp_path) is True


def test_a_plugin_cell_whose_hook_saw_no_index_is_excluded_not_scored(tmp_path, task) -> None:
    """A plugin cell whose hook resolved the repository as 'not indexed'
    never touched the index, so it must not score as 'plugin on'. The driver
    records the resolve; the scorer excludes such a cell."""
    from bench.code_plane_ablation import score_run, write_tasks

    run = tmp_path / "run"
    for arm, resolved in (("A", None), ("C", False)):
        cell = run / "cells" / task.id / arm / "s1"
        cell.mkdir(parents=True)
        src = FIXTURE_DIR / ("transcript_arm_C.jsonl" if arm == "C" else "transcript_arm_A.jsonl")
        (cell / "transcript.jsonl").write_text(src.read_text())
        (cell / "meta.json").write_text(json.dumps({"duration_s": 5.0, "shunt_resolved": resolved}))
    write_tasks([task], {}, tmp_path / "tasks.jsonl")
    scores, _ = score_run(run, [task])
    by = {s.arm: s for s in scores}
    assert by["A"].excluded is False
    assert by["C"].excluded is True and "shunt_unmapped" in by["C"].exclusion_reasons
    # a resolved plugin cell is scored as before
    (run / "cells" / task.id / "C" / "s1" / "meta.json").write_text(
        json.dumps({"duration_s": 5.0, "shunt_resolved": True})
    )
    scores, _ = score_run(run, [task])
    assert {s.arm: s for s in scores}["C"].excluded is False


def test_a_timed_out_cell_takes_its_whole_process_group_down(tmp_path: Path) -> None:
    """The agent's hook and MCP children share its process group; a timeout
    kills the group, so no grandchild outlives the cell."""
    import os
    import time

    from bench.agent_traj.claude_code_driver import run_process_group

    pidfile = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    with (tmp_path / "o").open("wb") as fo, (tmp_path / "e").open("wb") as fe:
        rc, timed_out = run_process_group(
            ["sh", "-c", script],
            cwd=tmp_path,
            env=dict(os.environ),
            stdout=fo,
            stderr=fe,
            timeout_s=1.0,
            grace_s=1.0,
        )
    assert (rc, timed_out) == (None, True)
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild survived the cell's timeout")


def test_a_finished_cell_reports_its_exit_code(tmp_path: Path) -> None:
    import os

    from bench.agent_traj.claude_code_driver import run_process_group

    with (tmp_path / "o").open("wb") as fo, (tmp_path / "e").open("wb") as fe:
        got = run_process_group(
            ["sh", "-c", "exit 3"],
            cwd=tmp_path,
            env=dict(os.environ),
            stdout=fo,
            stderr=fe,
            timeout_s=10.0,
        )
    assert got == (3, False)


def test_the_mcp_config_is_private_from_its_first_byte(tmp_path: Path) -> None:
    from bench.agent_traj.claude_code_driver import ClaudeCliDriver, DriverConfig

    cfg = DriverConfig(loom_url="http://bench.invalid", loom_token="secret-bearer")
    p = ClaudeCliDriver(cfg, tmp_path).mcp_config_for("C")
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert "secret-bearer" in p.read_text()
    assert not [x for x in tmp_path.iterdir() if x.name.startswith(".mcp_")], "no temp left"


def test_workdir_dirty_reads_git_status(tmp_path: Path) -> None:
    import subprocess

    from bench.agent_traj.claude_code_driver import workdir_dirty

    assert workdir_dirty(tmp_path / "missing") is None
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert workdir_dirty(tmp_path) is False
    (tmp_path / "x.cpp").write_text("int x;\n")
    assert workdir_dirty(tmp_path) is True
