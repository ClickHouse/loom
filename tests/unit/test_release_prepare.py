"""scripts/release/prepare.py: one version across every file, the chart's own
version, and the release notes from pull request bodies."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.unit.git_repo import git as _git

ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts/release/prepare.py"
    spec = importlib.util.spec_from_file_location("release_prepare", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = _module()
PLANNED_CHART = prepare.planned_chart  # before a fixture replaces it

TEMPLATE = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()


def _copy(tmp_path: Path) -> Path:
    for path in {pin.path for pin in prepare.PINS}:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    return tmp_path


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-qm", message, "--allow-empty")


def _body(category: str, entry: str = "...", steps: str = "none") -> str:
    """The template as a contributor fills it: one category left, the rest deleted."""
    body = TEMPLATE
    start = body.index("### Changelog category")
    end = body.index("### Changelog entry")
    header = body[start : body.index("\n", start) + 1]
    body = body[:start] + header + f"\n- {category}\n\n" + body[end:]
    body = body.replace("\n...\n", f"\n{entry}\n", 1)
    return re.sub(r"^- Operator steps:.*$", f"- Operator steps: {steps}", body, count=1, flags=re.M)


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """main() with git, gh and the files replaced; returns the calls in order."""
    calls: list[tuple[str, ...]] = []
    notes_file = tmp_path / "release-notes.md"

    def notes(version: str, previous: str) -> str:
        calls.append(("notes", version, previous))
        return "## 1.2.0\n"

    monkeypatch.setattr(prepare, "validate", lambda version: None)
    monkeypatch.setattr(prepare, "refuse_tagged", lambda version: None)
    monkeypatch.setattr(prepare, "_git", lambda root, *args: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr(prepare, "previous_release", lambda version: "v1.1.0")
    monkeypatch.setattr(prepare, "released_chart", lambda tag: "0.1.0")
    monkeypatch.setattr(prepare, "planned_chart", lambda *args: "0.2.0")
    monkeypatch.setattr(prepare, "release_notes", notes)
    monkeypatch.setattr(
        prepare,
        "bump",
        lambda *args: calls.append(("bump", *args, notes_file.read_text())),
    )
    monkeypatch.setattr(prepare, "NOTES_FILE", notes_file)
    return calls


def test_every_file_names_the_servers_version() -> None:
    """What CI asks of every pull request, the release's included."""
    assert prepare.check() == []


