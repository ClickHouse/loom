"""Instruction-file drift linter: pure functions over text + a tree."""

from __future__ import annotations

from server.code.app.index.instructions_lint import (
    FileReport,
    LintReport,
    extract_refs,
    find_instruction_files,
    is_instruction_file,
    lint_text,
    render_report,
    select_targets,
    suggest_paths,
)

TREE = {
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    ".claude/skills/review/SKILL.md",
    ".github/copilot-instructions.md",
    "src/Interpreters/executeQuery.cpp",
    "src/Core/Settings.cpp",
    "ci/jobs/scripts/private_sync_pr.py",
    "tests/ci/release.py",
    "README.md",
}


def test_instruction_files_are_the_ones_agents_read() -> None:
    assert is_instruction_file("CLAUDE.md")
    assert is_instruction_file(".claude/skills/review/SKILL.md")
    assert is_instruction_file(".github/copilot-instructions.md")
    assert is_instruction_file(".cursor/rules/style.mdc")
    assert not is_instruction_file("README.md")
    assert not is_instruction_file("src/Interpreters/executeQuery.cpp")
    assert find_instruction_files(TREE) == [
        ".claude/CLAUDE.md",
        ".claude/skills/review/SKILL.md",
        ".github/copilot-instructions.md",
        "CLAUDE.md",
    ]


def test_extract_refs_finds_paths_and_skips_placeholders_and_urls() -> None:
    text = "\n".join(
        [
            "Read src/Core/Settings.cpp and `./src/Interpreters/executeQuery.cpp`.",
            "See https://github.com/x/y/blob/main/docs/guide.md for more.",
            "Edit src/path/File.cpp or path/to/thing.py — illustrative.",
            "Version 26.1.2 is current.",
            "```bash",
            "python ci/jobs/private_sync_pr.py --check",
            "```",
            "`DB::IColumn::insert()` and `DB::Settings` are symbols.",
        ]
    )
    refs = extract_refs(text, symbols=True)
    by_kind = {(r.kind, r.value): r for r in refs}
    assert ("path", "src/Core/Settings.cpp") in by_kind
    assert ("path", "src/Interpreters/executeQuery.cpp") in by_kind
    # URL path segments, placeholders and version numbers are not refs
    assert not any(v.endswith("docs/guide.md") for k, v in by_kind if k == "path")
    assert ("path", "src/path/File.cpp") not in by_kind
    assert ("path", "path/to/thing.py") not in by_kind
    assert not any(v == "26.1.2" for k, v in by_kind if k == "path")
    # Code-block refs are kept and tagged, with the right line number
    blk = by_kind[("path", "ci/jobs/private_sync_pr.py")]
    assert blk.in_code_block and blk.line == 6
    assert ("symbol", "DB::IColumn::insert") in by_kind
    assert ("symbol", "DB::Settings") in by_kind


def test_lint_text_reports_missing_paths_with_move_suggestions() -> None:
    text = (
        "Run ci/jobs/private_sync_pr.py then tests/ci/auto_release.py; see src/Core/Settings.cpp."
    )
    rep = lint_text("CLAUDE.md", text, TREE)
    assert isinstance(rep, FileReport)
    assert rep.refs == 3
    stale = {f.ref.value: f for f in rep.stale}
    assert set(stale) == {"ci/jobs/private_sync_pr.py", "tests/ci/auto_release.py"}
    # moved file: same basename elsewhere is offered
    assert stale["ci/jobs/private_sync_pr.py"].suggestions == ["ci/jobs/scripts/private_sync_pr.py"]
    assert stale["tests/ci/auto_release.py"].suggestions == []


def test_lint_text_judges_only_resolved_symbols() -> None:
    text = "Call `DB::IColumn::insert` not `DB::Gone::away`; `Other::thing` unknown."
    rep = lint_text(
        "AGENTS.md",
        text,
        TREE,
        symbol_exists={"DB::IColumn::insert": True, "DB::Gone::away": False},
    )
    assert [f.ref.value for f in rep.stale] == ["DB::Gone::away"]


def test_suggest_paths_prefers_shared_directory_prefix() -> None:
    tree = {"a/b/x.py", "z/x.py", "a/x.py", "q/r/s/x.py"}
    assert suggest_paths("a/b/c/x.py", tree, limit=2) == ["a/b/x.py", "a/x.py"]


def test_render_report_summarises_and_lists_stale() -> None:
    rep = LintReport(
        head="abcdef1234567890",
        files=[
            lint_text("CLAUDE.md", "see tests/ci/auto_release.py", TREE),
            lint_text("AGENTS.md", "see src/Core/Settings.cpp", TREE),
        ],
    )
    out = render_report(rep, stale_only=True)
    assert out.startswith("instruction files: 2 ·")
    assert "stale refs: 1" in out
    assert "tests/ci/auto_release.py:1" in out
    assert "AGENTS.md" not in out  # clean file hidden under stale_only
    assert rep.to_dict()["stale_refs"] == 1


def test_skill_relative_paths_resolve_and_runtime_artefacts_are_ignored() -> None:
    tree = {
        ".claude/skills/perf/SKILL.md",
        ".claude/skills/perf/scripts/perf_api.py",
        ".claude/skills/perf/references/ci-api.md",
        "src/Core/Settings.cpp",
        "cmake/CMakeLists.txt",
    }
    text = "\n".join(
        [
            "Run scripts/perf_api.py, read references/ci-api.md, then scripts/gone.py.",
            "Output lands in tmp/perf-output.tsv and left/server.log.",
            "```",
            "cmake -DFOO=ON -Dcmake/CMakeLists.txt src/Core/Settings.cpp",
            "```",
        ]
    )
    rep = lint_text(".claude/skills/perf/SKILL.md", text, tree)
    # 3 skill-relative refs + src/Core/Settings.cpp counted; tmp/ and left/
    # are runtime artefacts, and a path glued to a flag (-Dcmake/...) is not
    # extracted at all rather than surfacing as "-cmake/CMakeLists.txt".
    assert rep.refs == 4
    assert [f.ref.value for f in rep.stale] == ["scripts/gone.py"]
    assert all(not r.value.startswith("-") for r in extract_refs(text))


def test_select_targets_adds_glob_matches_to_instruction_files() -> None:
    tree = TREE | {"docs/en/intro.md", "docs/en/ops/backup.md", "docs/img/a.png"}
    assert select_targets(tree) == find_instruction_files(tree)
    got = select_targets(tree, ["docs/*.md"])
    assert "docs/en/intro.md" in got and "docs/en/ops/backup.md" in got
    assert "docs/img/a.png" not in got and "CLAUDE.md" in got
