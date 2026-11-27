"""Present-page validation hashes each source at its point of use."""

from __future__ import annotations

import pathlib
import subprocess
from collections import Counter

import pytest

from scripts.i18n import lifecycle as lc
from scripts.i18n import translate as t


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "docs").mkdir()
    for name in ("a", "b"):
        (tmp_path / f"docs/{name}.md").write_text(f"# {name.upper()}\n\nKeep `--guard` and 42.\n")
    subprocess.run(["git", "add", "docs"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setattr(t, "ROOT", tmp_path)
    monkeypatch.setenv("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    # These independent passes do not participate in source freshness classification.
    monkeypatch.setattr(lc, "link_problems", lambda: [])
    monkeypatch.setattr(t, "untranslated_pages", lambda: [])
    monkeypatch.setattr(t, "language_lines_differ", lambda: [])
    return tmp_path


def publish(root: pathlib.Path, source: str, lang: t.Language) -> pathlib.Path:
    path = root / t.target_path(lang.code, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    prepared, line = t.prepare(source, lang)
    body = "\n".join(t.english_requests(prepared))
    body = body.replace("# A", "# 一").replace("# B", "# 二")
    path.write_text(t.render(source, lang, body, line, t.DEFAULT_MODEL))
    return path


@pytest.mark.parametrize("source_count, language_count", [(1, 1), (1, 3), (2, 1), (2, 3)])
def test_present_pages_hash_the_source_once_at_each_use(
    repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    source_count: int,
    language_count: int,
) -> None:
    sources = [f"docs/{name}.md" for name in ("a", "b")[:source_count]]
    for source in sources:
        for lang in t.LANGUAGES[:language_count]:
            publish(repo, source, lang)
    blob = t.blob
    calls: Counter[str] = Counter()

    def counted(source: str) -> str:
        calls[source] += 1
        return blob(source)

    monkeypatch.setattr(t, "blob", counted)
    assert lc.present_problems() == []
    assert calls == {source: language_count for source in sources}


def test_present_page_freshness_is_not_reused_between_checks(repo: pathlib.Path) -> None:
    source = "docs/a.md"
    path = publish(repo, source, t.LANGUAGES[1])
    previous = path.read_text()
    relative = path.relative_to(repo).as_posix()
    assert lc.present_problems() == []
    (repo / source).write_text("# A\n\nKeep `--changed` and 43.\n")
    assert lc.candidate_problems(relative, previous) == [f"{relative}: pending output is stale"]
    # An old present page stays asynchronous; a current stamp requires full validation.
    assert lc.present_problems() == []
    stamp = t.STAMP.match(previous)
    assert stamp
    path.write_text(previous.replace(stamp[2], t.blob(source), 1))
    assert any("code" in problem or "numbers" in problem for problem in lc.present_problems())
    path.write_text(path.read_text().replace("--guard", "--changed").replace("and 42.", "and 43."))
    assert lc.present_problems() == []


@pytest.mark.parametrize("restamp", [False, True])
def test_present_pages_recheck_source_edits_between_outputs(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, restamp: bool
) -> None:
    source = "docs/a.md"
    paths = sorted(publish(repo, source, lang) for lang in t.LANGUAGES[:2])
    first, later = [path.relative_to(repo).as_posix() for path in paths]
    candidate = lc.candidate_problems
    hash_source = t.blob
    visited = []

    def edit_after_first(
        path: str,
        text: str,
        *,
        current: bool | None = True,
        inventory: frozenset[str] | None = None,
    ) -> list[str]:
        visited.append(path)
        problems = candidate(path, text, current=current, inventory=inventory)
        if path == first:
            (repo / source).write_text("# A\n\nKeep `--changed` and 43.\n")
            if restamp:
                previous = paths[1].read_text()
                stamp = t.STAMP.match(previous)
                assert stamp
                paths[1].write_text(previous.replace(stamp[2], hash_source(source), 1))
        return problems

    monkeypatch.setattr(lc, "candidate_problems", edit_after_first)
    problems = lc.present_problems()
    assert visited == [first, later]
    if restamp:
        # A current stamp must not hide an old body behind a cached source hash.
        assert any(
            problem.startswith(f"{later}:") and ("code" in problem or "numbers" in problem)
            for problem in problems
        )
    else:
        assert problems == []


@pytest.mark.parametrize("corruption", ["banner", "code", "number", "heading", "mapping"])
def test_current_present_pages_keep_their_validation(repo: pathlib.Path, corruption: str) -> None:
    path = publish(repo, "docs/a.md", t.LANGUAGES[1])
    text = path.read_text()
    assert lc.present_problems() == []
    if corruption == "banner":
        text = text.replace("Machine-translated", "Unverified")
    elif corruption == "code":
        text = text.replace("--guard", "--removed")
    elif corruption == "number":
        text = text.replace("and 42.", "and 41.")
    elif corruption == "heading":
        text = text.replace("# 一", "# A")
    else:
        text = text.replace("source=docs/a.md", "source=docs/b.md")
    path.write_text(text)
    assert lc.present_problems()


@pytest.mark.parametrize("boundary", ["file", "parent"])
def test_present_page_rechecks_source_boundary_after_discovery(
    repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    publish(repo, "docs/a.md", t.LANGUAGES[1])
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    (outside / "a.md").write_bytes((repo / "docs/a.md").read_bytes())
    discover = t.sources

    def replaced() -> tuple[str, ...]:
        inventory = discover()
        if boundary == "file":
            (repo / "docs/a.md").unlink()
            (repo / "docs/a.md").symlink_to(outside / "a.md")
        else:
            (repo / "docs").rename(repo / "original-docs")
            (repo / "docs").symlink_to(outside, target_is_directory=True)
        return inventory

    monkeypatch.setattr(t, "sources", replaced)
    assert lc.present_problems()