def test_a_bump_moves_every_version_and_nothing_else(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    current = prepare.read(prepare.SERVER)
    major, minor, _ = prepare.parse(current)
    target = f"{major}.{minor + 1}.0"
    chart = prepare.read(prepare.CHART)
    prepare.bump(target, prepare.planned_chart(target, current, chart, root), root)
    assert prepare.check(root) == []
    assert prepare.read(prepare.SERVER, root) == target
    for path in {pin.path for pin in prepare.PINS}:
        before = (ROOT / path).read_text().splitlines()
        after = (root / path).read_text().splitlines()
        changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
        if path == prepare.CHART.path:
            # The chart's own version is the one other line that moves: a minor.
            c_major, c_minor, _ = prepare.parse(chart)
            assert (f"version: {chart}", f"version: {c_major}.{c_minor + 1}.0") in changed
            changed = [(a, b) for a, b in changed if not a.startswith("version: ")]
        assert changed and all(current in a and target in b for a, b in changed), path
    # The files still parse as what they are.
    for path in ("server.json", "frontend/package-lock.json", "sdk/typescript/package-lock.json"):
        json.loads((root / path).read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    (server,) = [p for p in lock["package"] if p["name"] == "clickhouse-loom-server"]
    assert server["version"] == target
    for path in ("frontend/package-lock.json", "sdk/typescript/package-lock.json"):
        data = json.loads((root / path).read_text())
        assert data["version"] == data["packages"][""]["version"] == target


def test_a_version_already_tagged_is_refused(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    _git(root, "init", "-q")
    _commit(root, "files")
    _git(root, "tag", "v98.0.0")
    with pytest.raises(ValueError, match="v98.0.0 is already tagged"):
        prepare.refuse_tagged("98.0.0", root)
    prepare.refuse_tagged("98.0.1", root)


@pytest.mark.parametrize(
    ("version", "error"),
    [
        # A release candidate is a tag on the release commit, whose files name the final.
        ("1.9.0-rc.1", "is not a final X.Y.Z: a candidate is a tag"),
        ("1.9", "'1.9' is not X.Y.Z"),
        ("v1.9.0", "'v1.9.0' is not X.Y.Z"),
    ],
)
def test_a_bump_takes_a_final_version_only(version: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        prepare.require_final(version)


def test_a_bump_refuses_an_older_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="is below the current version"):
        prepare.validate("0.0.1", _copy(tmp_path))


def test_a_pin_that_no_longer_matches_stops_the_bump_before_any_write(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    lock = root / "frontend/package-lock.json"
    lock.write_text(lock.read_text().replace('"packages": {', '"packages":  {'))
    before = {pin.path: (root / pin.path).read_text() for pin in prepare.PINS}
    with pytest.raises(ValueError, match="matches 0 times"):
        prepare.bump("99.0.0", "1.1.0", root)
    assert {pin.path: (root / pin.path).read_text() for pin in prepare.PINS} == before


def test_a_file_left_behind_is_named(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    chart = root / "deploy/helm/loom/Chart.yaml"
    chart.write_text(chart.read_text().replace('appVersion: "', 'appVersion: "0.0.0-'))
    (error,) = prepare.check(root)
    assert error.startswith("deploy/helm/loom/Chart.yaml names 0.0.0-")


def test_the_version_table_names_every_file_the_script_bumps() -> None:
    text = (ROOT / "docs/development/release-process.md").read_text()
    table = text[text.index("## Where the version lives") : text.index("## Release notes")]
    for pin in [*prepare.PINS, prepare.CHART]:
        assert f"`{pin.path}`" in table, pin.path


@pytest.mark.parametrize(
    ("chart", "current", "version", "expected"),
    [
        ("0.1.0", "1.1.0", "1.2.0", "0.2.0"),
        ("0.2.0", "1.2.0", "1.2.1", "0.2.1"),
        ("0.2.1", "1.2.1", "2.0.0", "0.3.0"),
        ("1.4.0", "2.0.0", "2.1.0", "1.5.0"),
    ],
)
def test_a_patch_release_moves_the_chart_a_patch_and_any_other_a_minor(
    chart: str, current: str, version: str, expected: str
) -> None:
    assert prepare.next_chart_version(version, current, chart) == expected


def test_the_chart_moves_one_step_from_the_previous_release(tmp_path: Path) -> None:
    """A changed mind (one version, then another) still moves the chart once from
    the chart the previous release shipped."""
    root = _copy(tmp_path)
    for version in ("98.1.0", "98.2.0", "98.2.0"):
        prepare.bump(version, prepare.planned_chart(version, "98.0.3", "4.7.1", root), root)
        assert prepare.read(prepare.CHART, root) == "4.8.0", version
    prepare.bump("98.2.1", prepare.planned_chart("98.2.1", "98.2.0", "4.8.0", root), root)
    assert prepare.read(prepare.CHART, root) == "4.8.1"


def test_a_chart_already_raised_since_the_previous_release_keeps_its_version(
    tmp_path: Path,
) -> None:
    root = _copy(tmp_path)
    chart = root / prepare.CHART.path
    chart.write_text(re.sub(r"^version: .*$", "version: 7.0.0", chart.read_text(), flags=re.M))
    prepare.bump("98.1.0", prepare.planned_chart("98.1.0", "98.0.0", "0.1.0", root), root)
    assert prepare.read(prepare.CHART, root) == "7.0.0"


def test_every_release_raises_the_chart_above_the_previous_releases(tmp_path: Path) -> None:
    chart = tmp_path / "deploy/helm/loom/Chart.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text('version: 0.1.0\nappVersion: "1.0.0"\n')
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "chart")
    _git(tmp_path, "tag", "v1.0.0")
    chart.write_text('version: 0.1.0\nappVersion: "1.1.0"\n')
    _commit(tmp_path, "appVersion")
    error = prepare.check_chart("v1.0.0", tmp_path)
    assert (
        error and "the chart's version is 0.1.0, not above the 0.1.0 that v1.0.0 shipped" in error
    )
    chart.write_text('version: 0.2.0\nappVersion: "1.1.0"\n')
    _commit(tmp_path, "chart version")
    assert prepare.check_chart("v1.0.0", tmp_path) is None


def test_the_chart_is_compared_with_the_release_it_ships_or_the_one_before(
    tmp_path: Path,
) -> None:
    """On the tag's own run and for the next release, the release before; none on
    a dry run of a version that already shipped from another commit."""
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "one")
    _git(tmp_path, "tag", "v1.0.0")
    (tmp_path / "f").write_text("2")
    _commit(tmp_path, "two")
    _git(tmp_path, "tag", "v1.1.0")
    assert prepare.chart_baseline("1.1.0", tmp_path) == "v1.0.0"  # the tag's own run
    assert prepare.chart_baseline("1.2.0", tmp_path) == "v1.1.0"  # the next release
    assert prepare.chart_baseline("1.2.0-rc.1", tmp_path) == "v1.1.0"  # its candidate
    (tmp_path / "f").write_text("3")
    _commit(tmp_path, "three")
    # A dry run of a branch whose files still name the shipped 1.1.0: nothing to check.
    assert prepare.chart_baseline("1.1.0", tmp_path) is None
    assert prepare.chart_baseline("1.1.0-rc.1", tmp_path) is None  # a candidate's run too
    with pytest.raises(ValueError, match="no final release tag below 0.9.0"):
        prepare.chart_baseline("0.9.0", tmp_path)  # no tags fetched reads the same


def test_a_filled_template_gives_its_category_entry_and_steps() -> None:
    parsed = prepare.changelog(
        _body("Bug Fix (user-visible misbehavior in a release)", "A search finds\nmore.", "run X")
    )
    assert parsed == (
        "Bug Fix (user-visible misbehavior in a release)",
        "A search finds more.",
        "run X",
    )


@pytest.mark.parametrize(
    "body", ["", "Fixes the thing.", TEMPLATE, "### Changelog category\n\n- Bug Fixes\n"]
)
def test_a_body_naming_no_single_template_category_is_unclassified(body: str) -> None:
    parsed = prepare.changelog(body)
    assert parsed and parsed[0] == prepare.UNCLASSIFIED


def test_a_category_without_the_templates_guidance_is_read() -> None:
    category, _, _ = prepare.changelog("### Changelog category\n\n- Bug Fix\n")
    assert category == "Bug Fix (user-visible misbehavior in a release)"


def test_the_categories_are_the_templates() -> None:
    names = prepare.categories()
    assert names[0] == "New Feature" and len(names) == 10
    assert "Critical Bug Fix (data loss, cross-tenant read, auth bypass)" in names


def test_an_operator_step_keeps_its_wrapped_lines_up_to_the_next_item() -> None:
    steps = "set `LOOM_A`,\nthen run the backfill"  # as GitHub's editor wraps it
    assert prepare.changelog(_body("New Feature", "x", steps)) == ("New Feature", "x", steps)


@pytest.mark.parametrize(
    "steps",
    [
        "none",
        "None.",
        "No.",
        "No operator steps.",
        "nothing to do",
        "N/A",
        '"none"',
        "`none`",
        "**none**",
    ],
)
def test_no_step_written_any_way_is_no_step(steps: str) -> None:
    assert prepare.changelog(_body("New Feature", "x", steps)) == ("New Feature", "x", "")


def test_a_step_that_begins_with_no_is_a_step() -> None:
    steps = "No downtime, but run `loom knowledge migrate-originals` first"
    assert prepare.changelog(_body("New Feature", "x", steps)) == ("New Feature", "x", steps)


@pytest.mark.parametrize(
    "line",
    [
        "- **Operator steps:** run the backfill",
        "* Operator steps: run the backfill",
        "- Operator steps (deploy order): run the backfill",
    ],
)
def test_an_operator_steps_label_written_another_way_is_read(line: str) -> None:
    body = _body("New Feature", "x").replace("- Operator steps: none", line)
    assert prepare.changelog(body) == ("New Feature", "x", "run the backfill")


def test_the_templates_own_operator_steps_text_is_no_step() -> None:
    placeholder = re.search(r"^- Operator steps:.*$", TEMPLATE, re.M)
    assert placeholder
    body = _body("New Feature", "x").replace("- Operator steps: none", placeholder.group(0))
    assert prepare.changelog(body) == ("New Feature", "x", "")


@pytest.mark.parametrize("item", ["- **Operator steps: none**", "- Operator steps:\n"])
def test_a_bolded_none_or_an_empty_item_is_no_step(item: str) -> None:
    body = _body("New Feature", "x").replace("- Operator steps: none", item)
    assert prepare.changelog(body) == ("New Feature", "x", "")


def test_a_category_that_needs_no_entry_keeps_its_operator_steps() -> None:
    category = "CI Fix or Improvement (changelog entry is not required)"
    assert prepare.changelog(_body(category, "x", "set LOOM_X first")) == (
        category,
        "x",
        "set LOOM_X first",
    )
    text = prepare.notes(
        "1.2.0",
        "v1.1.0",
        [{"number": 9, "title": "T", "body": _body(category, "x", "set LOOM_X first")}],
        "## What changed for clients: a → b\n\nNothing.\n",
    )
    assert "(#9)" not in text.split("### Operator steps")[0]  # no entry
    assert "### Operator steps\n\n- #9: set LOOM_X first\n" in text


@pytest.mark.parametrize(
    "steps",
    [
        "in this order:\n  - run migrate\n  - restart the workers",
        "in this order:\n\n  - run migrate\n  - restart the workers",  # a loose list
        "in this order:\n  1. run migrate\n  2. restart the workers",
    ],
)
def test_a_nested_list_under_operator_steps_is_kept_as_written(steps: str) -> None:
    assert prepare.changelog(_body("New Feature", "x", steps))[2] == steps
    text = prepare.notes(
        "1.2.0",
        "v1.1.0",
        [{"number": 9, "title": "T", "body": _body("New Feature", "x", steps)}],
        "## What changed for clients: a → b\n\nNothing.\n",
    )
    # The first line carries the pull request; the rest nests under it.
    assert "- #9: in this order:\n" in text and "\n    " in text.split("### Operator steps")[1]


def test_the_notes_group_entries_in_the_templates_order() -> None:
    pulls = [
        {"number": 1, "title": "A", "body": _body("Build/Testing/Packaging Improvement", "Bench.")},
        {
            "number": 2,
            "title": "B",
            "body": _body("CI Fix or Improvement (changelog entry is not required)"),
        },
        {
            "number": 3,
            "title": "Fix C",
            "body": _body("Bug Fix (user-visible misbehavior in a release)"),
        },
        {"number": 4, "title": "D", "body": _body("New Feature", "Another.", "set Y")},
        {"number": 5, "title": "E", "body": "no template"},
        {"number": 6, "title": "F", "body": _body("Bug Fix", "Kept.")},
    ]
    wire = (
        "## What changed for clients: v1.1.0 → HEAD\n\n"
        "### Changed operations\n- `memory.set`: field `x` added\n"
    )
    text = prepare.notes("1.2.0", "v1.1.0", pulls, wire)
    assert text.startswith("## Release notes for v1.2.0\n")
    # A new feature above a packaging change, whichever merged first.
    assert text.index("### New Feature") < text.index("### Bug Fix") < text.index("### Build")
    # The template's guidance in parentheses stays out of the headings.
    assert "### Bug Fix\n" in text and "(user-visible" not in text
    assert "### New Feature\n\n- Another. (#4)\n" in text
    # An empty entry falls back to the title; a category without its guidance is read.
    assert "### Bug Fix\n\n- Fix C (#3)\n- Kept. (#6)\n" in text
    assert "(#2)" not in text
    assert (
        "### Unclassified\n\n- E (#5): its description names no single changelog category\n"
    ) in text
    assert "### Operator steps\n\n- #4: set Y\n" in text
    assert text.count("What changed for clients") == 1
    # The wire diff's own sections sit under the notes' section.
    assert text.endswith(
        "### What changed for clients since v1.1.0\n\n"
        "#### Changed operations\n- `memory.set`: field `x` added\n"
    )


def test_operator_steps_given_only_as_a_list_nest_under_their_pull_request() -> None:
    steps = "\n  - run migrate\n  - restart the workers"
    text = prepare.notes(
        "1.2.0",
        "v1.1.0",
        [{"number": 9, "title": "T", "body": _body("New Feature", "x", steps)}],
        "## What changed for clients: a → b\n\nNothing.\n",
    )
    assert "### Operator steps\n\n- #9:\n    - run migrate\n    - restart the workers\n" in text


def test_the_previous_release_is_the_highest_final_tag_below(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "x")
    for tag in ("v1.0.0", "v1.0.1", "v1.1.0", "v1.10.0", "v1.2.0-rc.1", "v2.0.0"):
        _git(tmp_path, "tag", tag)
    assert prepare.previous_release("1.2.0", tmp_path) == "v1.1.0"
    assert prepare.previous_release("1.11.0", tmp_path) == "v1.10.0"
    with pytest.raises(ValueError, match="no final release tag below 1.0.0"):
        prepare.previous_release("1.0.0", tmp_path)


def test_the_merged_pull_requests_are_the_squash_merges_since_the_tag(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("0")
    _commit(tmp_path, "before (#1)")
    _git(tmp_path, "tag", "v1.0.0")
    for n, subject in enumerate(
        ["A change (#12)", 'Revert "A change (#12)" (#13)', "a commit with no number", "B (#14)"]
    ):
        (tmp_path / "f").write_text(str(n + 1))
        _commit(tmp_path, subject)
    (tmp_path / "server").mkdir()
    (tmp_path / "server/__init__.py").write_text('__version__ = "1.1.0"\n')
    _commit(tmp_path, "Release 1.1.0 (#15)")  # the release pull request itself
    assert list(prepare.merged_pull_requests("v1.0.0", tmp_path)) == [12, 13, 14]


def _released(root: Path) -> Path:
    """v1.0.0, then #12, #13 and #14, and a wire changelog that leaves a mark."""
    root.mkdir()
    _git(root, "init", "-q")
    _commit(root, "before (#1)")
    _git(root, "tag", "v1.0.0")
    for n in (12, 13, 14):
        _commit(root, f"Change {n} (#{n})")
    wire = root / "scripts/codegen/wire_changelog.py"
    wire.parent.mkdir(parents=True)
    wire.write_text(
        "import pathlib\npathlib.Path('wire-ran').touch()\n"
        "print('## What changed for clients: a → b\\n\\nNothing.')\n"
    )
    return root


def _gh_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, numbers: list[int]) -> Path:
    """`gh` on PATH that lists `numbers`, newest first as gh does, and keeps its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pulls = [
        {"number": n, "title": f"T{n}", "body": _body("New Feature", f"Entry {n}.")}
        for n in numbers
    ]
    (bin_dir / "listed.json").write_text(json.dumps(pulls))
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{bin_dir}/args"\ncat "{bin_dir}/listed.json"\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


def test_the_notes_read_the_merged_pull_requests_in_one_call_in_merge_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 23:30 UTC the day before: a merger east of UTC, as GitHub records it.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-11-07T00:30:00+01:00")
    root = _released(tmp_path / "repo")
    gh = _gh_listing(tmp_path, monkeypatch, [14, 13, 12])
    text = prepare.release_notes("1.1.0", "v1.0.0", root)
    assert "- Entry 12. (#12)\n- Entry 13. (#13)\n- Entry 14. (#14)\n" in text
    # A UTC day before the oldest merge since the tag: 2026-11-06T23:30Z.
    args = (gh / "args").read_text().splitlines()
    assert args[args.index("--search") + 1] == "merged:>=2026-11-05"


def test_a_merged_pull_request_gh_does_not_list_stops_the_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _released(tmp_path / "repo")
    _gh_listing(tmp_path, monkeypatch, [14, 12])
    with pytest.raises(ValueError, match="did not return the merged pull requests #13$"):
        prepare.release_notes("1.1.0", "v1.0.0", root)
    assert not (root / "wire-ran").exists()


def test_the_notes_are_written_before_the_bump(
    prepared: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "1.2.0"])
    prepare.main()
    assert prepared == [
        ("notes", "1.2.0", "v1.1.0"),
        ("bump", "1.2.0", "0.2.0", "## 1.2.0\n"),  # the file already written
    ]


def test_notes_only_prints_and_writes_nothing(
    prepared: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def tagged(version: str) -> None:
        raise ValueError(f"v{version} is already tagged")

    monkeypatch.setattr(prepare, "refuse_tagged", tagged)  # the notes may be made again
    monkeypatch.setattr(sys, "argv", ["prepare.py", "notes", "1.2.0"])
    prepare.main()
    assert prepared == [("notes", "1.2.0", "v1.1.0")]
    assert capsys.readouterr().out == "## 1.2.0\n\n"
    assert not prepare.NOTES_FILE.exists()


def test_a_failure_making_the_notes_bumps_nothing(
    prepared: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_gh(version: str, previous: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", "pr", "list"])

    monkeypatch.setattr(prepare, "release_notes", no_gh)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "1.2.0"])
    with pytest.raises(SystemExit) as exited:
        prepare.main()
    assert str(exited.value) == "gh pr list failed"  # a plain line outside CI
    assert prepared == [] and not prepare.NOTES_FILE.exists()


def test_a_chart_that_cannot_be_read_at_the_previous_release_says_so(
    prepared: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(tag: str) -> str:
        raise subprocess.CalledProcessError(128, ["git", "show"], stderr="fatal: bad object\n")

    monkeypatch.setattr(prepare, "released_chart", unreadable)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "1.2.0"])
    with pytest.raises(SystemExit, match="^git show failed: fatal: bad object$"):
        prepare.main()
    assert prepared == []


def test_a_refused_version_stops_before_any_gh_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_call(*args: object) -> str:
        raise AssertionError("release_notes ran")

    def no_fetch(*args: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("fetched the tags")

    monkeypatch.setattr(prepare, "release_notes", no_call)
    monkeypatch.setattr(prepare, "_git", no_fetch)  # a non-final version needs no tags
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "9.9.9-rc.1"])
    with pytest.raises(SystemExit, match="is not a final X.Y.Z: a candidate is a tag"):
        prepare.main()


def test_a_version_tagged_on_the_remote_is_refused_after_the_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone had not fetched v98.0.0 yet: the refusal sees the remote's tags."""
    root = _copy(tmp_path)
    _git(root, "init", "-q")
    _commit(root, "files")

    def fetch(where: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            _git(root, "tag", "v98.0.0")  # what the remote has
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)

    validate = prepare.validate
    monkeypatch.setattr(prepare, "_git", fetch)
    monkeypatch.setattr(prepare, "validate", lambda version: validate(version, root))
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "98.0.0"])
    with pytest.raises(SystemExit, match="v98.0.0 is already tagged"):
        prepare.main()


def test_the_tags_that_cannot_be_fetched_stop_the_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare, "validate", lambda version: None)
    monkeypatch.setattr(
        prepare, "_git", lambda root, *args: subprocess.CompletedProcess(args, 1, "", "no network")
    )
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "1.2.0"])
    with pytest.raises(SystemExit, match="failed: no network$"):
        prepare.main()


