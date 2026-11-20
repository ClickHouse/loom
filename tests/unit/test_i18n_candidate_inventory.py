"""Candidate validation reuses discovery while checking the source at each use."""

from __future__ import annotations

import pathlib
import subprocess
from collections.abc import Callable

import pytest

from scripts.i18n import lifecycle as lc
from scripts.i18n import translate as t

JA = t.LANGUAGES[1]


def git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def publish(root: pathlib.Path, source: str) -> None:
    prepared, line = t.prepare(source, JA)
    body = "\n".join(t.english_requests(prepared))
    for english, japanese in (("A", "一"), ("B", "二"), ("C", "三")):
        body = body.replace("# " + english, "# " + japanese)
    output = root / t.target_path(JA.code, source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(t.render(source, JA, body, line, t.DEFAULT_MODEL))


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "docs").mkdir()
    for name in ("a", "b"):
        (tmp_path / f"docs/{name}.md").write_text(f"# {name.upper()}\n\nKeep `--guard`.\n")
    git(tmp_path, "add", "docs")
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setenv("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    monkeypatch.setattr(lc, "link_problems", lambda: [])
    monkeypatch.setattr(t, "untranslated_pages", lambda: [])
    monkeypatch.setattr(t, "language_lines_differ", lambda: [])
    return tmp_path


@pytest.mark.parametrize("count", [1, 2])
def test_present_candidates_share_one_inventory(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    for name in ("a", "b")[:count]:
        publish(repo, f"docs/{name}.md")
    sources = t.sources
    discoveries = []

    def discover() -> tuple[str, ...]:
        discoveries.append(True)
        return sources()

    monkeypatch.setattr(t, "sources", discover)
    assert lc.present_problems() == []
    assert len(discoveries) == 1

    output = repo / t.target_path(JA.code, f"docs/{('a', 'b')[count - 1]}.md")
    output.write_text(output.read_text().replace("--guard", "--removed"))
    assert lc.present_problems()
    assert len(discoveries) == 2


def change_after_discovery(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    sources = t.sources

    def discover() -> tuple[str, ...]:
        inventory = sources()
        change()
        return inventory

    monkeypatch.setattr(t, "sources", discover)


@pytest.mark.parametrize("boundary", ["file", "parent"])
def test_candidate_rechecks_source_boundary_after_discovery(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    publish(repo, "docs/a.md")
    output = t.target_path(JA.code, "docs/a.md")
    candidate = (repo / output).read_text()
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    (outside / "a.md").write_bytes((repo / "docs/a.md").read_bytes())

    def replace_source() -> None:
        if boundary == "file":
            (repo / "docs/a.md").unlink()
            (repo / "docs/a.md").symlink_to(outside / "a.md")
        else:
            (repo / "docs").rename(repo / "original-docs")
            (repo / "docs").symlink_to(outside, target_is_directory=True)

    change_after_discovery(monkeypatch, replace_source)
    assert lc.candidate_problems(output, candidate)


@pytest.mark.parametrize("change", ["bytes", "missing"])
def test_candidate_rechecks_source_freshness_after_discovery(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    publish(repo, "docs/a.md")
    output = t.target_path(JA.code, "docs/a.md")
    candidate = (repo / output).read_text()

    def replace_source() -> None:
        source = repo / "docs/a.md"
        if change == "bytes":
            source.write_text("# A\n\nKeep `--different`.\n")
        else:
            source.unlink()

    change_after_discovery(monkeypatch, replace_source)
    assert lc.candidate_problems(output, candidate) == [f"{output}: pending output is stale"]


def test_independent_pass_discovers_added_and_removed_sources(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish(repo, "docs/a.md")
    assert lc.present_problems() == []
    (repo / "docs/c.md").write_text("# C\n\nKeep `--third`.\n")
    git(repo, "add", "docs/c.md")
    publish(repo, "docs/c.md")
    sources = t.sources
    discovered = []

    def discover() -> tuple[str, ...]:
        inventory = sources()
        discovered.append(inventory)
        return inventory

    monkeypatch.setattr(t, "sources", discover)
    assert lc.present_problems() == []
    assert discovered == [("docs/a.md", "docs/b.md", "docs/c.md")]
    git(repo, "rm", "--cached", "docs/c.md")
    assert lc.present_problems() == []
    assert discovered[-1] == ("docs/a.md", "docs/b.md")
    assert len(discovered) == 2