def test_a_release_with_no_final_tag_below_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every git call succeeds, and the tag list is empty.
    monkeypatch.setattr(
        prepare, "_git", lambda root, *args: subprocess.CompletedProcess(args, 0, "", "")
    )
    monkeypatch.setattr(prepare, "validate", lambda version: None)
    monkeypatch.setattr(prepare, "refuse_tagged", lambda version: None)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "1.2.0"])
    with pytest.raises(SystemExit, match="no final release tag below 1.2.0"):
        prepare.main()


def test_a_version_file_the_check_cannot_read_is_an_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable() -> list[str]:
        raise ValueError("server/__init__.py: matches 0 times, not once")

    monkeypatch.setattr(prepare, "check", unreadable)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "check"])
    with pytest.raises(SystemExit) as exited:
        prepare.main()
    # A message exits with status 1.
    assert str(exited.value) == "::error::server/__init__.py: matches 0 times, not once"


def test_a_skipped_chart_check_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(prepare, "chart_baseline", lambda version: None)
    for tag_version in ("1.2.0", "1.2.0-rc.3"):  # the final is what is on another commit
        monkeypatch.setattr(sys, "argv", ["prepare.py", "check-chart", tag_version])
        prepare.main()
        assert capsys.readouterr().out == (
            "::notice::no chart check: v1.2.0 is tagged on another commit\n"
        )


def test_a_chart_the_check_cannot_read_at_the_baseline_is_an_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(baseline: str) -> str:
        raise subprocess.CalledProcessError(128, ["git", "show"], stderr="fatal: bad path\n")

    monkeypatch.setattr(prepare, "chart_baseline", lambda version: "v1.1.0")
    monkeypatch.setattr(prepare, "check_chart", unreadable)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "check-chart", "1.2.0"])
    with pytest.raises(SystemExit, match="could not read the chart at v1.1.0: fatal: bad path"):
        prepare.main()


def test_a_candidate_comes_before_its_final_and_the_final_goes_where_it_was_soaked(
    tmp_path: Path,
) -> None:
    (tmp_path / "f").write_text("a")
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "soaked")
    _git(tmp_path, "tag", "v1.2.0-rc.1")
    assert prepare.check_tag("1.2.0-rc.1", tmp_path) is None  # the candidate's own run
    _git(tmp_path, "tag", "v1.2.0")
    assert prepare.check_tag("1.2.0", tmp_path) is None  # the final on the soaked commit
    _git(tmp_path, "tag", "-d", "v1.2.0")
    (tmp_path / "f").write_text("b")
    _commit(tmp_path, "a fix after the soak")
    _git(tmp_path, "tag", "v1.2.0")
    error = prepare.check_tag("1.2.0", tmp_path)
    assert error and "v1.2.0 is not on v1.2.0-rc.1's commit" in error
    _git(tmp_path, "tag", "v1.2.0-rc.2")  # the fix soaked as rc.2: its commit is HEAD
    assert prepare.check_tag("1.2.0", tmp_path) is None
    _git(tmp_path, "tag", "v1.2.0-rc.3")
    error = prepare.check_tag("1.2.0-rc.3", tmp_path)
    assert error == "v1.2.0-rc.3 is on v1.2.0's commit, which is released: run v1.2.0 instead"
    # A stray candidate on the final's own commit leaves the final's runs alone.
    assert prepare.check_tag("1.2.0", tmp_path) is None


def test_a_final_with_no_candidate_is_not_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("a")
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "one")
    _git(tmp_path, "tag", "v1.3.0-rc.1-x")  # not a candidate, on an earlier commit
    _commit(tmp_path, "two")
    _git(tmp_path, "tag", "v1.3.0")
    assert prepare.check_tag("1.3.0", tmp_path) is None


def test_a_dry_run_after_a_candidate_shipped_is_not_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("a")
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "soaked")
    _git(tmp_path, "tag", "v1.2.0-rc.1")
    (tmp_path / "f").write_text("b")
    _commit(tmp_path, "main moved on")
    assert not prepare.tag_on_head("1.2.0", tmp_path)  # no v1.2.0 tag: a dry run
    assert not prepare.tag_on_head("1.2.0-rc.1", tmp_path)  # the tag is not on HEAD
    _git(tmp_path, "tag", "v1.3.0-rc.1")
    assert prepare.tag_on_head("1.3.0-rc.1", tmp_path)


def test_a_tag_check_off_its_commit_says_it_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(prepare, "tag_on_head", lambda version: False)
    monkeypatch.setattr(prepare, "check_tag", lambda version: "never asked")
    monkeypatch.setattr(sys, "argv", ["prepare.py", "check-tag", "1.2.0"])
    prepare.main()
    assert (
        "::notice::no candidate-order check: v1.2.0 is not on this commit"
        in capsys.readouterr().out
    )


def test_a_tag_out_of_candidate_order_is_an_error_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare, "tag_on_head", lambda tag_version: True)
    monkeypatch.setattr(prepare, "check_tag", lambda tag_version: "out of order")
    monkeypatch.setattr(sys, "argv", ["prepare.py", "check-tag", "1.2.0"])
    with pytest.raises(SystemExit, match="::error::out of order"):
        prepare.main()


def test_a_chart_version_that_is_not_x_y_z_is_an_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chart = tmp_path / "deploy/helm/loom/Chart.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text('version: 0.1.0\nappVersion: "1.1.0"\n')
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "chart")
    _git(tmp_path, "tag", "v1.1.0")
    chart.write_text('version: "0.2.0"\nappVersion: "1.2.0"\n')  # quoted
    _commit(tmp_path, "quoted chart version")
    check_chart = prepare.check_chart
    monkeypatch.setattr(prepare, "chart_baseline", lambda version: "v1.1.0")
    monkeypatch.setattr(prepare, "check_chart", lambda baseline: check_chart(baseline, tmp_path))
    monkeypatch.setattr(sys, "argv", ["prepare.py", "check-chart", "1.2.0"])
    with pytest.raises(SystemExit) as exited:
        prepare.main()
    assert str(exited.value) == (
        "::error file=deploy/helm/loom/Chart.yaml::'\"0.2.0\"' is not a SemVer X.Y.Z[-pre] version"
    )


def test_a_chart_that_cannot_be_planned_stops_before_the_notes(
    prepared: list[tuple[str, ...]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy(tmp_path)
    chart = root / prepare.CHART.path
    chart.write_text(re.sub(r"^version: .*$", 'version: "0.2.0"', chart.read_text(), flags=re.M))
    # The real planned_chart, on a working tree whose chart version is quoted.
    monkeypatch.setattr(prepare, "planned_chart", lambda *args: PLANNED_CHART(*args, root=root))
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "1.2.0"])
    with pytest.raises(
        SystemExit, match="^deploy/helm/loom/Chart.yaml: '\"0.2.0\"' is not a SemVer"
    ):
        prepare.main()
    assert prepared == [] and not prepare.NOTES_FILE.exists()  # no gh call, no file


def test_a_tag_list_that_cannot_be_read_fails_the_candidate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "tag":
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: bad repository\n")
        return subprocess.CompletedProcess(["git", *args], 0, "abc\n", "")  # on HEAD

    monkeypatch.setattr(prepare, "_git", git)
    monkeypatch.setattr(sys, "argv", ["prepare.py", "check-tag", "1.2.0"])
    with pytest.raises(SystemExit) as exited:
        prepare.main()  # an empty list would have passed the final
    assert str(exited.value) == "::error::git tag --list v1.2.0-rc.* failed: fatal: bad repository"


def test_a_version_file_that_is_missing_is_an_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy(tmp_path)
    (root / "frontend/package-lock.json").unlink()
    check = prepare.check
    monkeypatch.setattr(prepare, "check", lambda: check(root))
    monkeypatch.setattr(sys, "argv", ["prepare.py", "check"])
    with pytest.raises(SystemExit) as exited:
        prepare.main()
    assert exited.value.code == 1
    assert "::error::frontend/package-lock.json: missing\n" in capsys.readouterr().out


def test_a_bump_with_a_version_file_missing_is_a_plain_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy(tmp_path)
    (root / "frontend/package-lock.json").unlink()
    validate = prepare.validate
    monkeypatch.setattr(prepare, "_git", lambda root, *args: subprocess.CompletedProcess(args, 0))
    monkeypatch.setattr(prepare, "validate", lambda version: validate(version, root))
    monkeypatch.setattr(sys, "argv", ["prepare.py", "bump", "99.0.0"])
    with pytest.raises(SystemExit) as exited:
        prepare.main()
    assert str(exited.value) == "frontend/package-lock.json: missing"


def test_a_tag_check_git_cannot_read_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No repository: the check fails, never skips or passes."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # no outer checkout
    with pytest.raises(subprocess.CalledProcessError):
        prepare.tag_on_head("1.2.0-rc.1", tmp_path)


def _release_version_job() -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    return workflow["jobs"]["version"]


def test_the_release_runs_every_check_on_the_tag_with_its_tags() -> None:
    job = _release_version_job()
    (checkout,) = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["fetch-depth"] == 0  # the candidate order and the chart read tags
    script = "\n".join(s.get("run", "") for s in job["steps"])
    for command in ("check", 'check-chart "$VERSION"', 'check-tag "$VERSION"'):
        assert f"python3 scripts/release/prepare.py {command}\n" in script, command


@pytest.mark.parametrize(
    ("ref", "passes"),
    [
        ("refs/tags/v1.2.0", True),
        ("refs/tags/v1.2.0-rc.1", True),
        ("refs/tags/v1.2.0-beta.1", False),
        ("refs/tags/v1.2.0-rc.1.1", False),
        ("refs/tags/v1.2", False),
    ],
)
def test_the_only_prerelease_the_release_takes_is_a_candidate(
    tmp_path: Path, ref: str, passes: bool
) -> None:
    (parse,) = [s for s in _release_version_job()["steps"] if s.get("id") == "parse"]
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", parse["run"]],
        env={**os.environ, "REF": ref, "DRY_RUN": "false", "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is passes, result.stdout
    if not passes:
        assert "is not X.Y.Z or X.Y.Z-rc.N" in result.stdout


@pytest.mark.parametrize(
    ("chart_text", "error"),
    [
        ('appVersion: "1.0.0"\n', r"matches 0 times, not once \(at v1.0.0\)$"),
        (
            'version: "0.1.0"\nappVersion: "1.0.0"\n',
            r"is not a SemVer X.Y.Z\[-pre\] version \(at v1.0.0\)$",
        ),
    ],
)
def test_a_chart_unreadable_at_a_tag_names_the_tag(
    tmp_path: Path, chart_text: str, error: str
) -> None:
    chart = tmp_path / "deploy/helm/loom/Chart.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text(chart_text)
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "chart")
    _git(tmp_path, "tag", "v1.0.0")
    with pytest.raises(ValueError, match=error):
        prepare.released_chart("v1.0.0", tmp_path)


def test_a_soaked_candidate_run_again_after_its_final_is_not_told_to_delete_itself(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "one")
    _git(tmp_path, "tag", "v1.2.0-rc.1")
    _commit(tmp_path, "the fix")
    _git(tmp_path, "tag", "v1.2.0-rc.2")
    _git(tmp_path, "tag", "v1.2.0")
    error = prepare.check_tag("1.2.0-rc.2", tmp_path)
    assert error == "v1.2.0-rc.2 is on v1.2.0's commit, which is released: run v1.2.0 instead"
    assert prepare.check_tag("1.2.0", tmp_path) is None  # rc.2 stays the last candidate
    _commit(tmp_path, "after the release")
    _git(tmp_path, "tag", "v1.2.0-rc.3")  # a stray, on another commit than the final
    error = prepare.check_tag("1.2.0-rc.3", tmp_path)
    assert error and "v1.2.0 is already tagged" in error
    assert ":refs/tags/v1.2.0-rc.3" in error  # the stray tag to delete
